"""Durable, resumable transfer state for archive operations.

The payload bytes are authoritative. ``transfer.json`` is an atomic ledger
that avoids hashing already-verified files on every run, but reconciliation
can rebuild it from the pinned upstream inventory and the bytes on disk.
Download transport is provided by the source provider; this module owns
pin/reconcile/plan/verify bookkeeping.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path, PurePosixPath

from .hashing import hash_file, iter_payload_files

TRANSFER_VERSION = 1
TRANSFER_FILE = "transfer.json"
LOCK_FILE = "transfer.lock"
SMALL_FILE_LIMIT = 8 * 1024**2


class LedgerError(ValueError):
    """The transfer ledger cannot be trusted as acceleration state."""


class CleanStop(Exception):
    """Internal control flow for a budget or handled SIGINT stop."""

    def __init__(self, reason: str, detail: str):
        super().__init__(detail)
        self.reason = reason
        self.detail = detail


class PartialTransfer(Exception):
    """A cleanly stopped archive that should make the CLI exit 10."""

    def __init__(self, bundle_dir: Path, reason: str, detail: str, plan: dict):
        super().__init__(detail)
        self.bundle_dir = bundle_dir
        self.reason = reason
        self.detail = detail
        self.plan = plan


class StopController:
    """Coordinate byte/time budgets and a non-destructive SIGINT."""

    def __init__(self, max_bytes: int | None = None, max_minutes: float | None = None):
        self.max_bytes = max_bytes
        self.max_seconds = max_minutes * 60 if max_minutes is not None else None
        self.deadline: float | None = None
        self.interrupted = False

    def start(self) -> None:
        if self.max_seconds is not None:
            self.deadline = time.monotonic() + self.max_seconds

    def check(self, session: dict) -> None:
        if self.interrupted:
            raise CleanStop("interrupt", "SIGINT received")
        if self.max_bytes is not None and session["bytes_network"] >= self.max_bytes:
            raise CleanStop(
                "budget",
                f"network byte budget reached ({session['bytes_network']} >= {self.max_bytes})",
            )
        if self.deadline is not None and time.monotonic() >= self.deadline:
            raise CleanStop("budget", "time budget reached")

    @contextmanager
    def sigint_handler(self):
        if threading.current_thread() is not threading.main_thread():
            yield
            return
        previous = signal.getsignal(signal.SIGINT)

        def request_stop(_signum, _frame):
            self.interrupted = True

        signal.signal(signal.SIGINT, request_stop)
        try:
            yield
        finally:
            signal.signal(signal.SIGINT, previous)


def _utc_now() -> str:
    # Keep the manifest's single timestamp convention without importing
    # archiver at module import time (archiver imports this module).
    from .archiver import utc_now

    return utc_now()


def ledger_path(bundle_dir: Path) -> Path:
    return bundle_dir / TRANSFER_FILE


def load_ledger(bundle_dir: Path) -> dict:
    path = ledger_path(bundle_dir)
    try:
        ledger = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LedgerError(f"cannot read {path}: {exc}") from exc
    if ledger.get("transfer_version") != TRANSFER_VERSION:
        raise LedgerError(
            f"unsupported transfer ledger version {ledger.get('transfer_version')!r} in {path}"
        )
    required = ("repo_id", "repo_type", "revision", "revision_ref", "expected", "metadata")
    missing = [key for key in required if key not in ledger]
    if missing:
        raise LedgerError(f"incomplete transfer ledger {path}: missing {', '.join(missing)}")
    ledger.setdefault("files", {})
    ledger.setdefault("sessions", [])
    ledger.setdefault("events", [])
    return ledger


def save_ledger(bundle_dir: Path, ledger: dict) -> None:
    """Atomically persist the ledger in its bundle directory."""
    path = ledger_path(bundle_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with tmp.open("w", encoding="utf-8") as handle:
            json.dump(ledger, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _bundle_identity(bundle_dir: Path) -> dict:
    """Identify this directory so a lock copied with a bundle is detectable."""
    stat = bundle_dir.stat()
    return {
        "path": str(bundle_dir.resolve()),
        "device": stat.st_dev,
        "inode": stat.st_ino,
    }


def _lock_was_copied(owner: dict, ours: dict) -> bool:
    """Return whether a lock belongs to a different physical directory.

    The absolute path is useful diagnostics, but device/inode identity is the
    safety boundary: aliases of the same live directory must retain mutual
    exclusion, while a filesystem copy (including one carried on removable
    media) must not inherit the source directory's live lock.
    """
    owner_bundle = owner.get("bundle")
    our_bundle = ours["bundle"]
    if not isinstance(owner_bundle, dict):
        return False
    try:
        return (
            int(owner_bundle["device"]),
            int(owner_bundle["inode"]),
        ) != (our_bundle["device"], our_bundle["inode"])
    except (KeyError, TypeError, ValueError):
        return False


@contextmanager
def transfer_lock(bundle_dir: Path, progress=print):
    """Hold the per-bundle lock, reclaiming dead or copied owners."""
    bundle_dir.mkdir(parents=True, exist_ok=True)
    path = bundle_dir / LOCK_FILE
    ours = {
        "pid": os.getpid(),
        "host": socket.gethostname(),
        "started": _utc_now(),
        "bundle": _bundle_identity(bundle_dir),
    }
    while True:
        try:
            with path.open("x", encoding="utf-8") as handle:
                json.dump(ours, handle, indent=2)
                handle.write("\n")
            break
        except FileExistsError:
            try:
                owner = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                owner = {}
            same_host = owner.get("host") == ours["host"]
            try:
                owner_pid = int(owner.get("pid") or 0)
            except (TypeError, ValueError):
                owner_pid = 0
            copied = bool(owner) and _lock_was_copied(owner, ours)
            stale = not owner or copied or (same_host and not _pid_alive(owner_pid))
            if stale:
                kind = "copied" if copied else "stale"
                progress(f"Reclaiming {kind} transfer lock: {path}")
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
                continue
            raise SystemExit(
                f"error: archive already in progress for {bundle_dir} "
                f"(pid {owner.get('pid', '?')} on {owner.get('host', '?')}, "
                f"started {owner.get('started', '?')})"
            )
    try:
        yield
    finally:
        try:
            owner = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            owner = None
        if owner == ours:
            path.unlink(missing_ok=True)


def find_resume(
    vault: Path,
    source,
    revision: str | None,
    payload_root: str,
) -> tuple[Path, dict | None] | None:
    """Find an in-progress bundle before making a provider metadata call.

    A lone payload directory (or corrupt ledger) is also returned so the
    caller can re-pin the revision prefix encoded by its bundle directory and
    reconcile the bytes. Multiple candidates require explicit cleanup rather
    than guessing which immutable revision the user meant.
    """
    from .sources import SourceRef, get_provider

    assert isinstance(source, SourceRef)
    parent = vault / source.bundle_name
    if not parent.is_dir():
        return None
    requested = revision or get_provider(source.provider).default_revision
    matches: list[tuple[Path, dict]] = []
    orphans: list[Path] = []
    for child in sorted(p for p in parent.iterdir() if p.is_dir()):
        if (child / "manifest.json").is_file():
            continue
        transfer = child / TRANSFER_FILE
        if transfer.is_file():
            try:
                ledger = load_ledger(child)
            except LedgerError:
                orphans.append(child)
                continue
            if ledger["repo_id"] != source.locator or ledger["repo_type"] != source.artifact_type:
                continue
            if ledger["revision_ref"] == requested or ledger["revision"] == requested:
                matches.append((child, ledger))
            continue
        payload = child / payload_root
        if payload.is_dir() and any(payload.iterdir()):
            orphans.append(child)
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise SystemExit(
            f"error: multiple in-progress archives match {source.canonical} @ {requested}: "
            + ", ".join(str(path) for path, _ in matches)
        )
    if len(orphans) == 1:
        return orphans[0], None
    if len(orphans) > 1:
        raise SystemExit(
            f"error: multiple ledger-less partial archives exist for {source.canonical}; "
            "specify a revision or resolve them manually: " + ", ".join(map(str, orphans))
        )
    return None


def new_ledger(snapshot, include: list[str] | None = None) -> dict:
    from .subset import select_subset

    files = list(snapshot.files)
    subset = None
    if include:
        files, subset = select_subset(files, include)
    expected = []
    for spec in files:
        expected.append({
            "path": spec.path,
            "size": spec.size,
            "lfs_sha256": spec.sha256,
            "git_sha1": spec.git_sha1,
        })
    expected.sort(key=lambda item: item["path"])
    source = snapshot.source
    ledger = {
        "transfer_version": TRANSFER_VERSION,
        "provider": source.provider,
        "address": source.canonical,
        "repo_id": source.locator,
        "repo_type": source.artifact_type,
        "revision": snapshot.revision,
        "revision_ref": snapshot.revision_ref,
        "pinned_at": _utc_now(),
        "expected": expected,
        "metadata": snapshot.metadata,
        "files": {},
        "sessions": [],
        "events": [],
    }
    if subset is not None:
        ledger["subset"] = subset
    return ledger


def begin_session(bundle_dir: Path, ledger: dict, shard: tuple[int, int] | None = None) -> dict:
    session = {
        "started": _utc_now(),
        "ended": None,
        "end_reason": None,
        "bytes_network": 0,
        "bytes_adopted": 0,
        "bytes_local_sources": 0,
        "files_completed": 0,
        "retries": 0,
        "host": socket.gethostname(),
    }
    if shard is not None:
        session["shard"] = f"{shard[0]}/{shard[1]}"
    ledger["sessions"].append(session)
    save_ledger(bundle_dir, ledger)
    return session


def finish_session(bundle_dir: Path, ledger: dict, session: dict, reason: str) -> None:
    session["ended"] = _utc_now()
    session["end_reason"] = reason
    save_ledger(bundle_dir, ledger)


def record_event(ledger: dict, path: str | None, event: str, detail: str) -> None:
    ledger["events"].append({"at": _utc_now(), "path": path, "event": event, "detail": detail})


def _payload_path(payload_dir: Path, relative: str) -> Path:
    rel = PurePosixPath(relative)
    if rel.is_absolute() or not rel.parts or any(part in ("", ".", "..") for part in rel.parts):
        raise SystemExit(f"error: unsafe path in pinned Hub inventory: {relative!r}")
    return payload_dir.joinpath(*rel.parts)


def _digest_matches(expected: dict, hashes: dict) -> bool | None:
    if expected.get("lfs_sha256"):
        return hashes["sha256"] == expected["lfs_sha256"]
    if expected.get("git_sha1"):
        return hashes["git_sha1"] == expected["git_sha1"]
    return None


def _verified_record(expected: dict, path: Path, source: str, attempts: int) -> dict:
    hashes = hash_file(path, with_git_sha1=True)
    return {
        "status": "verified",
        "size": path.stat().st_size,
        "sha256": hashes["sha256"],
        "blake3": hashes.get("blake3"),
        "git_sha1": hashes["git_sha1"],
        "verified_against_upstream": _digest_matches(expected, hashes),
        "verified_at": _utc_now(),
        "source": source,
        "attempts": attempts,
    }


def _discard_payload_file(path: Path, payload_dir: Path) -> None:
    path.unlink(missing_ok=True)
    parent = path.parent
    while parent != payload_dir:
        try:
            parent.rmdir()
        except OSError:
            break
        parent = parent.parent


def _partial_bytes(payload_dir: Path, expected: dict, ledger: dict | None = None) -> int:
    """Best-effort byte count for provider incomplete files."""
    from .sources import get_provider

    provider = get_provider((ledger or {}).get("provider") or "huggingface")
    return provider.partial_bytes(payload_dir, expected)


def reconcile(
    bundle_dir: Path,
    payload_dir: Path,
    ledger: dict,
    session: dict,
    progress=print,
    apply: bool = True,
    rehash: bool = False,
) -> dict:
    """Reconcile ledger acceleration state with authoritative payload bytes."""
    payload_dir.mkdir(parents=True, exist_ok=True)
    expected_by_path = {item["path"]: item for item in ledger["expected"]}
    present_by_path = dict(iter_payload_files(payload_dir))

    for relative in sorted(set(present_by_path) - set(expected_by_path)):
        path = present_by_path[relative]
        record_event(ledger, relative, "unexpected_file", "removed; not in pinned transfer set")
        if apply:
            _discard_payload_file(path, payload_dir)
            save_ledger(bundle_dir, ledger)

    adopted_files = 0
    adopted_bytes = 0
    for relative, expected in sorted(expected_by_path.items()):
        path = _payload_path(payload_dir, relative)
        state = ledger["files"].get(relative) or {}
        expected_size = expected.get("size")
        size_matches = path.is_file() and (expected_size is None or path.stat().st_size == expected_size)

        if state.get("status") == "verified" and size_matches:
            if not rehash:
                continue
            record = _verified_record(
                expected,
                path,
                str(state.get("source") or "adopted"),
                int(state.get("attempts") or 0),
            )
            matches = record["verified_against_upstream"]
            if matches is None and state.get("sha256"):
                matches = record["sha256"] == state["sha256"]
            if matches is not False:
                ledger["files"][relative] = record
                if apply:
                    save_ledger(bundle_dir, ledger)
                continue
            record_event(
                ledger,
                relative,
                "rehash_mismatch",
                "verified file failed digest re-check; removed and demoted",
            )
            if apply:
                _discard_payload_file(path, payload_dir)
            ledger["files"][relative] = {
                "status": "missing",
                "attempts": int(state.get("attempts") or 0),
            }
            if apply:
                save_ledger(bundle_dir, ledger)
            continue
        if state.get("status") == "verified" and not path.is_file():
            record_event(ledger, relative, "verified_file_missing", "demoted to missing")
        elif path.is_file() and not size_matches:
            actual_size = path.stat().st_size
            record_event(
                ledger,
                relative,
                "size_mismatch",
                f"expected {expected_size}, found {actual_size}; removed and demoted",
            )
            if apply:
                _discard_payload_file(path, payload_dir)
        elif path.is_file():
            record = _verified_record(expected, path, "adopted", int(state.get("attempts") or 0))
            if record["verified_against_upstream"] is not False:
                ledger["files"][relative] = record
                adopted_files += 1
                adopted_bytes += record["size"]
                session["files_completed"] += 1
                session["bytes_adopted"] += record["size"]
                if apply:
                    save_ledger(bundle_dir, ledger)
                continue
            record_event(ledger, relative, "digest_mismatch", "local bytes did not match upstream; removed")
            if apply:
                _discard_payload_file(path, payload_dir)

        ledger["files"][relative] = {
            "status": "missing",
            "attempts": int(state.get("attempts") or 0),
        }
        if apply:
            save_ledger(bundle_dir, ledger)

    if adopted_files:
        progress(f"Adopted {adopted_files} existing files ({adopted_bytes} bytes) after hashing")
    return transfer_plan(payload_dir, ledger)


def transfer_plan(payload_dir: Path, ledger: dict) -> dict:
    counts = {"verified": 0, "partial": 0, "missing": 0}
    bytes_by_state = {"verified": 0, "partial": 0, "missing": 0}
    total = 0
    for expected in ledger["expected"]:
        size = expected.get("size") or 0
        total += size
        state = ledger["files"].get(expected["path"]) or {}
        if state.get("status") == "verified":
            counts["verified"] += 1
            bytes_by_state["verified"] += size
            continue
        partial = min(_partial_bytes(payload_dir, expected, ledger), size) if size else 0
        if partial:
            counts["partial"] += 1
            bytes_by_state["partial"] += partial
        else:
            counts["missing"] += 1
            bytes_by_state["missing"] += size
    remaining = max(0, total - bytes_by_state["verified"] - bytes_by_state["partial"])
    return {
        "files": {**counts, "total": len(ledger["expected"])},
        "bytes": {**bytes_by_state, "total": total, "remaining_network": remaining},
        "complete": counts["verified"] == len(ledger["expected"]),
    }


def add_disk_preflight(bundle_dir: Path, plan: dict) -> dict:
    """Attach estimate-style free-space headroom to a transfer plan."""
    import shutil

    probe = bundle_dir.resolve()
    while not probe.exists():
        probe = probe.parent
    free = shutil.disk_usage(probe).free
    needed = plan["bytes"]["remaining_network"]
    if free >= needed * 1.1:
        verdict = "ok"
    elif free >= needed:
        verdict = "tight"
    else:
        verdict = "insufficient"
    plan["disk"] = {
        "checked_path": str(probe),
        "free_bytes": free,
        "needed_bytes": needed,
        "verdict": verdict,
    }
    return plan


def print_plan(plan: dict, progress=print) -> None:
    from .readme_gen import human_size

    files = plan["files"]
    sizes = plan["bytes"]
    progress("Transfer plan:")
    progress(
        f"  verified: {files['verified']}/{files['total']} files, "
        f"{human_size(sizes['verified'])}"
    )
    progress(
        f"  partial:  {files['partial']} files, {human_size(sizes['partial'])} banked"
    )
    progress(
        f"  missing:  {files['missing']} files; estimated network remaining "
        f"{human_size(sizes['remaining_network'])}"
    )
    disk = plan["disk"]
    progress(
        f"  disk:     needs {human_size(disk['needed_bytes'])}, "
        f"free {human_size(disk['free_bytes'])} at {disk['checked_path']} — "
        f"{disk['verdict'].upper()}"
    )


def transfer_groups(
    expected: list[dict],
    shard: tuple[int, int] | None = None,
) -> list[tuple[int | None, list[dict]]]:
    """Return deterministic byte-balanced lane groups in participant order."""
    if shard is None:
        return [(None, sorted(expected, key=lambda item: (item.get("size") or 0, item["path"])))]

    participant, total_lanes = shard
    lanes: list[list[dict]] = [[] for _ in range(total_lanes)]
    lane_bytes = [0] * total_lanes
    # Longest-processing-time balancing is deterministic and keeps typical
    # equal-sized weight shards evenly distributed by bytes, not file count.
    for item in sorted(expected, key=lambda value: (-(value.get("size") or 0), value["path"])):
        lane = min(
            range(total_lanes),
            key=lambda number: (lane_bytes[number], len(lanes[number]), number),
        )
        lanes[lane].append(item)
        lane_bytes[lane] += item.get("size") or 0
    for lane in lanes:
        lane.sort(key=lambda item: (item.get("size") or 0, item["path"]))

    first = participant - 1
    order = [(first + offset) % total_lanes for offset in range(total_lanes)]
    return [(lane, lanes[lane]) for lane in order]


def print_shard_plan(ledger: dict, shard: tuple[int, int], progress=print) -> None:
    """Describe the cooperative lane this participant will prioritize."""
    from .readme_gen import human_size

    groups = transfer_groups(ledger["expected"], shard)
    lane, assigned = groups[0]
    assigned_bytes = sum(item.get("size") or 0 for item in assigned)
    total_bytes = sum(item.get("size") or 0 for item in ledger["expected"])
    percent = (assigned_bytes * 100 / total_bytes) if total_bytes else 0
    order = " -> ".join(str(number + 1) for number, _items in groups if number is not None)
    progress(
        f"Cooperative shard {shard[0]}/{shard[1]}: lane {lane + 1} first "
        f"({len(assigned)} files, {human_size(assigned_bytes)}, {percent:.1f}% of bytes); "
        f"full lane order {order}"
    )
    if len(assigned) == 0 or len(ledger["expected"]) < shard[1]:
        progress(
            "  WARNING: fewer files than cooperative lanes; this bundle cannot "
            "distribute useful starting work across every participant"
        )
    lane_sizes = [sum(item.get("size") or 0 for item in items) for _lane, items in groups]
    ideal = total_bytes / shard[1] if shard[1] else 0
    if ideal and max(lane_sizes) > ideal * 1.5:
        progress(
            f"  WARNING: whole-file lane sizes are uneven "
            f"({human_size(min(lane_sizes))}–{human_size(max(lane_sizes))}); "
            "a monolithic file limits cooperative coverage"
        )


class NetworkCounter:
    """Receive actual network-byte callbacks from a provider's progress wrapper."""

    def __init__(self, session: dict, stop_controller: StopController | None = None):
        self.session = session
        self.stop_controller = stop_controller
        self.lock = threading.Lock()
        self.pending_stop: CleanStop | None = None

    def add(self, amount: int, defer_only: bool = False) -> None:
        with self.lock:
            self.session["bytes_network"] += max(0, int(amount))
            if self.stop_controller is not None:
                # Providers typically report a chunk immediately before writing
                # it. Defer the first stop until the following callback so the
                # triggering chunk is durably banked in the incomplete file.
                if self.pending_stop is not None:
                    if not defer_only:
                        raise self.pending_stop
                    return
                try:
                    self.stop_controller.check(self.session)
                except CleanStop as stop:
                    self.pending_stop = stop


def _download_one(
    expected: dict,
    payload_dir: Path,
    ledger: dict,
    counter: NetworkCounter,
    stop_controller: StopController | None,
    local_sources: dict[str, list[dict]],
) -> dict:
    """Worker-safe download/hash result; it never writes the ledger."""
    if stop_controller is not None:
        stop_controller.check(counter.session)
    relative = expected["path"]
    path = _payload_path(payload_dir, relative)
    previous = ledger["files"].get(relative) or {}
    attempts = int(previous.get("attempts") or 0)
    events = []
    retries = 0
    record = None

    digest = expected.get("lfs_sha256")
    for candidate in local_sources.get(digest, []) if digest else []:
        attempts += 1
        try:
            method = _copy_local_file(candidate["path"], path)
            record = _verified_record(
                expected,
                path,
                f"local:{candidate['bundle_id']}",
                attempts,
            )
        except OSError as exc:
            _discard_payload_file(path, payload_dir)
            events.append({
                "at": _utc_now(),
                "path": relative,
                "event": "local_source_error",
                "detail": f"{candidate['bundle_id']} could not be copied: {exc}",
            })
            continue
        if record["verified_against_upstream"] is not False:
            record["local_copy_method"] = method
            return {
                "path": relative,
                "record": record,
                "events": events,
                "retries": retries,
                "bytes_local_sources": record["size"],
            }
        events.append({
            "at": _utc_now(),
            "path": relative,
            "event": "local_source_mismatch",
            "detail": f"{candidate['bundle_id']} failed re-verification; falling back",
        })
        _discard_payload_file(path, payload_dir)

    from .sources import get_provider, source_from_ledger

    provider = get_provider(ledger.get("provider") or "huggingface")
    source = source_from_ledger(ledger)
    tqdm_class = provider.progress_wrapper(counter)

    for retry in range(2):
        attempts += 1
        try:
            provider.download_file(
                source,
                ledger["revision"],
                relative,
                payload_dir,
                force=retry > 0,
                tqdm_class=tqdm_class,
            )
        except BaseException:
            if counter.pending_stop is not None:
                raise counter.pending_stop from None
            raise
        record = _verified_record(expected, path, "network", attempts)
        if record["verified_against_upstream"] is not False:
            break
        events.append({
            "at": _utc_now(),
            "path": relative,
            "event": "digest_mismatch",
            "detail": f"download attempt {attempts} did not match pinned upstream digest",
        })
        if retry == 0:
            retries += 1
            _discard_payload_file(path, payload_dir)
    assert record is not None
    if record["verified_against_upstream"] is False:
        events.append({
            "at": _utc_now(),
            "path": relative,
            "event": "persistent_digest_mismatch",
            "detail": "second download mismatch; retained and marked as an upstream verification failure",
        })
    return {
        "path": relative,
        "record": record,
        "events": events,
        "retries": retries,
        "bytes_local_sources": 0,
    }


def _copy_local_file(source: Path, destination: Path) -> str:
    """Copy an independent file, preferring APFS copy-on-write cloning."""
    if not source.is_file() or source.is_symlink():
        raise OSError(f"source file is absent or not a regular file: {source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.unlink(missing_ok=True)
    if sys.platform == "darwin":
        try:
            result = subprocess.run(
                ["cp", "-c", str(source), str(destination)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        except OSError:
            result = None
        if result is not None and result.returncode == 0:
            return "clonefile"
        destination.unlink(missing_ok=True)
    shutil.copy2(source, destination)
    return "copy"


def local_source_index(bundle_dir: Path, ledger: dict) -> dict[str, list[dict]]:
    """Index registered sibling LFS blobs from the same Hub repository."""
    index: dict[str, list[dict]] = {}
    for manifest_path in sorted(bundle_dir.parent.glob("*/manifest.json")):
        if manifest_path.parent == bundle_dir:
            continue
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if (
                manifest.get("artifact_type") != ledger["repo_type"]
                or manifest.get("source", {}).get("repo_id") != ledger["repo_id"]
            ):
                continue
            bundle_id = manifest["bundle_id"]
            for item in manifest.get("inventory", {}).get("files", []):
                digest = item.get("upstream_lfs_sha256")
                if (
                    not digest
                    or item.get("sha256") != digest
                    or item.get("verified_against_upstream") is False
                ):
                    continue
                relative = PurePosixPath(item["path"])
                if (
                    relative.is_absolute()
                    or not relative.parts
                    or any(part in ("", ".", "..") for part in relative.parts)
                ):
                    continue
                source = manifest_path.parent.joinpath(*relative.parts)
                index.setdefault(digest, []).append({
                    "bundle_id": bundle_id,
                    "path": source,
                })
        except (json.JSONDecodeError, KeyError, OSError, TypeError):
            continue
    return index


def _record_download_result(bundle_dir: Path, ledger: dict, session: dict, result: dict) -> None:
    """Main-thread commit point for a worker's completed file."""
    ledger["events"].extend(result["events"])
    ledger["files"][result["path"]] = result["record"]
    session["files_completed"] += 1
    session["retries"] += result["retries"]
    session["bytes_local_sources"] += result.get("bytes_local_sources", 0)
    save_ledger(bundle_dir, ledger)


def _transfer_small_files(
    small: list[dict],
    bundle_dir: Path,
    payload_dir: Path,
    ledger: dict,
    session: dict,
    counter: NetworkCounter,
    local_sources: dict[str, list[dict]],
    stop_controller: StopController | None,
    jobs: int,
    progress,
) -> None:
    if not small:
        return
    progress(
        f"Transferring {len(small)} small files with {min(jobs, len(small))} workers "
        f"(< {SMALL_FILE_LIMIT} bytes each) ..."
    )
    clean_stop = None
    first_error = None
    with ThreadPoolExecutor(max_workers=jobs) as executor:
        futures = {
            executor.submit(
                _download_one,
                expected,
                payload_dir,
                ledger,
                counter,
                stop_controller,
                local_sources,
            ): expected
            for expected in small
        }
        for future in as_completed(futures):
            expected = futures[future]
            try:
                result = future.result()
            except CleanStop as stop:
                if clean_stop is None or stop.reason == "interrupt":
                    clean_stop = stop
            except BaseException as exc:
                if first_error is None:
                    first_error = exc
            else:
                source = result["record"]["source"]
                suffix = f" from {source}" if source.startswith("local:") else ""
                progress(f"Verified {expected['path']} ({result['record']['size']} bytes){suffix}")
                _record_download_result(bundle_dir, ledger, session, result)
                if stop_controller is not None:
                    try:
                        stop_controller.check(session)
                    except CleanStop as stop:
                        if clean_stop is None or stop.reason == "interrupt":
                            clean_stop = stop
    if first_error is not None:
        raise first_error
    if clean_stop is not None:
        raise clean_stop


def transfer_all(
    bundle_dir: Path,
    payload_dir: Path,
    ledger: dict,
    session: dict,
    progress=print,
    stop_controller: StopController | None = None,
    jobs: int = 4,
    shard: tuple[int, int] | None = None,
) -> dict:
    """Fetch and immediately verify every remaining file at the pinned commit."""
    from .sources import get_provider

    counter = NetworkCounter(session, stop_controller)
    local_sources = local_source_index(bundle_dir, ledger)
    groups = transfer_groups(ledger["expected"], shard)
    provider = get_provider(ledger.get("provider") or "huggingface")

    with provider.transfer_session(payload_dir):
        for lane, assigned in groups:
            remaining = [
                expected for expected in assigned
                if (ledger["files"].get(expected["path"]) or {}).get("status") != "verified"
            ]
            if not remaining:
                continue
            if lane is not None:
                progress(f"Cooperative lane {lane + 1}/{shard[1]} ...")
            small = [item for item in remaining if (item.get("size") or 0) < SMALL_FILE_LIMIT]
            large = [item for item in remaining if (item.get("size") or 0) >= SMALL_FILE_LIMIT]
            _transfer_small_files(
                small,
                bundle_dir,
                payload_dir,
                ledger,
                session,
                counter,
                local_sources,
                stop_controller,
                jobs,
                progress,
            )

            for index, expected in enumerate(large, 1):
                progress(
                    f"Transferring large file {index}/{len(large)}: {expected['path']} "
                    f"({expected.get('size') or 0} bytes)"
                )
                result = _download_one(
                    expected,
                    payload_dir,
                    ledger,
                    counter,
                    stop_controller,
                    local_sources,
                )
                _record_download_result(bundle_dir, ledger, session, result)
                if stop_controller is not None:
                    stop_controller.check(session)

    return transfer_plan(payload_dir, ledger)


def _same_transfer_set(left: dict, right: dict) -> bool:
    if not all(left.get(key) == right.get(key) for key in (
        "transfer_version",
        "repo_id",
        "repo_type",
        "revision",
        "expected",
    )):
        return False
    aliases = {"huggingface", "hf"}
    left_p = left.get("provider") or "huggingface"
    right_p = right.get("provider") or "huggingface"
    if left_p in aliases:
        left_p = "huggingface"
    if right_p in aliases:
        right_p = "huggingface"
    return left_p == right_p


def _merge_transfer_caches(
    source_payloads: list[Path],
    destination_payload: Path,
) -> tuple[int, int]:
    """Merge portable provider caches with one copy of each longest byte partial."""
    destination_cache = destination_payload / ".cache"
    copied_files = 0
    copied_partial_bytes = 0
    candidates: dict[Path, Path] = {}
    for source_payload in source_payloads:
        source_cache = source_payload / ".cache"
        if not source_cache.is_dir():
            continue
        for source in sorted(source_cache.rglob("*")):
            if not source.is_file() or source.is_symlink() or source.name.endswith(".lock"):
                continue
            relative = source.relative_to(source_cache)
            current = candidates.get(relative)
            if current is None or (
                source.name.endswith(".incomplete")
                and source.stat().st_size > current.stat().st_size
            ):
                candidates[relative] = source

    for relative, source in sorted(candidates.items()):
        destination = destination_cache / relative
        if source.name.endswith(".incomplete"):
            source_size = source.stat().st_size
            if destination.is_file() and destination.stat().st_size >= source_size:
                continue
            _copy_local_file(source, destination)
            copied_files += 1
            copied_partial_bytes += source_size
        elif not destination.exists():
            _copy_local_file(source, destination)
            copied_files += 1
    return copied_files, copied_partial_bytes


def assemble_partials(
    partials: list[Path],
    vault: Path,
    progress=print,
) -> tuple[Path, dict]:
    """Combine matching partial bundles offline into one resumable target."""
    import copy

    from .archiver import bundle_dir_for
    from .schema import payload_root_for
    from .sources import source_from_ledger

    if not partials:
        raise SystemExit("error: assemble needs at least one partial bundle")

    sources = []
    for source_dir in partials:
        try:
            source_ledger = load_ledger(source_dir)
        except LedgerError as exc:
            raise SystemExit(f"error: cannot assemble {source_dir}: {exc}") from exc
        sources.append((source_dir, source_ledger))

    seed = sources[0][1]
    for source_dir, source_ledger in sources[1:]:
        if not _same_transfer_set(seed, source_ledger):
            raise SystemExit(
                f"error: {source_dir} does not have the same pinned repository, "
                "revision, and expected inventory as the first partial"
            )

    destination = bundle_dir_for(
        vault,
        source_from_ledger(seed),
        seed["revision"],
    )
    root = payload_root_for(seed["repo_type"])
    destination_payload = destination / root
    expected_paths = {item["path"] for item in seed["expected"]}

    with transfer_lock(destination, progress=progress):
        if (destination / "manifest.json").is_file():
            raise SystemExit(f"error: destination is already a registered bundle: {destination}")
        if ledger_path(destination).is_file():
            ledger = load_ledger(destination)
            if not _same_transfer_set(seed, ledger):
                raise SystemExit(
                    f"error: destination partial has a different transfer set: {destination}"
                )
        else:
            ledger = copy.deepcopy(seed)
            ledger["files"] = {}
            ledger["sessions"] = []
            ledger["events"] = []
            save_ledger(destination, ledger)

        session = begin_session(destination, ledger)
        session["assembly_sources"] = len(sources)
        session_finished = False
        copied_payload_files = 0
        copied_cache_files = 0
        copied_partial_bytes = 0
        try:
            # Validate anything already present in the destination before it
            # suppresses a good copy from one of the incoming partials.
            plan = reconcile(
                destination,
                destination_payload,
                ledger,
                session,
                progress=progress,
                rehash=True,
            )
            for ordinal, (source_dir, source_ledger) in enumerate(sources, 1):
                source_payload = source_dir / root
                progress(f"Merging partial {ordinal}/{len(sources)}: {source_dir}")
                for relative, source_file in iter_payload_files(source_payload):
                    if relative not in expected_paths:
                        continue
                    destination_file = _payload_path(destination_payload, relative)
                    if destination_file.exists():
                        continue
                    _copy_local_file(source_file, destination_file)
                    copied_payload_files += 1

                hosts = sorted({
                    str(item.get("host"))
                    for item in source_ledger.get("sessions", [])
                    if item.get("host")
                })
                host_note = ", ".join(hosts) if hosts else "unknown host"
                record_event(
                    ledger,
                    None,
                    "assembled_partial",
                    f"merged cooperative input {ordinal}/{len(sources)} from {host_note}",
                )
                plan = reconcile(
                    destination,
                    destination_payload,
                    ledger,
                    session,
                    progress=progress,
                )

            copied_cache_files, copied_partial_bytes = _merge_transfer_caches(
                [source_dir / root for source_dir, _source_ledger in sources],
                destination_payload,
            )

            finish_session(destination, ledger, session, "assemble")
            session_finished = True
        except BaseException:
            if not session_finished:
                finish_session(destination, ledger, session, "error")
            raise

        plan = add_disk_preflight(destination, transfer_plan(destination_payload, ledger))
        progress(
            f"Assembled {copied_payload_files} payload files and {copied_cache_files} cache files "
            f"({copied_partial_bytes} partial bytes copied)"
        )
        print_plan(plan, progress=progress)
        return destination, plan


def file_records(ledger: dict, payload_root: str) -> tuple[list[dict], list[str]]:
    """Build manifest inventory records from completed ledger hashes."""
    records = []
    mismatches = []
    for expected in sorted(ledger["expected"], key=lambda item: item["path"]):
        state = ledger["files"].get(expected["path"]) or {}
        if state.get("status") != "verified":
            raise LedgerError(f"cannot register: {expected['path']} is not verified")
        if state.get("verified_against_upstream") is False:
            mismatches.append(expected["path"])
        records.append({
            "path": f"{payload_root}/{expected['path']}",
            "size": state["size"],
            "sha256": state["sha256"],
            "blake3": state.get("blake3"),
            "upstream_lfs_sha256": expected.get("lfs_sha256"),
            "upstream_git_sha1": expected.get("git_sha1"),
            "verified_against_upstream": state.get("verified_against_upstream"),
        })
    return records, mismatches


def transfer_summary(ledger: dict) -> dict:
    sessions = ledger.get("sessions") or []
    return {
        "sessions": len(sessions),
        "started": sessions[0]["started"] if sessions else ledger["pinned_at"],
        "completed": sessions[-1].get("ended") if sessions else None,
        "bytes_network": sum(int(s.get("bytes_network") or 0) for s in sessions),
        "bytes_adopted": sum(int(s.get("bytes_adopted") or 0) for s in sessions),
        "bytes_local_sources": sum(int(s.get("bytes_local_sources") or 0) for s in sessions),
        "retries": sum(int(s.get("retries") or 0) for s in sessions),
    }


def local_mirrors(ledger: dict) -> list[str]:
    """Return stable local-source provenance for the registered manifest."""
    return sorted({
        state["source"]
        for state in ledger.get("files", {}).values()
        if str(state.get("source") or "").startswith("local:")
    })

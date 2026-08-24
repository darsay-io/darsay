"""Durable, resumable transfer state for Hub archive operations.

The payload bytes are authoritative. ``transfer.json`` is an atomic ledger
that avoids hashing already-verified files on every run, but reconciliation
can rebuild it from the pinned upstream inventory and the bytes on disk.
"""

from __future__ import annotations

import json
import os
import signal
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path, PurePosixPath

from huggingface_hub import hf_hub_download

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


def _json_value(value):
    """Return a JSON-safe copy of Hub metadata without inventing values."""
    if isinstance(value, datetime):
        return value.isoformat(timespec="seconds")
    if isinstance(value, dict):
        return {str(k): _json_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(v) for v in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


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


@contextmanager
def transfer_lock(bundle_dir: Path, progress=print):
    """Hold the per-bundle lock, reclaiming a dead same-host owner."""
    bundle_dir.mkdir(parents=True, exist_ok=True)
    path = bundle_dir / LOCK_FILE
    ours = {"pid": os.getpid(), "host": socket.gethostname(), "started": _utc_now()}
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
            stale = not owner or (same_host and not _pid_alive(owner_pid))
            if stale:
                progress(f"Reclaiming stale transfer lock: {path}")
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
    repo_id: str,
    repo_type: str,
    revision: str | None,
    payload_root: str,
) -> tuple[Path, dict | None] | None:
    """Find an in-progress bundle before making a Hub metadata call.

    A lone payload directory (or corrupt ledger) is also returned so the
    caller can re-pin the commit prefix encoded by its bundle directory and
    reconcile the bytes. Multiple candidates require explicit cleanup rather
    than guessing which immutable revision the user meant.
    """
    from .archiver import bundle_name_for

    parent = vault / bundle_name_for(repo_id, repo_type)
    if not parent.is_dir():
        return None
    requested = revision or "main"
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
            if ledger["repo_id"] != repo_id or ledger["repo_type"] != repo_type:
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
            f"error: multiple in-progress archives match {repo_id} @ {requested}: "
            + ", ".join(str(path) for path, _ in matches)
        )
    if len(orphans) == 1:
        return orphans[0], None
    if len(orphans) > 1:
        raise SystemExit(
            f"error: multiple ledger-less partial archives exist for {repo_id}; "
            "specify a revision or resolve them manually: " + ", ".join(map(str, orphans))
        )
    return None


def new_ledger(repo_id: str, repo_type: str, revision_ref: str, info) -> dict:
    expected = []
    for sibling in info.siblings or []:
        expected.append({
            "path": sibling.rfilename,
            "size": sibling.size,
            "lfs_sha256": sibling.lfs.sha256 if sibling.lfs else None,
            "git_sha1": sibling.blob_id if not sibling.lfs else None,
        })
    expected.sort(key=lambda item: item["path"])
    card = info.card_data.to_dict() if info.card_data else {}
    return {
        "transfer_version": TRANSFER_VERSION,
        "repo_id": repo_id,
        "repo_type": repo_type,
        "revision": info.sha,
        "revision_ref": revision_ref,
        "pinned_at": _utc_now(),
        "expected": expected,
        "metadata": {
            "card_data": _json_value(card),
            "tags": list(info.tags or []),
            "gated": getattr(info, "gated", None) or False,
            "created_at": (
                info.created_at.isoformat(timespec="seconds") if info.created_at else None
            ),
            "last_modified": (
                info.last_modified.isoformat(timespec="seconds") if info.last_modified else None
            ),
            "downloads": getattr(info, "downloads", None),
            "likes": getattr(info, "likes", None),
        },
        "files": {},
        "sessions": [],
        "events": [],
    }


def begin_session(bundle_dir: Path, ledger: dict) -> dict:
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


def _partial_bytes(payload_dir: Path, expected: dict) -> int:
    """Best-effort byte count for Hub local-dir incomplete files."""
    etag = expected.get("lfs_sha256") or expected.get("git_sha1")
    if not etag:
        return 0
    try:
        from huggingface_hub._local_folder import get_local_download_paths

        paths = get_local_download_paths(payload_dir, expected["path"])
        path = paths.incomplete_path(etag)
        return path.stat().st_size if path.is_file() else 0
    except (ImportError, OSError, ValueError):
        return 0


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
        partial = min(_partial_bytes(payload_dir, expected), size) if size else 0
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


class NetworkCounter:
    """Receive actual network-byte callbacks from huggingface_hub progress."""

    def __init__(self, session: dict, stop_controller: StopController | None = None):
        self.session = session
        self.stop_controller = stop_controller
        self.lock = threading.Lock()
        self.pending_stop: CleanStop | None = None

    def add(self, amount: int, defer_only: bool = False) -> None:
        with self.lock:
            self.session["bytes_network"] += max(0, int(amount))
            if self.stop_controller is not None:
                # huggingface_hub reports a chunk immediately before writing it.
                # Defer the first stop until the following callback so the
                # triggering chunk is durably banked in the incomplete file.
                if self.pending_stop is not None:
                    if not defer_only:
                        raise self.pending_stop
                    return
                try:
                    self.stop_controller.check(self.session)
                except CleanStop as stop:
                    self.pending_stop = stop


def _tqdm_class(counter: NetworkCounter):
    from tqdm.auto import tqdm

    class TransferTqdm(tqdm):
        def __init__(self, *args, **kwargs):
            name = str(kwargs.get("name") or "")
            desc = str(kwargs.get("desc") or "")
            self._modelvault_xet = name.startswith("huggingface_hub.xet_get") or (
                "reconstructing file" in desc
            )
            super().__init__(*args, **kwargs)

        def update_transfer(self, amount=1):
            counter.add(amount, defer_only=self._modelvault_xet)
            if self._modelvault_xet and counter.pending_stop is not None:
                # Python exceptions raised by a Rust progress callback are
                # reported as unraisable and do not stop Xet. Abort its global
                # session explicitly; the caller translates the resulting
                # transport exception back to the pending clean stop.
                from huggingface_hub.utils._xet import abort_xet_session

                abort_xet_session()

        def set_transfer_postfix_str(self, *args, **kwargs):
            # Xet routes its actual network-byte counter through this class in
            # addition to the ordinary reconstruction progress bar.
            return None

    return TransferTqdm


@contextmanager
def _resumable_hub_transport():
    """Restore safe same-bundle partial resume around ``hf_hub_download``.

    huggingface_hub 1.18 switched to process-unique temporary files and
    intentionally stopped preserving cross-call partials. modelvault has a
    stronger per-bundle lock, so a stable local-dir incomplete file is safe
    here. The Hub client still owns metadata, HTTP Range requests, retries,
    Xet, and the final move; this wrapper only restores its former temp-file
    lifetime while the modelvault lock is held.
    """
    import huggingface_hub.file_download as file_download

    original = file_download._download_to_tmp_and_move

    def resumable_download(
        incomplete_path,
        destination_path,
        url_to_download,
        headers,
        expected_size,
        filename,
        force_download,
        etag,
        xet_file_data,
        tqdm_class=None,
    ):
        if destination_path.exists() and not force_download:
            return
        if incomplete_path.exists() and force_download:
            incomplete_path.unlink(missing_ok=True)
        with incomplete_path.open("ab") as handle:
            resume_size = handle.tell()
            if expected_size is not None:
                file_download._check_disk_space(expected_size, incomplete_path.parent)
                file_download._check_disk_space(expected_size, destination_path.parent)
            if xet_file_data is not None and file_download.is_xet_available():
                file_download.xet_get(
                    incomplete_path=incomplete_path,
                    xet_file_data=xet_file_data,
                    headers=headers,
                    expected_size=expected_size,
                    displayed_filename=filename,
                    tqdm_class=tqdm_class,
                )
            else:
                file_download.http_get(
                    url_to_download,
                    handle,
                    resume_size=resume_size,
                    headers=headers,
                    expected_size=expected_size,
                    tqdm_class=tqdm_class,
                )
        file_download._chmod_and_move(incomplete_path, destination_path)

    file_download._download_to_tmp_and_move = resumable_download
    try:
        yield
    finally:
        file_download._download_to_tmp_and_move = original


def _download_one(
    expected: dict,
    payload_dir: Path,
    ledger: dict,
    counter: NetworkCounter,
    stop_controller: StopController | None,
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
    for retry in range(2):
        attempts += 1
        try:
            hf_hub_download(
                repo_id=ledger["repo_id"],
                filename=relative,
                revision=ledger["revision"],
                local_dir=payload_dir,
                repo_type=ledger["repo_type"],
                force_download=retry > 0,
                tqdm_class=_tqdm_class(counter),
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
    return {"path": relative, "record": record, "events": events, "retries": retries}


def _record_download_result(bundle_dir: Path, ledger: dict, session: dict, result: dict) -> None:
    """Main-thread commit point for a worker's completed file."""
    ledger["events"].extend(result["events"])
    ledger["files"][result["path"]] = result["record"]
    session["files_completed"] += 1
    session["retries"] += result["retries"]
    save_ledger(bundle_dir, ledger)


def transfer_all(
    bundle_dir: Path,
    payload_dir: Path,
    ledger: dict,
    session: dict,
    progress=print,
    stop_controller: StopController | None = None,
    jobs: int = 4,
) -> dict:
    """Fetch and immediately verify every remaining file at the pinned commit."""
    remaining = [
        expected for expected in ledger["expected"]
        if (ledger["files"].get(expected["path"]) or {}).get("status") != "verified"
    ]
    remaining.sort(key=lambda item: (item.get("size") or 0, item["path"]))
    small = [item for item in remaining if (item.get("size") or 0) < SMALL_FILE_LIMIT]
    large = [item for item in remaining if (item.get("size") or 0) >= SMALL_FILE_LIMIT]
    counter = NetworkCounter(session, stop_controller)

    with _resumable_hub_transport():
        if small:
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
                        progress(f"Verified {expected['path']} ({result['record']['size']} bytes)")
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
            )
            _record_download_result(bundle_dir, ledger, session, result)
            if stop_controller is not None:
                stop_controller.check(session)

    return transfer_plan(payload_dir, ledger)


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

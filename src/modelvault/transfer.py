"""Durable, resumable transfer state for Hub archive operations.

The payload bytes are authoritative. ``transfer.json`` is an atomic ledger
that avoids hashing already-verified files on every run, but reconciliation
can rebuild it from the pinned upstream inventory and the bytes on disk.
"""

from __future__ import annotations

import json
import os
import socket
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path, PurePosixPath

from huggingface_hub import hf_hub_download

from .hashing import hash_file, iter_payload_files

TRANSFER_VERSION = 1
TRANSFER_FILE = "transfer.json"
LOCK_FILE = "transfer.lock"


class LedgerError(ValueError):
    """The transfer ledger cannot be trusted as acceleration state."""


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


def _partial_bytes(payload_dir: Path, relative: str) -> int:
    """Best-effort byte count for Hub local-dir incomplete files."""
    rel = PurePosixPath(relative)
    download_dir = payload_dir / ".cache" / "huggingface" / "download"
    parent = download_dir.joinpath(*rel.parts[:-1])
    if not parent.is_dir():
        return 0
    sizes = [p.stat().st_size for p in parent.glob(f"{rel.name}*.incomplete") if p.is_file()]
    return max(sizes, default=0)


def reconcile(bundle_dir: Path, payload_dir: Path, ledger: dict, session: dict, progress=print) -> dict:
    """Reconcile ledger acceleration state with authoritative payload bytes."""
    payload_dir.mkdir(parents=True, exist_ok=True)
    expected_by_path = {item["path"]: item for item in ledger["expected"]}
    present_by_path = dict(iter_payload_files(payload_dir))

    for relative in sorted(set(present_by_path) - set(expected_by_path)):
        path = present_by_path[relative]
        record_event(ledger, relative, "unexpected_file", "removed; not in pinned transfer set")
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
            _discard_payload_file(path, payload_dir)
        elif path.is_file():
            record = _verified_record(expected, path, "adopted", int(state.get("attempts") or 0))
            if record["verified_against_upstream"] is not False:
                ledger["files"][relative] = record
                adopted_files += 1
                adopted_bytes += record["size"]
                session["files_completed"] += 1
                session["bytes_adopted"] += record["size"]
                save_ledger(bundle_dir, ledger)
                continue
            record_event(ledger, relative, "digest_mismatch", "local bytes did not match upstream; removed")
            _discard_payload_file(path, payload_dir)

        ledger["files"][relative] = {
            "status": "missing",
            "attempts": int(state.get("attempts") or 0),
        }
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
        partial = min(_partial_bytes(payload_dir, expected["path"]), size) if size else 0
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


class NetworkCounter:
    """Receive actual network-byte callbacks from huggingface_hub progress."""

    def __init__(self, session: dict):
        self.session = session

    def add(self, amount: int) -> None:
        self.session["bytes_network"] += max(0, int(amount))


def _tqdm_class(counter: NetworkCounter):
    from tqdm.auto import tqdm

    class TransferTqdm(tqdm):
        def update_transfer(self, amount=1):
            counter.add(amount)

        def set_transfer_postfix_str(self, *args, **kwargs):
            # Xet routes its actual network-byte counter through this class in
            # addition to the ordinary reconstruction progress bar.
            return None

    return TransferTqdm


def transfer_all(
    bundle_dir: Path,
    payload_dir: Path,
    ledger: dict,
    session: dict,
    progress=print,
) -> dict:
    """Fetch and immediately verify every remaining file at the pinned commit."""
    remaining = [
        expected for expected in ledger["expected"]
        if (ledger["files"].get(expected["path"]) or {}).get("status") != "verified"
    ]
    remaining.sort(key=lambda item: (item.get("size") or 0, item["path"]))
    counter = NetworkCounter(session)
    tqdm_class = _tqdm_class(counter)

    for index, expected in enumerate(remaining, 1):
        relative = expected["path"]
        path = _payload_path(payload_dir, relative)
        previous = ledger["files"].get(relative) or {}
        attempts = int(previous.get("attempts") or 0)
        progress(
            f"Transferring {index}/{len(remaining)}: {relative} "
            f"({expected.get('size') or 0} bytes)"
        )
        record = None
        for retry in range(2):
            attempts += 1
            hf_hub_download(
                repo_id=ledger["repo_id"],
                filename=relative,
                revision=ledger["revision"],
                local_dir=payload_dir,
                repo_type=ledger["repo_type"],
                force_download=retry > 0,
                tqdm_class=tqdm_class,
            )
            record = _verified_record(expected, path, "network", attempts)
            if record["verified_against_upstream"] is not False:
                break
            record_event(
                ledger,
                relative,
                "digest_mismatch",
                f"download attempt {attempts} did not match pinned upstream digest",
            )
            if retry == 0:
                session["retries"] += 1
                _discard_payload_file(path, payload_dir)
                save_ledger(bundle_dir, ledger)
        assert record is not None
        if record["verified_against_upstream"] is False:
            record_event(
                ledger,
                relative,
                "persistent_digest_mismatch",
                "second download mismatch; retained and marked as an upstream verification failure",
            )
        ledger["files"][relative] = record
        session["files_completed"] += 1
        save_ledger(bundle_dir, ledger)

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

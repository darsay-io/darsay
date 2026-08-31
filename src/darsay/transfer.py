"""Durable, resumable transfer state for archive operations.

The payload bytes are authoritative. ``transfer.json`` is an atomic ledger
that avoids hashing already-verified files on every run, but reconciliation
can rebuild it from the pinned upstream inventory and the bytes on disk.
Download transport is provided by the source provider; this module owns
pin/reconcile/plan/verify bookkeeping.
"""

from __future__ import annotations

import errno
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from collections import deque
from concurrent.futures import (
    FIRST_COMPLETED,
    CancelledError,
    Future,
    ThreadPoolExecutor,
    as_completed,
    wait,
)
from contextlib import contextmanager, suppress
from pathlib import Path, PurePosixPath

from . import __version__
from .hashing import hash_file, iter_payload_files

TRANSFER_VERSION = 1
TRANSFER_FILE = "transfer.json"
LOCK_FILE = "transfer.lock"
SMALL_FILE_LIMIT = 8 * 1024**2
# check() runs at every transfer chunk and hash chunk; probe the disk at
# most this often so the floor guard stays off the hot path.
DISK_PROBE_INTERVAL_S = 2.0
# Reconnect schedule once the network goes away: quick first retries (a
# laptop re-associating with Wi-Fi is back in seconds), then one attempt
# every 30 s until the patience window closes.
RECONNECT_WAITS_S = (2.0, 4.0, 8.0, 15.0, 30.0)
# Pacing and reconnect sleeps run in slices this long so Ctrl-C, budgets,
# and the floor land promptly even mid-wait.
PACE_SLICE_S = 0.2


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
    """Coordinate byte/time budgets, the free-space floor, and a
    non-destructive SIGINT.

    Ctrl-C escalates: the first press requests a clean stop (checked at every
    transfer callback and hash chunk, so it lands fast), the second raises
    KeyboardInterrupt in the main thread to abort even a stalled connection,
    and a third hard-exits the process.
    """

    def __init__(
        self,
        max_bytes: int | None = None,
        max_minutes: float | None = None,
        min_free_bytes: int | None = None,
        disk_path: Path | None = None,
    ):
        self.max_bytes = max_bytes
        self.max_seconds = max_minutes * 60 if max_minutes is not None else None
        self.min_free_bytes = min_free_bytes
        self.disk_path = disk_path
        self.deadline: float | None = None
        self.interrupted = False
        self.sigints = 0
        self._disk_stop: CleanStop | None = None
        self._next_disk_probe = 0.0

    def start(self) -> None:
        if self.max_seconds is not None:
            self.deadline = time.monotonic() + self.max_seconds

    def wants_stop(self) -> bool:
        """Cheap, non-raising: has a stop already been requested or tripped?"""
        return (
            self.interrupted
            or self._disk_stop is not None
            or (self.deadline is not None and time.monotonic() >= self.deadline)
        )

    def check_interrupt(self) -> None:
        """Raise for Ctrl-C only.

        For work that spends no network bytes and no disk — a digest pass
        over a file that already landed — budgets and the floor are not a
        reason to stop; the bytes are paid for and the hash is seconds away.
        """
        if self.interrupted:
            raise CleanStop("interrupt", "SIGINT received")

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
        if self.min_free_bytes and self.disk_path is not None:
            self._check_free_space()

    def _check_free_space(self) -> None:
        """Sticky floor guard so every worker stops once one probe trips.

        Races on the throttle timestamp are benign — at worst two threads
        probe in the same interval.
        """
        if self._disk_stop is not None:
            raise self._disk_stop
        now = time.monotonic()
        if now < self._next_disk_probe:
            return
        self._next_disk_probe = now + DISK_PROBE_INTERVAL_S
        try:
            free = shutil.disk_usage(self.disk_path).free
        except OSError:
            return
        if free < self.min_free_bytes:
            from .readme_gen import human_size

            raise self.stop_for_disk(
                "destination free space fell below the floor "
                f"({human_size(free)} free < {human_size(self.min_free_bytes)} floor)"
            )

    def stop_for_disk(self, detail: str) -> CleanStop:
        """Trip the sticky ``disk`` stop from outside the floor probe.

        ENOSPC from a write and a file that cannot fit above the floor both
        end the session the way the floor does, and every worker stops at
        its next check.
        """
        if self._disk_stop is None:
            self._disk_stop = CleanStop("disk", detail)
        return self._disk_stop

    def check_headroom(self, relative: str, needed: int) -> None:
        """Refuse to begin a file that cannot finish above the floor.

        The floor probe pauses mid-file, once the space is already gone;
        this pauses at the file boundary before it, so a 5 GiB shard is
        never started with 3 GiB of headroom. Files are transferred
        smallest first, so once one does not fit nothing after it does
        either: the stop is sticky like the floor's. Disabled with the
        floor (``--min-free 0``); ENOSPC then pauses the same way.
        """
        if not (self.min_free_bytes and self.disk_path is not None) or needed <= 0:
            return
        if self._disk_stop is not None:
            raise self._disk_stop
        try:
            free = shutil.disk_usage(self.disk_path).free
        except OSError:
            return
        headroom = free - self.min_free_bytes
        if headroom < needed:
            from .readme_gen import human_size

            raise self.stop_for_disk(
                f"{relative} needs {human_size(needed)} more, but only "
                f"{human_size(max(0, headroom))} is free above the "
                f"{human_size(self.min_free_bytes)} floor"
            )

    @contextmanager
    def sigint_handler(self):
        if threading.current_thread() is not threading.main_thread():
            yield
            return
        previous = signal.getsignal(signal.SIGINT)

        def request_stop(_signum, _frame):
            self.sigints += 1
            if self.sigints == 1:
                self.interrupted = True
                return
            if self.sigints == 2:
                # The handler runs in the main thread, so this aborts even a
                # blocked read; workers stop at their next callback check.
                raise KeyboardInterrupt
            # Leave the terminal usable even though cleanup is skipped.
            if os.isatty(2):
                os.write(2, b"\x1b[?2026l\x1b[?25h\r\ndarsay: killed\r\n")
            os._exit(130)

        signal.signal(signal.SIGINT, request_stop)
        try:
            yield
        finally:
            signal.signal(signal.SIGINT, previous)


def pace(seconds: float, stop_controller: StopController | None = None) -> None:
    """Sleep ``seconds`` in short slices, returning early once a stop is wanted."""
    deadline = time.monotonic() + seconds
    while True:
        if stop_controller is not None and stop_controller.wants_stop():
            return
        left = deadline - time.monotonic()
        if left <= 0:
            return
        time.sleep(min(PACE_SLICE_S, left))


class Throttle:
    """Leaky bucket over network bytes for an operator's rate cap.

    Every chunk the transport hands over is accepted — it is already in
    memory — and the caller sleeps off the debt it adds, so the running
    average never exceeds the cap while TCP flow control slows the sender
    upstream. Debt drains at the cap and never turns into credit: a cold
    start, a hashing pause, or a reconnect earns no burst.
    """

    def __init__(self, bytes_per_second: int, clock=time.monotonic):
        self.rate = float(bytes_per_second)
        self._debt = 0.0
        self._clock = clock
        self._at = clock()
        self._lock = threading.Lock()

    def debit(self, amount: int) -> float:
        """Charge ``amount`` bytes; return how long the caller should sleep."""
        with self._lock:
            now = self._clock()
            drained = (now - self._at) * self.rate
            self._debt = max(0.0, self._debt - drained) + max(0, int(amount))
            self._at = now
            return self._debt / self.rate


class Link:
    """What every worker knows about the network: down since when, who is
    waiting, when the next attempt is due, how many attempts so far.

    Workers report ``lost`` and ``retrying``; the first network byte that
    arrives afterwards reports ``online``. Only an attempt begun during the
    outage can prove the link — bytes still draining from a stream that was
    open before the drop cannot — so ``online`` counts only while some path
    is ``retrying``. The meter renders ``snapshot``; the display announces
    each transition once via ``transitions``.
    """

    def __init__(self, patience_s: float, session: dict, clock=time.monotonic):
        self.patience = max(0.0, float(patience_s))
        self.session = session
        self._clock = clock
        self._lock = threading.Lock()
        self.offline_since: float | None = None
        self.reason: str | None = None
        self.attempts = 0
        self._waiting: dict[str, float] = {}
        self._retrying: set[str] = set()
        self.serial = 0
        self.transitions: list[tuple[int, str, dict]] = []

    @property
    def offline(self) -> bool:
        return self.offline_since is not None

    def offline_for(self) -> float:
        since = self.offline_since
        return 0.0 if since is None else max(0.0, self._clock() - since)

    def lost(self, path: str, reason: str, attempt: int) -> float:
        """Record a failed attempt for ``path``; return when to try again."""
        with self._lock:
            now = self._clock()
            if self.offline_since is None:
                self.offline_since = now
                self.attempts = 0
                self.serial += 1
                self.transitions.append((self.serial, "lost", {"reason": reason}))
            self.reason = reason
            self.attempts += 1
            wait = RECONNECT_WAITS_S[min(attempt, len(RECONNECT_WAITS_S) - 1)]
            retry_at = now + wait
            self._waiting[path] = retry_at
            self._retrying.discard(path)
            return retry_at

    def retrying(self, path: str) -> None:
        """An attempt for ``path`` is in flight; its bytes will prove the link."""
        with self._lock:
            self._waiting.pop(path, None)
            self._retrying.add(path)

    def online(self) -> None:
        """Bytes arrived: the network is back, if an attempt was in flight."""
        if self.offline_since is None:
            return
        with self._lock:
            since = self.offline_since
            if since is None or not self._retrying:
                return
            seconds = max(0.0, self._clock() - since)
            self.serial += 1
            self.transitions.append(
                (
                    self.serial,
                    "restored",
                    {"seconds": seconds, "attempts": self.attempts},
                )
            )
            self.session["reconnects"] += 1
            self.offline_since = None
            self.reason = None
            self.attempts = 0
            self._waiting.clear()
            self._retrying.clear()

    def snapshot(self) -> dict | None:
        with self._lock:
            since = self.offline_since
            if since is None:
                return None
            now = self._clock()
            retry_in = None
            if self._waiting and not self._retrying:
                retry_in = max(0.0, min(self._waiting.values()) - now)
            return {
                "state": "reconnecting" if self._retrying else "offline",
                "since": max(0.0, now - since),
                "retry_in": retry_in,
                "attempts": self.attempts,
                "reason": self.reason,
            }

    def transitions_after(self, serial: int) -> list[tuple[int, str, dict]]:
        with self._lock:
            return [item for item in self.transitions if item[0] > serial]


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
    required = (
        "repo_id",
        "repo_type",
        "revision",
        "revision_ref",
        "expected",
        "metadata",
    )
    missing = [key for key in required if key not in ledger]
    if missing:
        raise LedgerError(
            f"incomplete transfer ledger {path}: missing {', '.join(missing)}"
        )
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


# How the panel's closing record line names a clean stop, by reason.
_STOP_VERDICTS = {
    "disk": "paused: disk",
    "budget": "paused: budget",
    "offline": "paused: offline",
    "moved": "paused: moved",
    "interrupt": "stopped: Ctrl-C",
}


@contextmanager
def _live_transfer(display):
    """Run the live panel and route log lines through it.

    The panel's closing record line says how the transfer ended —
    complete, paused and why, aborted, or the error's class — so the
    scrollback tells the whole story without the lines that follow it.
    """
    display.start()
    verdict = "complete"
    try:
        yield display.echo
    except CleanStop as stop:
        verdict = _STOP_VERDICTS.get(stop.reason, f"paused: {stop.reason}")
        raise
    except KeyboardInterrupt:
        verdict = "aborted"
        raise
    except BaseException as exc:
        verdict = f"error: {type(exc).__name__}"
        raise
    finally:
        display.stop(verdict=verdict)


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
                with suppress(FileNotFoundError):
                    path.unlink()
                continue
            raise SystemExit(
                f"error: archive already in progress for {bundle_dir} "
                f"(pid {owner.get('pid', '?')} on {owner.get('host', '?')}, "
                f"started {owner.get('started', '?')})"
            ) from None
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
            if (
                ledger["repo_id"] != source.locator
                or ledger["repo_type"] != source.artifact_type
            ):
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
            "specify a revision or resolve them manually: "
            + ", ".join(map(str, orphans))
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
        expected.append(
            {
                "path": spec.path,
                "size": spec.size,
                "lfs_sha256": spec.sha256,
                "git_sha1": spec.git_sha1,
            }
        )
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


def begin_session(
    bundle_dir: Path, ledger: dict, shard: tuple[int, int] | None = None
) -> dict:
    session = {
        "started": _utc_now(),
        "ended": None,
        "end_reason": None,
        # The build that ran this session, so a ledger or a pasted terminal
        # never has to be matched to a release by line numbers.
        "tool": f"darsay {__version__}",
        "bytes_network": 0,
        "bytes_adopted": 0,
        "bytes_local_sources": 0,
        "files_completed": 0,
        "retries": 0,
        "reconnects": 0,
        "host": socket.gethostname(),
    }
    if shard is not None:
        session["shard"] = f"{shard[0]}/{shard[1]}"
    ledger["sessions"].append(session)
    save_ledger(bundle_dir, ledger)
    return session


def finish_session(bundle_dir: Path, ledger: dict, session: dict, reason: str) -> bool:
    """Close the session record; return whether the ledger was written.

    A ``disk`` pause must not become an error because its own bookkeeping
    hit the full disk: that one write failure is tolerated (``False``), and
    the next run re-derives the record from the payload.
    """
    session["ended"] = _utc_now()
    session["end_reason"] = reason
    try:
        save_ledger(bundle_dir, ledger)
    except OSError as exc:
        if reason != "disk" or not _disk_full(exc):
            raise
        return False
    return True


def record_event(ledger: dict, path: str | None, event: str, detail: str) -> None:
    ledger["events"].append(
        {"at": _utc_now(), "path": path, "event": event, "detail": detail}
    )


def _payload_path(payload_dir: Path, relative: str) -> Path:
    rel = PurePosixPath(relative)
    if (
        rel.is_absolute()
        or not rel.parts
        or any(part in ("", ".", "..") for part in rel.parts)
    ):
        raise SystemExit(f"error: unsafe path in pinned Hub inventory: {relative!r}")
    return payload_dir.joinpath(*rel.parts)


def _digest_matches(expected: dict, hashes: dict) -> bool | None:
    if expected.get("lfs_sha256"):
        return hashes["sha256"] == expected["lfs_sha256"]
    if expected.get("git_sha1"):
        return hashes["git_sha1"] == expected["git_sha1"]
    return None


def _verified_record(
    expected: dict,
    path: Path,
    source: str,
    attempts: int,
    interrupt_check=None,
    on_bytes=None,
) -> dict:
    hashes = hash_file(
        path,
        with_git_sha1=True,
        interrupt_check=interrupt_check,
        on_bytes=on_bytes,
    )
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


def _partial_bytes(
    payload_dir: Path, expected: dict, ledger: dict | None = None
) -> int:
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
    stop: StopController | None = None,
    meter=None,
) -> dict:
    """Reconcile ledger acceleration state with authoritative payload bytes.

    ``meter`` is an optional live ``TransferMeter`` for a hashing pass — assemble
    uses it so a dest that already holds the bytes does not look hung.
    """
    payload_dir.mkdir(parents=True, exist_ok=True)
    expected_by_path = {item["path"]: item for item in ledger["expected"]}
    present_by_path = dict(iter_payload_files(payload_dir))
    interrupt_check = (lambda: stop.check(session)) if stop is not None else None

    def hashed_record(expected, path, source, attempts):
        relative = expected["path"]
        size = path.stat().st_size if path.is_file() else 0
        on_bytes = None
        if meter is not None:
            meter.begin_hash(relative, size)

            def on_bytes(n, total, *, _path=relative):
                meter.note_hash_bytes(_path, n, total)

        try:
            return _verified_record(
                expected,
                path,
                source,
                attempts,
                interrupt_check=interrupt_check,
                on_bytes=on_bytes,
            )
        finally:
            if meter is not None:
                meter.finish_hash(relative, size)

    for relative in sorted(set(present_by_path) - set(expected_by_path)):
        path = present_by_path[relative]
        record_event(
            ledger, relative, "unexpected_file", "removed; not in pinned transfer set"
        )
        if apply:
            _discard_payload_file(path, payload_dir)
            save_ledger(bundle_dir, ledger)

    adopted_files = 0
    adopted_bytes = 0
    for relative, expected in sorted(expected_by_path.items()):
        if interrupt_check is not None:
            interrupt_check()
        path = _payload_path(payload_dir, relative)
        state = ledger["files"].get(relative) or {}
        expected_size = expected.get("size")
        size_matches = path.is_file() and (
            expected_size is None or path.stat().st_size == expected_size
        )

        # A moved file whose bytes are absent is an *intended* absence: the
        # verified bytes live in another vault. Trust the record and skip it
        # (never demote to missing, never re-fetch). If the bytes have come
        # back — copied by hand, restored from the other vault — the record
        # is only a hint: fall through and the present-file adoption path
        # below re-hashes them and promotes them to ``verified``. Bytes that
        # fail those checks are removed while the moved record is kept.
        if state.get("status") == "moved" and not path.is_file():
            continue

        if state.get("status") == "verified" and size_matches:
            if not rehash:
                continue
            record = hashed_record(
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
            record_event(
                ledger, relative, "verified_file_missing", "demoted to missing"
            )
        elif path.is_file() and not size_matches:
            actual_size = path.stat().st_size
            outcome = (
                "removed; moved record kept"
                if state.get("status") == "moved"
                else "removed and demoted"
            )
            record_event(
                ledger,
                relative,
                "size_mismatch",
                f"expected {expected_size}, found {actual_size}; {outcome}",
            )
            if apply:
                _discard_payload_file(path, payload_dir)
        elif path.is_file():
            record = hashed_record(
                expected,
                path,
                "adopted",
                int(state.get("attempts") or 0),
            )
            if record["verified_against_upstream"] is not False:
                ledger["files"][relative] = record
                adopted_files += 1
                adopted_bytes += record["size"]
                session["files_completed"] += 1
                session["bytes_adopted"] += record["size"]
                if apply:
                    save_ledger(bundle_dir, ledger)
                continue
            record_event(
                ledger,
                relative,
                "digest_mismatch",
                "local bytes did not match upstream; removed",
            )
            if apply:
                _discard_payload_file(path, payload_dir)

        if state.get("status") == "moved":
            # The bytes that came back failed the pin's checks and were
            # removed above, but that does not unsay the move: the verified
            # copy still lives in the other vault. Keep the record so
            # nothing re-fetches what that vault already holds.
            if apply:
                save_ledger(bundle_dir, ledger)
            continue

        ledger["files"][relative] = {
            "status": "missing",
            "attempts": int(state.get("attempts") or 0),
        }
        if apply:
            save_ledger(bundle_dir, ledger)

    if adopted_files:
        progress(
            f"Adopted {adopted_files} existing files ({adopted_bytes} bytes) after hashing"
        )
    return transfer_plan(payload_dir, ledger)


def transfer_plan(payload_dir: Path, ledger: dict) -> dict:
    counts = {"verified": 0, "moved": 0, "partial": 0, "missing": 0}
    bytes_by_state = {"verified": 0, "moved": 0, "partial": 0, "missing": 0}
    total = 0
    for expected in ledger["expected"]:
        size = expected.get("size") or 0
        total += size
        state = ledger["files"].get(expected["path"]) or {}
        status = state.get("status")
        if status == "verified":
            counts["verified"] += 1
            bytes_by_state["verified"] += size
            continue
        # A moved file is verified, its bytes handed to another vault. There
        # is nothing here to fetch and nothing here to register: it counts
        # toward neither remaining network nor completeness of this bundle.
        if status == "moved":
            counts["moved"] += 1
            bytes_by_state["moved"] += size
            continue
        partial = (
            min(_partial_bytes(payload_dir, expected, ledger), size) if size else 0
        )
        if partial:
            counts["partial"] += 1
            bytes_by_state["partial"] += partial
        else:
            counts["missing"] += 1
            bytes_by_state["missing"] += size
    remaining = max(
        0,
        total
        - bytes_by_state["verified"]
        - bytes_by_state["moved"]
        - bytes_by_state["partial"],
    )
    total_files = len(ledger["expected"])
    return {
        "files": {**counts, "total": total_files},
        "bytes": {**bytes_by_state, "total": total, "remaining_network": remaining},
        # ``complete`` = every file verified *here* (ready to register).
        "complete": counts["verified"] == total_files,
        # ``fetched`` = nothing left to fetch here: every file is verified
        # here or verified in another vault (a fully-drained skeleton).
        "fetched": counts["verified"] + counts["moved"] == total_files,
    }


def disk_verdict(free: int, needed: int, min_free: int | None = None) -> str:
    """Headroom verdict for ``needed`` more bytes, keeping the floor free."""
    usable = free - (min_free or 0)
    if usable >= needed * 1.1:
        return "ok"
    if usable >= needed:
        return "tight"
    return "insufficient"


def add_disk_preflight(
    bundle_dir: Path, plan: dict, min_free: int | None = None
) -> dict:
    """Attach estimate-style free-space headroom to a transfer plan."""
    probe = bundle_dir.resolve()
    while not probe.exists():
        probe = probe.parent
    free = shutil.disk_usage(probe).free
    needed = plan["bytes"]["remaining_network"]
    plan["disk"] = {
        "checked_path": str(probe),
        "free_bytes": free,
        "needed_bytes": needed,
        "min_free_bytes": min_free,
        "verdict": disk_verdict(free, needed, min_free),
    }
    return plan


def print_plan(plan: dict, progress=print, max_rate: int | None = None) -> None:
    from .progress import human_duration, human_rate
    from .readme_gen import human_size

    files = plan["files"]
    sizes = plan["bytes"]
    progress("Transfer plan:")
    progress(
        f"  verified: {files['verified']}/{files['total']} files, "
        f"{human_size(sizes['verified'])}"
    )
    if files.get("moved"):
        progress(
            f"  moved:    {files['moved']} files, {human_size(sizes['moved'])} — "
            "verified in another vault (assemble to register)"
        )
    progress(
        f"  partial:  {files['partial']} files, {human_size(sizes['partial'])} banked"
    )
    progress(
        f"  missing:  {files['missing']} files; estimated network remaining "
        f"{human_size(sizes['remaining_network'])}"
    )
    disk = plan["disk"]
    floor = disk.get("min_free_bytes")
    floor_note = f" ({human_size(floor)} floor)" if floor else ""
    progress(
        f"  disk:     needs {human_size(disk['needed_bytes'])}, "
        f"free {human_size(disk['free_bytes'])}{floor_note} at {disk['checked_path']} — "
        f"{disk['verdict'].upper()}"
    )
    if max_rate:
        remaining = sizes["remaining_network"]
        at_cap = (
            f" — about {human_duration(remaining / max_rate)} for the remaining "
            f"{human_size(remaining)}"
            if remaining
            else ""
        )
        progress(f"  rate:     capped at {human_rate(max_rate)}{at_cap}")


# A session shorter than this says nothing reliable about the link.
_RATE_SAMPLE_MIN_S = 60.0


def session_rate(ledger: dict) -> float | None:
    """Wall-clock network rate over the ledger's finished sessions, bytes/s.

    Sessions include their own hashing, waiting, and reconnects, so this is
    the honest pace an operator saw, not a link speed. ``None`` until at
    least a minute of moving bytes is on record.
    """
    from datetime import datetime

    moved = 0
    seconds = 0.0
    for session in ledger.get("sessions") or []:
        received = int(session.get("bytes_network") or 0)
        started, ended = session.get("started"), session.get("ended")
        if received <= 0 or not started or not ended:
            continue
        try:
            span = (
                datetime.fromisoformat(ended) - datetime.fromisoformat(started)
            ).total_seconds()
        except (TypeError, ValueError):
            continue
        if span <= 0:
            continue
        moved += received
        seconds += span
    if seconds < _RATE_SAMPLE_MIN_S or moved <= 0:
        return None
    return moved / seconds


def disk_outlook(
    plan: dict, ledger: dict, payload_dir: Path, max_rate: int | None = None
) -> list[str]:
    """The plan block's warning for a transfer the disk cannot hold.

    Says where it will pause — how many more bytes and files land before
    the floor, and roughly how long that takes when a rate cap or an
    earlier session gives a pace — so an operator can decide now rather
    than ten hours in.
    """
    from .progress import human_duration, human_rate
    from .readme_gen import human_size

    disk = plan["disk"]
    floor = int(disk.get("min_free_bytes") or 0)
    usable = max(0, int(disk["free_bytes"]) - floor)
    pending = int(plan["files"]["partial"]) + int(plan["files"]["missing"])
    fits = 0
    budget = usable
    # The transfer order, smallest first: once a file does not fit, nothing
    # after it does either.
    for expected in transfer_groups(ledger["expected"])[0][1]:
        state = ledger["files"].get(expected["path"]) or {}
        if state.get("status") == "verified":
            continue
        size = int(expected.get("size") or 0)
        need = max(0, size - _partial_bytes(payload_dir, expected, ledger))
        if need > budget:
            break
        budget -= need
        fits += 1
    when = ""
    paces = [pace for pace in (max_rate, session_rate(ledger)) if pace]
    if paces and usable:
        pace = min(paces)
        when = f", roughly {human_duration(usable / pace)} at {human_rate(pace)}"
    where = "at the free-space floor" if floor else "when the disk fills"
    lines = [
        f"WARNING: disk preflight is insufficient; the transfer will pause {where}",
        f"  after about {human_size(usable)} more "
        f"({fits} of {pending} remaining files){when}.",
    ]
    if fits < pending:
        lines.append(
            "  Free space (or move the vault to a larger disk), then re-run "
            "to continue."
        )
    return lines


def transfer_groups(
    expected: list[dict],
    shard: tuple[int, int] | None = None,
) -> list[tuple[int | None, list[dict]]]:
    """Return deterministic byte-balanced lane groups in participant order."""
    if shard is None:
        return [
            (
                None,
                sorted(
                    expected, key=lambda item: (item.get("size") or 0, item["path"])
                ),
            )
        ]

    participant, total_lanes = shard
    lanes: list[list[dict]] = [[] for _ in range(total_lanes)]
    lane_bytes = [0] * total_lanes
    # Longest-processing-time balancing is deterministic and keeps typical
    # equal-sized weight shards evenly distributed by bytes, not file count.
    for item in sorted(
        expected, key=lambda value: (-(value.get("size") or 0), value["path"])
    ):
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
    order = " -> ".join(
        str(number + 1) for number, _items in groups if number is not None
    )
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
    lane_sizes = [
        sum(item.get("size") or 0 for item in items) for _lane, items in groups
    ]
    ideal = total_bytes / shard[1] if shard[1] else 0
    if ideal and max(lane_sizes) > ideal * 1.5:
        progress(
            f"  WARNING: whole-file lane sizes are uneven "
            f"({human_size(min(lane_sizes))}–{human_size(max(lane_sizes))}); "
            "a monolithic file limits cooperative coverage"
        )


class NetworkCounter:
    """Receive actual network-byte callbacks from a provider's progress wrapper.

    This is the one place every received chunk passes through, so it also
    carries the rate cap (``throttle``) and tells the ``link`` when bytes
    prove the network is back.
    """

    def __init__(
        self,
        session: dict,
        stop_controller: StopController | None = None,
        *,
        link: Link | None = None,
        throttle: Throttle | None = None,
    ):
        self.session = session
        self.stop_controller = stop_controller
        self.link = link
        self.throttle = throttle
        self.lock = threading.Lock()
        self.pending_stop: CleanStop | None = None

    def add(self, amount: int, defer_only: bool = False) -> None:
        amount = max(0, int(amount))
        wait = 0.0
        with self.lock:
            self.session["bytes_network"] += amount
            if self.throttle is not None and amount:
                wait = self.throttle.debit(amount)
            # Providers typically report a chunk immediately before writing
            # it. Defer the first stop until the following callback so the
            # triggering chunk is durably banked in the incomplete file.
            if self.pending_stop is not None:
                if not defer_only:
                    raise self.pending_stop
                return
            if self.stop_controller is not None:
                try:
                    self.stop_controller.check(self.session)
                except CleanStop as stop:
                    self.pending_stop = stop
                    return
        if self.link is not None and amount:
            self.link.online()
        if wait > 0:
            pace(wait, self.stop_controller)

    def poll(self) -> None:
        """Raise a stop banked by an earlier callback (its bytes are durable)."""
        stop = self.pending_stop
        if stop is not None:
            raise stop

    def halt(self, stop: CleanStop) -> None:
        """Stop every in-flight transfer at its next chunk; bytes stay banked.

        For a failure elsewhere in the session: the other streams must not
        run a multi-gigabyte file to the end for a run that is already lost.
        """
        with self.lock:
            if self.pending_stop is None:
                self.pending_stop = stop


def _event(path: str | None, event: str, detail: str) -> dict:
    return {"at": _utc_now(), "path": path, "event": event, "detail": detail}


def _disk_full(exc: BaseException) -> bool:
    """Whether ``exc``, or anything it was raised from, is ENOSPC."""
    from .providers.base import iter_causes

    return any(
        isinstance(node, OSError) and node.errno == errno.ENOSPC
        for node in iter_causes(exc)
    )


def _disk_stop(stop_controller: StopController | None, detail: str) -> CleanStop:
    """The clean pause for ENOSPC — the same ``disk`` stop the floor raises.

    A full disk is a pause, not an error: the bytes on disk are intact and
    the same command resumes them. Tripping the controller stops the other
    workers too, since none of them can write either.
    """
    if stop_controller is not None:
        return stop_controller.stop_for_disk(detail)
    return CleanStop("disk", detail)


def _full_while_writing(relative: str) -> str:
    return f"destination is full — no space left on device while writing {relative}"


def _wait_for_link(
    link: Link,
    path: str,
    reason: str,
    attempt: int,
    stop_controller: StopController | None,
    session: dict,
) -> None:
    """Sleep out one reconnect interval, or raise the clean ``offline`` stop.

    Returns early once another worker's bytes prove the link is back.
    Budgets, the floor, and Ctrl-C keep their meaning while waiting: the
    stop controller is checked every slice, so a wait never outlives them.
    """
    from .progress import human_duration

    if link.patience <= 0:
        raise CleanStop("offline", f"network unreachable ({reason})")
    retry_at = link.lost(path, reason, attempt)
    while link.offline:
        if stop_controller is not None:
            stop_controller.check(session)
        offline_for = link.offline_for()
        if offline_for >= link.patience:
            raise CleanStop(
                "offline",
                f"network unreachable for {human_duration(offline_for)} ({reason})",
            )
        left = retry_at - time.monotonic()
        if left <= 0:
            return
        time.sleep(min(PACE_SLICE_S, left))


def _fetch_with_reconnect(
    provider,
    source,
    ledger: dict,
    relative: str,
    payload_dir: Path,
    *,
    force: bool,
    tqdm_class,
    counter: NetworkCounter,
    events: list[dict],
) -> None:
    """One ``download_file`` that outlives a network outage.

    A transient failure banks whatever arrived (the provider's partial is
    durable), waits for the link, and calls again — each retry resumes the
    partial rather than discarding it. Anything that is not the network
    propagates unchanged.
    """
    link = counter.link
    attempt = 0
    while True:
        if link is not None and link.offline:
            # Every attempt begun during the outage — a retry, or a fresh
            # file — is one whose first byte proves the link.
            link.retrying(relative)
        try:
            provider.download_file(
                source,
                ledger["revision"],
                relative,
                payload_dir,
                force=force,
                tqdm_class=tqdm_class,
            )
        except BaseException as exc:
            if counter.pending_stop is not None:
                raise counter.pending_stop from None
            if _disk_full(exc):
                raise _disk_stop(
                    counter.stop_controller, _full_while_writing(relative)
                ) from exc
            reason = None
            if link is not None and isinstance(exc, Exception):
                reason = provider.transient_network_error(exc)
            if reason is None:
                raise
            if attempt == 0:
                events.append(
                    _event(relative, "network_lost", f"{reason}; waiting to reconnect")
                )
            _wait_for_link(
                link,
                relative,
                reason,
                attempt,
                counter.stop_controller,
                counter.session,
            )
            attempt += 1
            force = False
            continue
        if link is not None:
            link.online()
        if attempt:
            events.append(
                _event(
                    relative,
                    "network_restored",
                    f"resumed after {attempt} reconnect attempt{'s' if attempt != 1 else ''}",
                )
            )
        return


class _FileJob:
    """One pinned file on its way through fetch → hash → commit.

    ``fetch`` and ``hash`` run on worker threads and never touch the ledger.
    ``settle`` runs wherever results are collected and says whether the
    record is final or the file goes around again: a sibling-bundle copy
    that failed re-verification falls back to the next candidate or the
    network; a first network fetch that missed the pinned digest is
    discarded and fetched once more with ``force``; a second miss is kept
    and recorded as an upstream verification failure.
    """

    def __init__(
        self,
        expected: dict,
        payload_dir: Path,
        ledger: dict,
        local_sources: dict[str, list[dict]],
    ):
        self.expected = expected
        self.relative: str = expected["path"]
        self.size = expected.get("size")
        self.payload_dir = payload_dir
        self.path = _payload_path(payload_dir, self.relative)
        previous = ledger["files"].get(self.relative) or {}
        self.attempts = int(previous.get("attempts") or 0)
        self.events: list[dict] = []
        self.retries = 0
        digest = expected.get("lfs_sha256")
        self.candidates: list[dict] = (
            list(local_sources.get(digest, [])) if digest else []
        )
        self.candidate: dict | None = None
        self.network_attempts = 0
        self.source: str | None = None
        self.copy_method: str | None = None
        self.record: dict | None = None
        self.bytes_local_sources = 0
        self.started = False

    def remaining_bytes(self, provider) -> int:
        """Bytes this file still needs on disk, for the floor guard."""
        size = int(self.size or 0)
        if not size or self.path.is_file():
            return 0
        return max(0, size - provider.partial_bytes(self.payload_dir, self.expected))

    def fetch(
        self,
        provider,
        source,
        ledger: dict,
        counter: NetworkCounter,
        stop_controller: StopController | None,
        meter=None,
        *,
        check_headroom: bool = True,
    ) -> None:
        """Bring the next source's bytes to ``self.path`` (worker thread).

        Sibling-bundle copies come first; the network is the last resort.
        ``check_headroom`` guards the first network fetch against the floor
        here; a pipeline that already checked the aggregate passes False.
        """
        self.started = True
        if stop_controller is not None:
            stop_controller.check(counter.session)
        if meter is not None:
            meter.set_current(self.relative, self.size, phase="download")
        while self.candidates:
            candidate = self.candidates.pop(0)
            self.attempts += 1
            try:
                if meter is not None:
                    meter.begin_hash(self.relative, self.size)
                self.copy_method = _copy_local_file(candidate["path"], self.path)
            except OSError as exc:
                _discard_payload_file(self.path, self.payload_dir)
                if _disk_full(exc):
                    raise _disk_stop(
                        stop_controller, _full_while_writing(self.relative)
                    ) from exc
                self.events.append(
                    _event(
                        self.relative,
                        "local_source_error",
                        f"{candidate['bundle_id']} could not be copied: {exc}",
                    )
                )
                continue
            self.candidate = candidate
            self.source = f"local:{candidate['bundle_id']}"
            return

        if (
            check_headroom
            and stop_controller is not None
            and self.network_attempts == 0
            and self.size
        ):
            # Pause at the boundary rather than start a file that cannot
            # finish above the floor; a banked partial only needs the rest.
            banked = provider.partial_bytes(self.payload_dir, self.expected)
            stop_controller.check_headroom(self.relative, int(self.size) - banked)
        self.network_attempts += 1
        self.attempts += 1
        if meter is not None:
            meter.set_current(self.relative, self.size, phase="download")
        _fetch_with_reconnect(
            provider,
            source,
            ledger,
            self.relative,
            self.payload_dir,
            force=self.network_attempts > 1,
            tqdm_class=provider.progress_wrapper(counter, meter=meter),
            counter=counter,
            events=self.events,
        )
        self.candidate = None
        self.source = "network"
        self.copy_method = None

    def hash(self, stop_controller: StopController | None, meter=None) -> None:
        """Digest the bytes at ``self.path`` (hash thread); never the ledger.

        Only Ctrl-C abandons a running digest; budgets and the floor are
        about network and disk, and this file's share of both is spent. An
        abandoned file stays on disk for the next run's reconcile to adopt.
        """
        assert self.source is not None
        if meter is not None:
            meter.begin_hash(self.relative, self.size)
        interrupt_check = (
            stop_controller.check_interrupt if stop_controller is not None else None
        )
        on_bytes = None
        if meter is not None:

            def on_bytes(n, total, *, _path=self.relative):
                meter.note_hash_bytes(_path, n, total, count=False)

        self.record = _verified_record(
            self.expected,
            self.path,
            self.source,
            self.attempts,
            interrupt_check=interrupt_check,
            on_bytes=on_bytes,
        )

    def settle(self) -> bool:
        """After a hash: True when the record is final, else it goes around."""
        record = self.record
        assert record is not None and self.source is not None
        if record["verified_against_upstream"] is not False:
            if self.candidate is not None:
                record["local_copy_method"] = self.copy_method
                self.bytes_local_sources = record["size"]
            return True
        if self.candidate is not None:
            self.events.append(
                _event(
                    self.relative,
                    "local_source_mismatch",
                    f"{self.candidate['bundle_id']} failed re-verification; falling back",
                )
            )
            _discard_payload_file(self.path, self.payload_dir)
            self.record = None
            return False
        self.events.append(
            _event(
                self.relative,
                "digest_mismatch",
                f"download attempt {self.attempts} did not match pinned upstream digest",
            )
        )
        if self.network_attempts == 1:
            self.retries += 1
            _discard_payload_file(self.path, self.payload_dir)
            self.record = None
            return False
        self.events.append(
            _event(
                self.relative,
                "persistent_digest_mismatch",
                "second download mismatch; retained and marked as an upstream verification failure",
            )
        )
        return True

    def result(self) -> dict:
        """The commit payload for ``_record_download_result``."""
        assert self.record is not None
        return {
            "path": self.relative,
            "record": self.record,
            "events": self.events,
            "retries": self.retries,
            "bytes_local_sources": self.bytes_local_sources,
        }


def _download_one(
    expected: dict,
    payload_dir: Path,
    ledger: dict,
    counter: NetworkCounter,
    stop_controller: StopController | None,
    local_sources: dict[str, list[dict]],
    meter=None,
) -> dict:
    """Worker-safe fetch → hash → settle loop for one file; never writes the ledger."""
    from .sources import get_provider, source_from_ledger

    provider = get_provider(ledger.get("provider") or "huggingface")
    source = source_from_ledger(ledger)
    job = _FileJob(expected, payload_dir, ledger, local_sources)
    try:
        while True:
            job.fetch(provider, source, ledger, counter, stop_controller, meter)
            job.hash(stop_controller, meter)
            if job.settle():
                return job.result()
    finally:
        if meter is not None:
            meter.clear_current(job.relative)


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
                index.setdefault(digest, []).append(
                    {
                        "bundle_id": bundle_id,
                        "path": source,
                    }
                )
        except (json.JSONDecodeError, KeyError, OSError, TypeError):
            continue
    return index


def _record_download_result(
    bundle_dir: Path,
    ledger: dict,
    session: dict,
    result: dict,
    stop_controller: StopController | None = None,
) -> None:
    """Main-thread commit point for a worker's completed file."""
    ledger["events"].extend(result["events"])
    ledger["files"][result["path"]] = result["record"]
    session["files_completed"] += 1
    session["retries"] += result["retries"]
    session["bytes_local_sources"] += result.get("bytes_local_sources", 0)
    try:
        save_ledger(bundle_dir, ledger)
    except OSError as exc:
        if not _disk_full(exc):
            raise
        # The verified file is on disk; reconciliation re-derives its entry.
        raise _disk_stop(
            stop_controller,
            "destination is full — the transfer ledger could not be written "
            f"after {result['path']}",
        ) from exc


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
    meter=None,
    live: bool = False,
) -> None:
    if not small:
        return
    progress(
        f"Transferring {len(small)} small files with {min(jobs, len(small))} workers "
        f"(< {SMALL_FILE_LIMIT} bytes each) ..."
    )
    clean_stop = None
    first_error = None
    cancelled = False
    executor = ThreadPoolExecutor(max_workers=jobs)
    try:
        futures = {
            executor.submit(
                _download_one,
                expected,
                payload_dir,
                ledger,
                counter,
                stop_controller,
                local_sources,
                meter,
            ): expected
            for expected in small
        }
        # Keep draining after a stop so results already earned by running
        # workers still land in the ledger; queued work is cancelled instead
        # of being started only to fail its own stop check.
        for future in as_completed(futures):
            expected = futures[future]
            try:
                result = future.result()
            except CancelledError:
                continue
            except CleanStop as stop:
                if clean_stop is None or stop.reason == "interrupt":
                    clean_stop = stop
            except KeyboardInterrupt:
                raise
            except BaseException as exc:
                if first_error is None:
                    first_error = exc
            else:
                source = result["record"]["source"]
                suffix = f" from {source}" if source.startswith("local:") else ""
                if not live:
                    progress(
                        f"Verified {expected['path']} ({result['record']['size']} bytes){suffix}"
                    )
                _record_download_result(
                    bundle_dir, ledger, session, result, stop_controller
                )
                if meter is not None:
                    meter.note()
                if stop_controller is not None:
                    try:
                        stop_controller.check(session)
                    except CleanStop as stop:
                        if clean_stop is None or stop.reason == "interrupt":
                            clean_stop = stop
            if (clean_stop is not None or first_error is not None) and not cancelled:
                cancelled = True
                for pending in futures:
                    pending.cancel()
    finally:
        executor.shutdown(wait=True, cancel_futures=True)
    if first_error is not None:
        raise first_error
    if clean_stop is not None:
        raise clean_stop


def _pipeline_headroom(
    job: _FileJob,
    in_flight: list[_FileJob],
    provider,
    stop_controller: StopController | None,
) -> None:
    """Refuse to start a fetch that cannot finish above the floor.

    With several streams running the floor must hold what *all* of them
    still need, not just this file: every in-flight fetch counts at the
    bytes it has yet to land. A sibling-bundle copy is not guarded, as
    before; if it fails and the file goes around to the network, it is.
    """
    if stop_controller is None or job.candidates or not job.size:
        return
    others = [other for other in in_flight if other is not job]
    needed = job.remaining_bytes(provider) + sum(
        other.remaining_bytes(provider) for other in others
    )
    label = job.relative
    if others:
        label = f"{job.relative} with {len(others)} in flight"
    stop_controller.check_headroom(label, needed)


def _transfer_large_files(
    large: list[dict],
    bundle_dir: Path,
    payload_dir: Path,
    ledger: dict,
    session: dict,
    counter: NetworkCounter,
    local_sources: dict[str, list[dict]],
    stop_controller: StopController | None,
    streams: int,
    progress,
    meter=None,
    live: bool = False,
) -> None:
    """Large files: ``streams`` fetch workers feeding one hash worker.

    A file's digest runs while the next file downloads, so the network
    never idles for verification (``hashlib`` releases the GIL; the two
    barely share a core). Workers never touch the ledger: every result is
    committed here, in the main thread, in the order digests finish.

    Stops: a fetch that raises a clean stop has banked its partial; a
    digest abandoned by Ctrl-C leaves a complete file that the next run's
    reconcile adopts after hashing; any other stop lets queued digests
    finish, since they cost no network and no disk. An error on one file
    halts the other streams at their next chunk, so a 5 GiB fetch does not
    run to the end for a session that is already lost.
    """
    if not large:
        return
    from .sources import get_provider, source_from_ledger

    provider = get_provider(ledger.get("provider") or "huggingface")
    source = source_from_ledger(ledger)
    streams = max(1, int(streams))
    width = min(streams, len(large))
    if not live:
        progress(
            f"Transferring {len(large)} large files with {width} "
            f"stream{'s' if width != 1 else ''}, hashing alongside "
            f"(>= {SMALL_FILE_LIMIT} bytes each) ..."
        )
    queue: deque[_FileJob] = deque(
        _FileJob(expected, payload_dir, ledger, local_sources) for expected in large
    )
    fetching: dict[Future, _FileJob] = {}
    hashing: dict[Future, _FileJob] = {}
    clean_stop: CleanStop | None = None
    first_error: BaseException | None = None
    begun = 0

    def note_stop(stop: CleanStop) -> None:
        nonlocal clean_stop
        if clean_stop is None or stop.reason == "interrupt":
            clean_stop = stop

    def fail(exc: BaseException, job: _FileJob) -> None:
        nonlocal first_error
        if first_error is None:
            first_error = exc
        counter.halt(CleanStop("error", f"{type(exc).__name__} on {job.relative}"))

    def halted() -> bool:
        return clean_stop is not None or first_error is not None

    def forget(job: _FileJob) -> None:
        if meter is not None:
            meter.clear_current(job.relative)

    def commit(job: _FileJob) -> None:
        result = job.result()
        forget(job)
        if not live:
            origin = result["record"]["source"]
            suffix = f" from {origin}" if origin.startswith("local:") else ""
            progress(
                f"Verified {job.relative} ({result['record']['size']} bytes){suffix}"
            )
        _record_download_result(bundle_dir, ledger, session, result, stop_controller)
        if meter is not None:
            meter.note()
        if stop_controller is not None:
            try:
                stop_controller.check(session)
            except CleanStop as stop:
                note_stop(stop)

    fetch_pool = ThreadPoolExecutor(
        max_workers=streams, thread_name_prefix="darsay-fetch"
    )
    hash_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="darsay-hash")

    def start_next_fetch() -> bool:
        """Begin the file at the head of the queue; False if it must wait."""
        nonlocal begun
        job = queue[0]
        try:
            _pipeline_headroom(job, list(fetching.values()), provider, stop_controller)
        except CleanStop as stop:
            note_stop(stop)
            return False
        queue.popleft()
        if not live and not job.started:
            begun += 1
            progress(
                f"Transferring large file {begun}/{len(large)}: "
                f"{job.relative} ({job.size or 0} bytes)"
            )
        future = fetch_pool.submit(
            job.fetch,
            provider,
            source,
            ledger,
            counter,
            stop_controller,
            meter,
            check_headroom=False,
        )
        fetching[future] = job
        return True

    def fetched(job: _FileJob, future: Future) -> None:
        """A fetch worker is done: queue the digest, or note why not."""
        try:
            future.result()
        except CancelledError:
            forget(job)
        except CleanStop as stop:
            forget(job)  # its partial is banked
            note_stop(stop)
        except KeyboardInterrupt:
            raise
        except BaseException as exc:
            forget(job)
            fail(exc, job)
        else:
            hashing[hash_pool.submit(job.hash, stop_controller, meter)] = job

    def hashed(job: _FileJob, future: Future) -> None:
        """The hash worker is done: commit, or send the file around again."""
        try:
            future.result()
        except CancelledError:
            forget(job)
        except CleanStop as stop:
            forget(job)  # Ctrl-C: the complete file stays for reconcile
            note_stop(stop)
        except KeyboardInterrupt:
            raise
        except BaseException as exc:
            forget(job)
            fail(exc, job)
        else:
            if job.settle():
                commit(job)
            else:
                # The next candidate, or the forced second fetch: ahead of
                # the rest, so the retry is not starved.
                queue.appendleft(job)

    try:
        while queue or fetching or hashing:
            # Keep ``streams`` fetches running while the hash thread keeps
            # up. If the disk cannot, fetching pauses rather than piling up
            # unverified files ahead of the ledger (and of Ctrl-C).
            while (
                queue
                and len(fetching) < streams
                and len(hashing) <= streams
                and not halted()
                and start_next_fetch()
            ):
                pass
            if not fetching and not hashing:
                break
            done, _ = wait(set(fetching) | set(hashing), return_when=FIRST_COMPLETED)
            for future in done:
                if future in fetching:
                    fetched(fetching.pop(future), future)
                else:
                    hashed(hashing.pop(future), future)
            if clean_stop is not None and clean_stop.reason == "interrupt":
                for pending in list(hashing):
                    pending.cancel()
    except BaseException as exc:
        # The main thread is leaving with the session lost — a second Ctrl-C,
        # a ledger write that failed: stop the streams at their next chunk
        # rather than let them run their files to the end.
        counter.halt(CleanStop("error", type(exc).__name__))
        raise
    finally:
        fetch_pool.shutdown(wait=True, cancel_futures=True)
        hash_pool.shutdown(wait=True, cancel_futures=True)
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
    max_rate: int | None = None,
    max_offline: float | None = None,
) -> dict:
    """Fetch and immediately verify every remaining file at the pinned commit.

    ``jobs`` is how many files are in flight at once: small files as pool
    workers, large files as concurrent streams beside one hash thread.
    ``max_rate`` caps network bytes per second across every stream;
    ``max_offline`` is how long a lost network is waited out before the
    session pauses cleanly.
    """
    from .config import DEFAULT_MAX_OFFLINE
    from .progress import TransferDisplay, meter_from_plan
    from .sources import get_provider

    throttle = Throttle(max_rate) if max_rate else None
    link = Link(DEFAULT_MAX_OFFLINE if max_offline is None else max_offline, session)
    counter = NetworkCounter(session, stop_controller, link=link, throttle=throttle)
    local_sources = local_source_index(bundle_dir, ledger)
    groups = transfer_groups(ledger["expected"], shard)
    provider = get_provider(ledger.get("provider") or "huggingface")
    plan = transfer_plan(payload_dir, ledger)
    meter = meter_from_plan(
        plan, session, stop_controller, link=link, max_rate=max_rate
    )
    display = TransferDisplay(meter, progress=progress)

    with (
        provider.transfer_session(
            payload_dir, max_rate=max_rate, on_retry=meter.note_retry
        ),
        _live_transfer(display) as emit,
    ):
        for lane, assigned in groups:
            remaining = [
                expected
                for expected in assigned
                if (ledger["files"].get(expected["path"]) or {}).get("status")
                not in ("verified", "moved")
            ]
            if not remaining:
                continue
            if lane is not None:
                emit(f"Cooperative lane {lane + 1}/{shard[1]} ...")
            small = [
                item for item in remaining if (item.get("size") or 0) < SMALL_FILE_LIMIT
            ]
            large = [
                item
                for item in remaining
                if (item.get("size") or 0) >= SMALL_FILE_LIMIT
            ]
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
                emit,
                meter=meter,
                live=display.live,
            )

            _transfer_large_files(
                large,
                bundle_dir,
                payload_dir,
                ledger,
                session,
                counter,
                local_sources,
                stop_controller,
                jobs,
                emit,
                meter=meter,
                live=display.live,
            )

        # Everything fetchable here is done, but some files live in another
        # vault (a skeleton). This is a clean stop, not a failure: raised
        # inside the live panel so its closing line reads ``paused: moved``,
        # and carried out through the same PartialTransfer path as a budget
        # or offline pause (exit 10 — assemble the halves to register).
        final = transfer_plan(payload_dir, ledger)
        if final["fetched"] and not final["complete"]:
            raise CleanStop(
                "moved",
                "every file is verified here or moved to another vault; "
                "assemble the halves into one vault to register",
            )

    return final


def _registered_pin_matches(destination: Path, seed: dict) -> bool:
    """True when a registered dest is the same pin as an incoming partial."""
    try:
        manifest = json.loads(
            (destination / "manifest.json").read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError, TypeError):
        return False
    source = manifest.get("source") or {}
    return source.get("revision") == seed.get("revision") and source.get(
        "repo_id"
    ) == seed.get("repo_id")


def _refuse_move_of_registered_sources(
    sources: list[tuple[Path, dict]], destination: Path
) -> None:
    """A registered payload is frozen; ``--move`` must not delete it.

    Relocate a finished bundle with rsync, then ``darsay rm`` the source.
    ``assemble --move`` is the partial/skeleton verb.
    """
    dest_real = destination.resolve()
    for source_dir, _ledger in sources:
        if source_dir.resolve() == dest_real:
            continue
        if (source_dir / "manifest.json").is_file():
            raise SystemExit(
                f"error: cannot --move a registered bundle: {source_dir}\n"
                "  a registered payload is frozen. rsync it into the other "
                "vault, then `darsay rm` the source if you want the space."
            )


def _same_transfer_set(left: dict, right: dict) -> bool:
    if not all(
        left.get(key) == right.get(key)
        for key in (
            "transfer_version",
            "repo_id",
            "repo_type",
            "revision",
            "expected",
        )
    ):
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
    *,
    apply: bool = True,
) -> tuple[int, int]:
    """Merge portable provider caches with one copy of each longest byte partial.

    ``apply=False`` only counts what would be copied.
    """
    destination_cache = destination_payload / ".cache"
    copied_files = 0
    copied_partial_bytes = 0
    candidates: dict[Path, Path] = {}
    for source_payload in source_payloads:
        source_cache = source_payload / ".cache"
        if not source_cache.is_dir():
            continue
        for source in sorted(source_cache.rglob("*")):
            if (
                not source.is_file()
                or source.is_symlink()
                or source.name.endswith(".lock")
            ):
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
            if apply:
                _copy_local_file(source, destination)
            copied_files += 1
            copied_partial_bytes += source_size
        elif not destination.exists():
            if apply:
                _copy_local_file(source, destination)
            copied_files += 1
    return copied_files, copied_partial_bytes


def _ledger_hosts(ledger: dict) -> list[str]:
    """Distinct hosts that ever ran a session against this ledger."""
    return sorted(
        {
            str(item.get("host"))
            for item in ledger.get("sessions", [])
            if item.get("host")
        }
    )


def release_moved(
    source_dir: Path,
    source_ledger: dict,
    destination_ledger: dict,
    root: str,
) -> tuple[int, int]:
    """Hand a source's verified bytes to the destination and skeletonize it.

    For each file the *destination* now holds as ``verified``, delete the
    matching bytes at the source and rewrite the source record as ``moved``
    (keeping every hash, adding ``moved_at``). This is ``mv`` with rsync's
    ``--remove-source-files`` guarantee: the gate is dest's own ``verified``
    record (ledger + size after an rsync, or its hash under ``--rehash``),
    so a file dest cannot vouch for keeps its source bytes.

    The source ledger is saved after every file, so a crash mid-loop leaves
    a consistent skeleton. Returns ``(moved_files, moved_bytes)``.
    """
    payload_dir = source_dir / root
    moved_files = 0
    moved_bytes = 0
    for expected in source_ledger["expected"]:
        relative = expected["path"]
        dest_state = destination_ledger["files"].get(relative) or {}
        src_state = source_ledger["files"].get(relative) or {}
        if dest_state.get("status") != "verified":
            continue
        if src_state.get("status") != "verified":
            continue
        path = _payload_path(payload_dir, relative)
        if not path.is_file():
            continue
        _discard_payload_file(path, payload_dir)
        source_ledger["files"][relative] = {
            **src_state,
            "status": "moved",
            "moved_at": _utc_now(),
        }
        moved_bytes += int(src_state.get("size") or expected.get("size") or 0)
        moved_files += 1
        save_ledger(source_dir, source_ledger)
    return moved_files, moved_bytes


def _release_sources(
    sources: list[tuple[Path, dict]],
    destination: Path,
    ledger: dict,
    root: str,
    progress,
) -> None:
    """Skeletonize each source once its bytes are verified in the destination.

    A source file is deleted only when the destination holds that same file
    as ``verified`` (see :func:`release_moved`), so an interrupted copy never
    costs the source its only copy. A source with every file moved out is
    dissolved (it holds no payload byte by construction); any other source
    is kept as a skeleton and what it still holds or owes is reported.
    """
    from .readme_gen import human_size

    progress("Releasing source payload files dest already holds as verified...")
    dest_real = destination.resolve()
    dest_vault = destination.parent.parent.name
    host = socket.gethostname()
    for source_dir, source_ledger in sources:
        if source_dir.resolve() == dest_real:
            # A source that *is* the destination (a re-run) has nothing to
            # hand to itself, and deleting here would delete the very bytes
            # the destination just verified.
            continue
        with transfer_lock(source_dir, progress=progress):
            moved_files, moved_bytes = release_moved(
                source_dir, source_ledger, ledger, root
            )
            if not moved_files:
                continue
            source_hosts = _ledger_hosts(source_ledger)
            source_host = ", ".join(source_hosts) if source_hosts else host
            record_event(
                ledger,
                None,
                "moved_in",
                f"received {moved_files} files ({moved_bytes} bytes) from a "
                f"skeleton on {source_host}",
            )
            remaining = transfer_plan(source_dir / root, source_ledger)
            if remaining["files"]["moved"] == remaining["files"]["total"]:
                # Fully drained: every expected file is moved, so the source
                # holds no payload byte by construction. ``fetched`` is not
                # enough here — it also counts files still verified only at
                # the source (a copy the destination failed to verify), and
                # those bytes must never be deleted.
                shutil.rmtree(source_dir, ignore_errors=True)
                progress(
                    f"Moved {moved_files} files ({human_size(moved_bytes)}) out of "
                    f"{source_dir}; nothing left to fetch there — skeleton removed."
                )
            else:
                record_event(
                    source_ledger,
                    None,
                    "moved_out",
                    f"handed {moved_files} files ({moved_bytes} bytes) to vault "
                    f"{dest_vault!r} on {host}",
                )
                save_ledger(source_dir, source_ledger)
                rem_files = (
                    remaining["files"]["partial"] + remaining["files"]["missing"]
                )
                if rem_files:
                    progress(
                        f"Moved {moved_files} files ({human_size(moved_bytes)}) out of "
                        f"{source_dir}; {rem_files} files "
                        f"({human_size(remaining['bytes']['remaining_network'])}) "
                        "remain to fetch there."
                    )
                else:
                    progress(
                        f"Moved {moved_files} files ({human_size(moved_bytes)}) out of "
                        f"{source_dir}; {remaining['files']['verified']} files the "
                        "destination could not verify stay put there."
                    )


# Filesystem types GNU ``df -l`` treats as remote even though their mount
# source has no ``host:`` or ``//host`` prefix (gnulib's ME_REMOTE).
_REMOTE_FS_TYPES = frozenset(
    {"acfs", "afs", "auristorfs", "coda", "fhgfs", "gpfs", "ibrix", "ocfs2", "vxfs"}
)
_MOUNTINFO_ESCAPES = {"\\040": " ", "\\011": "\t", "\\012": "\n", "\\134": "\\"}


def _parse_linux_mountinfo(text: str) -> list[tuple[str, bool]]:
    """``/proc/self/mountinfo`` → ``[(mount point, is_network), …]``.

    Remote is decided the way ``df -l`` decides it: an ``nfs``/``sshfs``/
    ``rclone`` source is ``host:path``, a ``cifs`` source is ``//host/share``,
    and a few cluster filesystems are remote by type.
    """
    mounts = []
    for line in text.splitlines():
        head, sep, tail = line.partition(" - ")
        fields = head.split()
        rest = tail.split()
        if not sep or len(fields) < 5 or len(rest) < 2:
            continue
        mount_point = fields[4]
        for escape, char in _MOUNTINFO_ESCAPES.items():
            mount_point = mount_point.replace(escape, char)
        fstype, source = rest[0], rest[1]
        network = ":" in source or source.startswith("//") or fstype in _REMOTE_FS_TYPES
        mounts.append((mount_point, network))
    return mounts


def _parse_darwin_mounts(text: str) -> list[tuple[str, bool]]:
    """``mount(8)`` output → ``[(mount point, is_network), …]``.

    Each line is ``<source> on <point> (<type>, <flag>, …)``; the kernel's
    ``MNT_LOCAL`` flag prints as ``local`` and is absent on ``smbfs``,
    ``nfs``, ``afpfs``, ``webdav``, and FUSE mounts.
    """
    mounts = []
    for line in text.splitlines():
        head, sep, flags = line.rpartition(" (")
        _source, on, mount_point = head.partition(" on ")
        if not sep or not on:
            continue
        mounts.append((mount_point, "local" not in flags.rstrip(")").split(", ")))
    return mounts


def _deepest_mount(target: str, mounts: list[tuple[str, bool]]) -> bool | None:
    """The ``is_network`` of the longest mount point containing ``target``."""
    best: tuple[int, bool] | None = None
    for mount_point, network in mounts:
        root = mount_point.rstrip("/")
        if root and target != root and not target.startswith(root + "/"):
            continue
        if best is None or len(root) > best[0]:
            best = (len(root), network)
    return None if best is None else best[1]


def is_network_filesystem(path: Path) -> bool | None:
    """Whether ``path`` lives on a network mount; ``None`` when unknowable.

    Reading a payload on such a mount to hash it is a second full transfer
    over the wire — the thing a follow-up to ``rsync`` must not do silently.
    """
    try:
        if sys.platform == "darwin":
            out = subprocess.run(
                ["mount"], check=True, capture_output=True, text=True
            ).stdout
            mounts = _parse_darwin_mounts(out)
        elif sys.platform.startswith("linux"):
            mounts = _parse_linux_mountinfo(
                Path("/proc/self/mountinfo").read_text(encoding="utf-8")
            )
        else:
            return None
    except (OSError, subprocess.CalledProcessError):
        return None
    return _deepest_mount(os.path.realpath(path), mounts)


def _files_to_hash(
    payload_dir: Path, ledger: dict, *, rehash: bool
) -> list[tuple[str, int]]:
    """Payload files this reconcile pass will actually hash."""
    expected = {item["path"] for item in ledger["expected"]}
    files = []
    for relative, path in iter_payload_files(payload_dir):
        if relative not in expected:
            continue
        state = ledger["files"].get(relative) or {}
        if state.get("status") == "verified" and not rehash:
            continue
        if state.get("status") == "moved" and not path.is_file():
            continue
        files.append((relative, path.stat().st_size))
    return files


def _reconcile_visible(
    destination: Path,
    destination_payload: Path,
    ledger: dict,
    session: dict,
    progress,
    *,
    apply: bool,
    rehash: bool,
) -> dict:
    """Reconcile dest with a live panel when dest already holds bytes to hash."""
    from .progress import TransferDisplay, TransferMeter
    from .readme_gen import human_size

    targets = _files_to_hash(destination_payload, ledger, rehash=rehash)
    total = sum(size for _path, size in targets)
    if not targets:
        return reconcile(
            destination,
            destination_payload,
            ledger,
            session,
            progress=progress,
            apply=apply,
            rehash=rehash,
        )

    hash_session = {
        "bytes_network": 0,
        "bytes_local_sources": 0,
        "files_completed": 0,
    }
    meter = TransferMeter(
        total_bytes=total,
        total_files=len(targets),
        verified_bytes=0,
        verified_files=0,
        partial_bytes=0,
        session=hash_session,
        disk_path=destination,
    )
    display = TransferDisplay(meter, progress=progress)
    with _live_transfer(display) as emit:
        emit(
            f"Hashing {len(targets)} files already at the destination "
            f"({human_size(total)}) against the pin — not downloading."
        )
        if is_network_filesystem(destination):
            emit(
                "warning: the destination is on a network mount, so that hashing "
                "reads every byte back over the wire. Run this where the vault "
                "is a local disk"
                + (
                    ", or omit --rehash to trust dest's ledger and file sizes."
                    if rehash
                    else "."
                )
            )
        return reconcile(
            destination,
            destination_payload,
            ledger,
            session,
            progress=emit,
            apply=apply,
            rehash=rehash,
            meter=meter,
        )


def assemble_partials(
    partials: list[Path],
    vault: Path,
    progress=print,
    *,
    move: bool = False,
    rehash: bool = False,
) -> tuple[Path, dict]:
    """Combine matching partial bundles offline into one resumable target.

    ``move=True`` hands each source's verified bytes to the destination and
    turns the source into a skeleton (see :func:`release_moved`): the pin and
    the recorded hashes stay behind, the payload bytes do not, so the source
    machine can go on to fetch the *other* half without re-downloading what
    it already handed over. A source left with nothing to fetch is removed.

    An out-of-band copy (``rsync``, ``cp -a``) of the partial into the
    destination is the same as this function's copy step: files dest already
    holds are not recopied. Dest files its ledger marks ``verified`` (size
    match) are trusted, not re-read — the gate ``archive`` uses after rsync;
    hashing dest over a network mount would pull the payload back over the
    wire. ``rehash=True`` hashes them anyway (run it where dest is a local
    disk); present files with no verified record are always hashed
    (adoption). ``move=True`` then skeletonizes the source for every file
    dest holds as ``verified``. A destination that is already *registered*
    is frozen — without ``--move`` that is an error; with ``--move`` dest is
    not rewritten and the source is skeletonized.
    """
    import copy

    from .archiver import bundle_dir_for
    from .config import free_space_floor
    from .readme_gen import human_size
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
    dest_registered = (destination / "manifest.json").is_file()

    if move:
        _refuse_move_of_registered_sources(sources, destination)

    trust_note = (
        "re-hashed dest against the pin"
        if rehash
        else "trusted dest ledger + size; --rehash re-hashes dest, best run where "
        "it is a local disk"
    )
    progress(f"Assembling into {destination}")
    with transfer_lock(destination, progress=progress):
        if dest_registered and not move:
            raise SystemExit(
                f"error: destination is already a registered bundle: {destination}"
            )
        if ledger_path(destination).is_file():
            ledger = load_ledger(destination)
            if not _same_transfer_set(seed, ledger):
                raise SystemExit(
                    f"error: destination partial has a different transfer set: {destination}"
                )
        elif dest_registered:
            if not _registered_pin_matches(destination, seed):
                raise SystemExit(
                    f"error: destination is a registered bundle of a different "
                    f"pin: {destination}"
                )
            ledger = copy.deepcopy(seed)
            ledger["files"] = {}
            ledger["sessions"] = []
            ledger["events"] = []
        else:
            ledger = copy.deepcopy(seed)
            ledger["files"] = {}
            ledger["sessions"] = []
            ledger["events"] = []
            save_ledger(destination, ledger)

        if dest_registered:
            # rsync (or archive) already produced the museum copy. Payload is
            # frozen: reconcile read-only, copy nothing, skeletonize sources.
            # Dest metadata is not rewritten.
            progress(
                "Destination is already registered; payload is frozen "
                "(no copy, no download)."
            )
            session = {"files_completed": 0, "bytes_adopted": 0, "bytes_network": 0}
            work = copy.deepcopy(ledger)
            _reconcile_visible(
                destination,
                destination_payload,
                work,
                session,
                progress,
                apply=False,
                rehash=rehash,
            )
            _release_sources(sources, destination, work, root, progress)
            plan = add_disk_preflight(
                destination,
                transfer_plan(destination_payload, work),
                min_free=free_space_floor(vault),
            )
            progress(
                f"Destination already registered; copied 0 payload files ({trust_note})"
            )
            print_plan(plan, progress=progress)
            return destination, plan

        session = begin_session(destination, ledger)
        session["assembly_sources"] = len(sources)
        session_finished = False
        copied_payload_files = 0
        copied_cache_files = 0
        copied_partial_bytes = 0
        try:
            # What dest already holds (an rsync/cp -a lands here): verified
            # ledger entries are trusted by size, unrecorded bytes are hashed,
            # --rehash hashes everything.
            plan = _reconcile_visible(
                destination,
                destination_payload,
                ledger,
                session,
                progress,
                apply=True,
                rehash=rehash,
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
                    progress(
                        f"Copying {relative} ({human_size(source_file.stat().st_size)})..."
                    )
                    _copy_local_file(source_file, destination_file)
                    copied_payload_files += 1

                hosts = _ledger_hosts(source_ledger)
                host_note = ", ".join(hosts) if hosts else "unknown host"
                record_event(
                    ledger,
                    None,
                    "assembled_partial",
                    f"merged cooperative input {ordinal}/{len(sources)} from {host_note}",
                )
                plan = _reconcile_visible(
                    destination,
                    destination_payload,
                    ledger,
                    session,
                    progress,
                    apply=True,
                    rehash=False,
                )

            copied_cache_files, copied_partial_bytes = _merge_transfer_caches(
                [source_dir / root for source_dir, _source_ledger in sources],
                destination_payload,
            )

            if move:
                _release_sources(sources, destination, ledger, root, progress)

            finish_session(destination, ledger, session, "assemble")
            session_finished = True
        except BaseException:
            if not session_finished:
                finish_session(destination, ledger, session, "error")
            raise

        plan = add_disk_preflight(
            destination,
            transfer_plan(destination_payload, ledger),
            min_free=free_space_floor(vault),
        )
        if copied_payload_files == 0 and copied_cache_files == 0:
            progress(
                f"Destination already held the payload; copied 0 files ({trust_note})"
            )
        else:
            progress(
                f"Assembled {copied_payload_files} payload files and "
                f"{copied_cache_files} cache files "
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
        records.append(
            {
                "path": f"{payload_root}/{expected['path']}",
                "size": state["size"],
                "sha256": state["sha256"],
                "blake3": state.get("blake3"),
                "upstream_lfs_sha256": expected.get("lfs_sha256"),
                "upstream_git_sha1": expected.get("git_sha1"),
                "verified_against_upstream": state.get("verified_against_upstream"),
            }
        )
    return records, mismatches


def transfer_summary(ledger: dict) -> dict:
    sessions = ledger.get("sessions") or []
    return {
        "sessions": len(sessions),
        "started": sessions[0]["started"] if sessions else ledger["pinned_at"],
        "completed": sessions[-1].get("ended") if sessions else None,
        "bytes_network": sum(int(s.get("bytes_network") or 0) for s in sessions),
        "bytes_adopted": sum(int(s.get("bytes_adopted") or 0) for s in sessions),
        "bytes_local_sources": sum(
            int(s.get("bytes_local_sources") or 0) for s in sessions
        ),
        "retries": sum(int(s.get("retries") or 0) for s in sessions),
    }


def local_mirrors(ledger: dict) -> list[str]:
    """Return stable local-source provenance for the registered manifest."""
    return sorted(
        {
            state["source"]
            for state in ledger.get("files", {}).values()
            if str(state.get("source") or "").startswith("local:")
        }
    )


def assemble_plan(
    partials: list[Path],
    vault: Path,
    *,
    move: bool = False,
    rehash: bool = False,
) -> dict:
    """What :func:`assemble_partials` would do — the same refusals, no lock, no write.

    Counts assume every file copied or adopted hashes clean at dest; the real
    run verifies each one before it trusts it, and ``--move`` releases a source
    file only once dest holds it as ``verified``.
    """
    import copy

    from .archiver import bundle_dir_for
    from .config import free_space_floor
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

    destination = bundle_dir_for(vault, source_from_ledger(seed), seed["revision"])
    root = payload_root_for(seed["repo_type"])
    destination_payload = destination / root
    dest_registered = (destination / "manifest.json").is_file()
    if move:
        _refuse_move_of_registered_sources(sources, destination)
    if dest_registered and not move:
        raise SystemExit(
            f"error: destination is already a registered bundle: {destination}"
        )
    if ledger_path(destination).is_file():
        ledger = load_ledger(destination)
        if not _same_transfer_set(seed, ledger):
            raise SystemExit(
                f"error: destination partial has a different transfer set: {destination}"
            )
    else:
        if dest_registered and not _registered_pin_matches(destination, seed):
            raise SystemExit(
                f"error: destination is a registered bundle of a different "
                f"pin: {destination}"
            )
        ledger = copy.deepcopy(seed)
        ledger["files"] = {}
    if dest_registered:
        state = "registered"
    elif ledger_path(destination).is_file():
        state = "partial"
    else:
        state = "new"

    expected = {item["path"]: int(item.get("size") or 0) for item in seed["expected"]}
    dest_verified = {
        rel for rel, st in ledger["files"].items() if st.get("status") == "verified"
    }
    present: set[str] = set()
    to_hash: list[tuple[str, int]] = []
    if destination_payload.is_dir():
        present = {
            rel for rel, _ in iter_payload_files(destination_payload) if rel in expected
        }
        to_hash = _files_to_hash(destination_payload, ledger, rehash=rehash)

    dest_real = destination.resolve()
    planned: dict[str, int] = {}
    rows = []
    for source_dir, source_ledger in sources:
        is_dest = source_dir.resolve() == dest_real
        row = {
            "path": source_dir,
            "is_destination": is_dest,
            "copy_files": 0,
            "copy_bytes": 0,
            "already_at_dest": 0,
            "in_earlier_partial": 0,
        }
        if not dest_registered and not is_dest:
            for relative, source_file in iter_payload_files(source_dir / root):
                if relative not in expected:
                    continue
                if relative in present:
                    row["already_at_dest"] += 1
                elif relative in planned:
                    row["in_earlier_partial"] += 1
                else:
                    size = source_file.stat().st_size
                    planned[relative] = size
                    row["copy_files"] += 1
                    row["copy_bytes"] += size
        rows.append((row, source_ledger))

    verified_after = dest_verified | {rel for rel, _ in to_hash} | set(planned)
    missing = [rel for rel in expected if rel not in verified_after]
    cache_files = cache_bytes = 0
    if not dest_registered:
        cache_files, cache_bytes = _merge_transfer_caches(
            [source_dir / root for source_dir, _ledger in sources],
            destination_payload,
            apply=False,
        )

    for row, source_ledger in rows:
        row["release_files"] = 0
        row["release_bytes"] = 0
        row["dissolves"] = False
        if not move or row["is_destination"]:
            continue
        source_payload = row["path"] / root
        left = 0  # expected files the source would still hold or still owe
        for relative, size in expected.items():
            src_state = source_ledger["files"].get(relative) or {}
            status = src_state.get("status")
            if status == "moved":
                continue
            if (
                status == "verified"
                and relative in verified_after
                and _payload_path(source_payload, relative).is_file()
            ):
                row["release_files"] += 1
                row["release_bytes"] += int(src_state.get("size") or size)
                continue
            left += 1
        row["dissolves"] = bool(row["release_files"]) and left == 0

    probe = dest_real
    while not probe.exists():
        probe = probe.parent
    free = shutil.disk_usage(probe).free
    needed = sum(planned.values()) + cache_bytes
    floor = free_space_floor(vault)
    return {
        "destination": destination,
        "state": state,
        "move": move,
        "rehash": rehash,
        "sources": [row for row, _ledger in rows],
        "copy": {"files": len(planned), "bytes": sum(planned.values())},
        "caches": {"files": cache_files, "partial_bytes": cache_bytes},
        "hash": {
            "files": len(to_hash),
            "bytes": sum(size for _rel, size in to_hash),
            "network_mount": bool(to_hash) and bool(is_network_filesystem(destination)),
        },
        "disk": {
            "checked_path": str(probe),
            "free_bytes": free,
            "needed_bytes": needed,
            "min_free_bytes": floor,
            "verdict": disk_verdict(free, needed, floor),
        },
        "after": {
            "verified_files": len(verified_after),
            "total_files": len(expected),
            "missing_files": len(missing),
            "missing_bytes": sum(expected[rel] for rel in missing),
            "complete": not missing,
        },
    }


def print_assemble_plan(plan: dict, progress=print) -> None:
    """Render :func:`assemble_plan` in the shape of :func:`print_plan`."""
    from .readme_gen import human_size

    def files(n: int) -> str:
        return f"{n} file{'s' if n != 1 else ''}"

    state = {
        "new": "new partial",
        "partial": "existing partial",
        "registered": "registered — payload frozen, nothing is copied",
    }[plan["state"]]
    progress(f"Would assemble into {plan['destination']}  ({state})")
    label = "from:"
    for row in plan["sources"]:
        notes = []
        if row["already_at_dest"]:
            notes.append(f"{row['already_at_dest']} already at dest")
        if row["in_earlier_partial"]:
            notes.append(f"{row['in_earlier_partial']} in an earlier partial")
        note = f"  ({', '.join(notes)})" if notes else ""
        if row["is_destination"]:
            what = "is the destination — nothing to hand to itself"
        elif plan["state"] == "registered":
            what = "dest already holds the payload"
        elif row["copy_files"]:
            what = f"copy {files(row['copy_files'])}, {human_size(row['copy_bytes'])}{note}"
        else:
            what = f"nothing to copy{note}"
        progress(f"  {label:<10}{row['path']}  — {what}")
        label = ""
    caches = plan["caches"]
    if caches["files"]:
        progress(
            f"  {'caches:':<10}{files(caches['files'])}, "
            f"{human_size(caches['partial_bytes'])} of resumable partial bytes "
            "(the longest copy of each)"
        )
    copied = plan["copy"]["files"]
    hashed = plan["hash"]
    if copied or hashed["files"]:
        parts = []
        if copied:
            parts.append(f"{copied} copied")
        if hashed["files"]:
            parts.append(
                f"{hashed['files']} already at dest, {human_size(hashed['bytes'])}"
                + (" (--rehash)" if plan["rehash"] else " (no verified record yet)")
            )
        progress(
            f"  {'verify:':<10}{files(copied + hashed['files'])} against the pin "
            f"({'; '.join(parts)})"
        )
        if hashed["network_mount"]:
            progress(
                "            warning: dest is on a network mount, so that hashing "
                "reads every byte back over the wire — run assemble where the "
                "vault is a local disk"
            )
    disk = plan["disk"]
    if disk["needed_bytes"]:
        floor = disk.get("min_free_bytes")
        floor_note = f" ({human_size(floor)} floor)" if floor else ""
        progress(
            f"  {'disk:':<10}needs {human_size(disk['needed_bytes'])}, "
            f"free {human_size(disk['free_bytes'])}{floor_note} at "
            f"{disk['checked_path']} — {disk['verdict'].upper()}"
        )
    after = plan["after"]
    have = f"{after['verified_files']}/{after['total_files']} files verified"
    if plan["state"] == "registered":
        progress(f"  {'after:':<10}{have} — already registered; dest is unchanged")
    elif after["complete"]:
        progress(
            f"  {'after:':<10}{have} — complete; `darsay archive` there registers "
            "it with zero network"
        )
    else:
        progress(
            f"  {'after:':<10}{have}; {files(after['missing_files'])} "
            f"({human_size(after['missing_bytes'])}) still to fetch — continue "
            "with `darsay archive`"
        )
    if plan["move"]:
        label = "--move:"
        for row in plan["sources"]:
            if row["is_destination"]:
                continue
            if row["release_files"]:
                fate = (
                    "removed (nothing left to fetch there)"
                    if row["dissolves"]
                    else "skeleton (pin + hashes stay)"
                )
                what = (
                    f"release {files(row['release_files'])} "
                    f"({human_size(row['release_bytes'])}) once verified at dest "
                    f"→ {fate}"
                )
            else:
                what = "nothing to release yet"
            progress(f"  {label:<10}{row['path']}  — {what}")
            label = ""

"""Offline diagnostics and reversible repairs for a darsay vault.

The doctor deliberately treats bundle payload bytes and hand-edited curation as
out of bounds.  Its fixers only rebuild derived README files or quarantine
stale, disposable coordination/runtime metadata.  Every mutation is prepared
in an append-only journal before the atomic replace/rename happens, which makes
both interruption recovery and ``doctor undo`` deterministic.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import errno
import fcntl
import hashlib
import io
import json
import os
import re
import shlex
import shutil
import socket
import stat
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import __version__

SCHEMA_VERSION = 1
DOCTOR_DIR = ".doctor"
RUNS_DIR = "runs"
EXIT_HEALTHY = 0
EXIT_FINDINGS = 1
EXIT_PARTIAL = 2
EXIT_FIX_FAILED = 3
EXIT_UNSAFE = 4
EXIT_CONCURRENT = 5
EXIT_ONLINE = 6
EXIT_USAGE = 64
EXIT_NOINPUT = 66
EXIT_CANTCREATE = 73
EXIT_IOERR = 74

SEVERITY_ORDER = {"info": 0, "warning": 1, "error": 2, "critical": 3}
SECRET_RE = re.compile(r"(?i)(token|password|secret|api[_-]?key)(\s*[:=]\s*)([^\s,;]+)")


class DoctorError(RuntimeError):
    """An expected doctor failure with a stable process exit code."""

    def __init__(self, message: str, code: int):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Check:
    id: str
    description: str
    severity: str
    fixer: str | None = None
    risk: str = "none"


CHECKS: tuple[Check, ...] = (
    Check(
        "vault.access",
        "The vault root is a readable directory and its doctor evidence area is writable.",
        "error",
    ),
    Check(
        "config.parse",
        "User and vault TOML configuration parses with the installed darsay.",
        "error",
    ),
    Check(
        "bundle.manifest",
        "Every registered bundle manifest is readable and supported.",
        "critical",
    ),
    Check(
        "bundle.paths",
        "Bundle-controlled payload paths remain relative and contain no symlink escape.",
        "critical",
    ),
    Check(
        "bundle.payload",
        "Registered payload files, sizes, and SHA-256 digests match the manifest.",
        "critical",
    ),
    Check(
        "bundle.readme",
        "The generated bundle README matches manifest.json plus curation.md.",
        "warning",
        "bundle.readme.regenerate",
        "low",
    ),
    Check(
        "transfer.lock",
        "Transfer locks are parseable and do not belong to a dead or copied owner.",
        "warning",
        "transfer.lock.quarantine",
        "low",
    ),
    Check(
        "runtime.hydration",
        "Disposable hydration records parse and point at an existing interpreter.",
        "warning",
        "runtime.hydration.quarantine",
        "low",
    ),
)

CHECK_BY_ID = {check.id: check for check in CHECKS}
FIXERS = {check.fixer: check.id for check in CHECKS if check.fixer is not None}
_DEFAULT_FIXER = object()
_FIXER_MUTATION_SCOPE = {
    "bundle.readme.regenerate": ("README.md", "WriteFile"),
    "transfer.lock.quarantine": ("transfer.lock", "Rename"),
    "runtime.hydration.quarantine": ("hydration.json", "Rename"),
}
_UNDO_MUTATION_SCOPE = {
    "doctor.undo.restore": "WriteFile",
    "doctor.undo.quarantine": "Rename",
}
_MUTABLE_BUNDLE_FILES = frozenset(
    leaf for leaf, _operation in _FIXER_MUTATION_SCOPE.values()
)
ONLY_ALIASES = {
    "fm-vault-and-bundle-state-generated-readme-is-missing-or-stale": "bundle.readme",
    "fm-vault-and-bundle-state-manifest-is-unreadable-or-truncated": "bundle.manifest",
    "fm-vault-and-bundle-state-manifest-schema-or-kind-is-incompatible": "bundle.manifest",
    "fm-configuration-runtime-and-installation-configuration-file-is-malformed-or-unrea": "config.parse",
    "fm-configuration-runtime-and-installation-hydration-record-is-corrupt-or-incompati": "runtime.hydration",
}


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="microseconds")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _redact(value: str) -> str:
    return SECRET_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}<redacted>", value)


def _json_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
    ).encode("utf-8")


def mutate_artifact_write(path: Path, data: bytes, mode: int = 0o600) -> None:
    """Atomically write evidence or restored state in the destination directory."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, raw_tmp = tempfile.mkstemp(prefix=f".{path.name}.doctor.tmp.", dir=path.parent)
    tmp = Path(raw_tmp)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        _fsync_dir(path.parent)
    finally:
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink()


def _fsync_dir(path: Path) -> None:
    with contextlib.suppress(OSError):
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def _private_dir(path: Path) -> None:
    """Create/open a private directory without following a final symlink."""
    if path.is_symlink():
        raise DoctorError(f"refusing symlinked doctor directory: {path}", EXIT_UNSAFE)
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise DoctorError(
            f"cannot create private directory {path}: {exc}", EXIT_CANTCREATE
        ) from exc
    if path.is_symlink() or not path.is_dir():
        raise DoctorError(f"unsafe doctor directory: {path}", EXIT_UNSAFE)
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise DoctorError(
            f"cannot safely open private directory {path}: {exc}", EXIT_UNSAFE
        ) from exc
    try:
        os.fchmod(fd, 0o700)
    finally:
        os.close(fd)


def _ensure_vault(vault: Path) -> None:
    """Require an existing vault; doctor must never create a mistyped root."""
    if not vault.exists():
        raise DoctorError(f"vault root not found: {vault}", EXIT_NOINPUT)
    if not vault.is_dir():
        raise DoctorError(f"vault root is not a directory: {vault}", EXIT_NOINPUT)


def _target_version() -> str:
    package = Path(__file__).resolve().parent
    digest = hashlib.sha256()
    for path in sorted(package.glob("*.py")):
        digest.update(path.name.encode())
        with contextlib.suppress(OSError):
            digest.update(path.read_bytes())
    return digest.hexdigest()


def _run_id(created_at: str) -> str:
    stamp = created_at.replace("-", "").replace(":", "")[:15] + "Z"
    suffix = hashlib.sha256(f"{_target_version()}:{created_at}".encode()).hexdigest()[
        :6
    ]
    return f"{stamp}__{suffix}"


def _doctor_root(vault: Path) -> Path:
    return vault / DOCTOR_DIR


def _runs_root(vault: Path) -> Path:
    return _doctor_root(vault) / RUNS_DIR


def _validate_evidence_tree(vault: Path, *, create: bool) -> None:
    """Reject attacker/user-controlled symlinks in the private evidence tree."""
    root = _doctor_root(vault)
    runs = _runs_root(vault)
    for path, label in ((root, "doctor root"), (runs, "doctor runs root")):
        if path.is_symlink():
            raise DoctorError(f"refusing symlinked {label}: {path}", EXIT_UNSAFE)
        if path.exists() and not path.is_dir():
            raise DoctorError(f"{label} is not a directory: {path}", EXIT_UNSAFE)
    if create:
        _private_dir(root)
        _private_dir(runs)


def _append_private_line(path: Path, line: str) -> None:
    """Append and fsync without following a pre-existing file symlink."""
    if path.is_symlink():
        raise DoctorError(f"refusing symlinked doctor artifact: {path}", EXIT_UNSAFE)
    flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags, 0o600)
        with os.fdopen(fd, "ab") as handle:
            handle.write(line.encode())
            handle.flush()
            os.fsync(handle.fileno())
    except OSError as exc:
        raise DoctorError(
            f"cannot append doctor artifact {path}: {exc}", EXIT_IOERR
        ) from exc


def _new_run(vault: Path, command: str) -> tuple[str, Path, str]:
    created_at = _utc_now()
    run_id = _run_id(created_at)
    root = _doctor_root(vault)
    runs = _runs_root(vault)
    _ensure_vault(vault)
    _validate_evidence_tree(vault, create=True)
    run = runs / run_id
    if run.exists():
        raise DoctorError(f"doctor run already exists: {run}", EXIT_CANTCREATE)
    try:
        _private_dir(run)
        _private_dir(run / "backups")
        _private_dir(run / "quarantine")
        for name, data in (
            ("actions.jsonl", b""),
            ("stderr.log", b""),
            ("stdout.json", b""),
        ):
            mutate_artifact_write(run / name, data)
        mutate_artifact_write(
            run / "run.json",
            _json_bytes(
                {
                    "schema_version": SCHEMA_VERSION,
                    "run_id": run_id,
                    "created_at": created_at,
                    "command": command,
                    "target": {"kind": "vault", "path": str(vault)},
                    "tool": {"name": "darsay", "version": __version__},
                }
            ),
        )
        mutate_latest(root, run)
    except OSError as exc:
        raise DoctorError(f"cannot create doctor run: {exc}", EXIT_CANTCREATE) from exc
    return run_id, run, created_at


def mutate_latest(root: Path, run: Path) -> None:
    latest = root / "latest"
    tmp = root / f".latest.doctor-symlink-tmp.{os.getpid()}"
    with contextlib.suppress(FileNotFoundError):
        tmp.unlink()
    try:
        tmp.symlink_to(Path(RUNS_DIR) / run.name)
        os.replace(tmp, latest)
        _fsync_dir(root)
    except (OSError, NotImplementedError):
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink()
        mutate_artifact_write(root / "latest.txt", (run.name + "\n").encode())


def _relative_in_vault(vault: Path, path: Path) -> Path:
    base = vault.resolve()
    lexical = path.absolute()
    try:
        relative = lexical.relative_to(base)
    except ValueError as exc:
        raise DoctorError(
            f"unsafe repair target outside vault: {path}", EXIT_UNSAFE
        ) from exc
    parent = base
    for part in relative.parts[:-1]:
        parent = parent / part
        if parent.is_symlink():
            raise DoctorError(
                f"refusing to mutate through symlinked directory: {parent}",
                EXIT_UNSAFE,
            )
    candidate = path.resolve(strict=False)
    try:
        candidate.relative_to(base)
    except ValueError as exc:
        raise DoctorError(
            f"unsafe repair target outside vault: {path}", EXIT_UNSAFE
        ) from exc
    if not relative.parts or relative.parts[0] == DOCTOR_DIR:
        raise DoctorError(f"unsafe repair target: {path}", EXIT_UNSAFE)
    if path.is_symlink():
        raise DoctorError(f"refusing to mutate symlink: {path}", EXIT_UNSAFE)
    return relative


def _validate_mutation_scope(relative: Path, fixer: str, operation: str) -> None:
    """Enforce the declared write allowlist even for untrusted journals."""
    if len(relative.parts) != 3 or relative.name not in _MUTABLE_BUNDLE_FILES:
        raise DoctorError(
            f"repair target is outside the mutable bundle allowlist: {relative}",
            EXIT_UNSAFE,
        )
    if fixer in _FIXER_MUTATION_SCOPE:
        expected_leaf, expected_operation = _FIXER_MUTATION_SCOPE[fixer]
        if (relative.name, operation) == (expected_leaf, expected_operation):
            return
    elif fixer in _UNDO_MUTATION_SCOPE:
        if operation == _UNDO_MUTATION_SCOPE[fixer]:
            return
    raise DoctorError(f"fixer {fixer!r} cannot {operation} {relative}", EXIT_UNSAFE)


def _safe_artifact_relative(root: Path, value: str) -> Path:
    relative = Path(value)
    if relative.is_absolute() or ".." in relative.parts:
        raise DoctorError(f"unsafe artifact path in journal: {value}", EXIT_UNSAFE)
    candidate = (root / relative).resolve(strict=False)
    try:
        candidate.relative_to(root.resolve())
    except ValueError as exc:
        raise DoctorError(
            f"artifact path escapes run directory: {value}", EXIT_UNSAFE
        ) from exc
    return candidate


@contextlib.contextmanager
def _open_target_parent(vault: Path, relative: Path):
    """Pin the real target parent by walking from the vault with no follows."""
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        current_fd = os.open(vault, flags)
    except OSError as exc:
        raise DoctorError(
            f"cannot safely open vault {vault}: {exc}", EXIT_UNSAFE
        ) from exc
    try:
        for part in relative.parts[:-1]:
            try:
                next_fd = os.open(part, flags, dir_fd=current_fd)
            except OSError as exc:
                raise DoctorError(
                    f"cannot safely open repair parent {relative.parent}: {exc}",
                    EXIT_UNSAFE,
                ) from exc
            os.close(current_fd)
            current_fd = next_fd
        yield current_fd
    finally:
        os.close(current_fd)


def _parent_is_current(vault: Path, relative: Path, pinned_fd: int) -> bool:
    try:
        with _open_target_parent(vault, relative) as current_fd:
            current = os.fstat(current_fd)
            pinned = os.fstat(pinned_fd)
            return (current.st_dev, current.st_ino) == (pinned.st_dev, pinned.st_ino)
    except DoctorError:
        return False


def _snapshot_at(parent_fd: int, name: str) -> dict:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(name, flags, dir_fd=parent_fd)
    except FileNotFoundError:
        return {
            "exists": False,
            "data": None,
            "sha256": None,
            "mode": None,
            "mtime_ns": None,
            "device": None,
            "inode": None,
            "size": None,
        }
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.ENOTDIR):
            raise DoctorError(
                f"refusing symlinked/non-directory repair target: {name}",
                EXIT_UNSAFE,
            ) from exc
        raise DoctorError(
            f"cannot safely open repair target {name}: {exc}", EXIT_IOERR
        ) from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise DoctorError(f"refusing to mutate non-file: {name}", EXIT_UNSAFE)
        chunks = []
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        data = b"".join(chunks)
        final_info = os.fstat(fd)
        if (
            info.st_dev,
            info.st_ino,
            info.st_size,
            info.st_mtime_ns,
        ) != (
            final_info.st_dev,
            final_info.st_ino,
            final_info.st_size,
            final_info.st_mtime_ns,
        ):
            raise DoctorError(
                f"repair target changed while it was read: {name}", EXIT_UNSAFE
            )
        return {
            "exists": True,
            "data": data,
            "sha256": _sha256(data),
            "mode": stat.S_IMODE(info.st_mode),
            "mtime_ns": info.st_mtime_ns,
            "device": info.st_dev,
            "inode": info.st_ino,
            "size": info.st_size,
        }
    finally:
        os.close(fd)


def _same_snapshot(left: dict, right: dict) -> bool:
    keys = ("exists", "sha256", "mode", "mtime_ns", "device", "inode", "size")
    return all(left[key] == right[key] for key in keys)


def _backup_snapshot(
    run: Path, relative: Path, snapshot: dict
) -> tuple[str | None, dict]:
    if not snapshot["exists"]:
        return None, {
            "before_exists": False,
            "before_sha256": None,
            "before_mode": None,
            "before_mtime_ns": None,
        }
    backup_relative = Path("backups") / relative
    backup = run / backup_relative
    _private_dir(backup.parent)
    mutate_artifact_write(backup, snapshot["data"], 0o600)
    if backup.is_symlink() or backup.read_bytes() != snapshot["data"]:
        raise DoctorError(f"backup verification failed for {relative}", EXIT_IOERR)
    return backup_relative.as_posix(), {
        "before_exists": True,
        "before_sha256": snapshot["sha256"],
        "before_mode": snapshot["mode"],
        "before_mtime_ns": snapshot["mtime_ns"],
    }


def mutate_prepare_at(
    parent_fd: int, target_name: str, data: bytes, mode: int, action_id: str
) -> str:
    """Prepare and fsync a same-directory temporary via a pinned parent."""
    temp_name = f".{target_name}.doctor.tmp.{os.getpid()}.{action_id}"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(temp_name, flags, mode, dir_fd=parent_fd)
    try:
        os.fchmod(fd, mode)
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError("short write while preparing doctor mutation")
            view = view[written:]
        os.fsync(fd)
    except BaseException:
        os.close(fd)
        with contextlib.suppress(OSError):
            os.remove(temp_name, dir_fd=parent_fd)
        raise
    os.close(fd)
    return temp_name


def mutate_replace_at(parent_fd: int, temp_name: str, target_name: str) -> None:
    """Commit a prepared file inside the pinned parent."""
    os.replace(
        temp_name,
        target_name,
        src_dir_fd=parent_fd,
        dst_dir_fd=parent_fd,
    )
    os.fsync(parent_fd)


def mutate_set_metadata_at(
    parent_fd: int, target_name: str, mode: int | None, mtime_ns: int | None
) -> None:
    if mode is None and mtime_ns is None:
        return
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(target_name, flags, dir_fd=parent_fd)
    try:
        if mode is not None:
            os.fchmod(fd, mode)
        if mtime_ns is not None:
            os.utime(fd, ns=(mtime_ns, mtime_ns))
        os.fsync(fd)
    finally:
        os.close(fd)


def mutate_restore_at(
    parent_fd: int,
    target_name: str,
    before: dict,
    action_id: str,
    quarantine_fd: int,
    quarantine_name: str,
) -> None:
    """Restore the pinned parent's pre-mutation state after a late failure."""
    if before["exists"]:
        temp_name = mutate_prepare_at(
            parent_fd,
            target_name,
            before["data"],
            before["mode"],
            f"{action_id}.rollback",
        )
        mutate_replace_at(parent_fd, temp_name, target_name)
        mutate_set_metadata_at(
            parent_fd, target_name, before["mode"], before["mtime_ns"]
        )
        return
    current = _snapshot_at(parent_fd, target_name)
    if current["exists"]:
        os.rename(
            target_name,
            quarantine_name,
            src_dir_fd=parent_fd,
            dst_dir_fd=quarantine_fd,
        )
        os.fsync(parent_fd)
        os.fsync(quarantine_fd)


def _append_action(run: Path, action: dict) -> None:
    path = run / "actions.jsonl"
    line = json.dumps(action, ensure_ascii=False, sort_keys=True) + "\n"
    _append_private_line(path, line)


def mutate(
    vault: Path,
    run: Path,
    target: Path,
    *,
    operation: str,
    fixer: str,
    data: bytes | None = None,
    expected_before_exists: bool | None = None,
    expected_before_sha256: str | None = None,
    result_mode: int | None = None,
    result_mtime_ns: int | None = None,
) -> dict:
    """The sole target-state mutation chokepoint for fix and undo."""
    started_at_ns = time.time_ns()
    if operation not in ("WriteFile", "Rename"):
        raise DoctorError(f"unsupported mutation operation: {operation}", EXIT_UNSAFE)
    relative = _relative_in_vault(vault, target)
    _validate_mutation_scope(relative, fixer, operation)
    if operation == "WriteFile" and data is None:
        raise DoctorError("WriteFile mutation has no data", EXIT_UNSAFE)
    action_id = _sha256(f"{run.name}\0{relative.as_posix()}\0{started_at_ns}".encode())[
        :16
    ]
    quarantine_relative = Path("quarantine") / relative
    quarantine = run / quarantine_relative
    _private_dir(quarantine.parent)
    quarantine_flags = (
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    )
    with _open_target_parent(vault, relative) as parent_fd:
        try:
            quarantine_fd = os.open(quarantine.parent, quarantine_flags)
        except OSError as exc:
            raise DoctorError(
                f"cannot safely open quarantine directory {quarantine.parent}: {exc}",
                EXIT_UNSAFE,
            ) from exc
        try:
            before_snapshot = _snapshot_at(parent_fd, relative.name)
            if operation == "Rename" and not before_snapshot["exists"]:
                raise DoctorError(
                    f"refusing to quarantine missing file: {target}", EXIT_UNSAFE
                )
            backup, before = _backup_snapshot(run, relative, before_snapshot)
            if expected_before_exists is not None and (
                before["before_exists"] != expected_before_exists
                or before["before_sha256"] != expected_before_sha256
            ):
                raise DoctorError(
                    f"refusing repair because {target} changed after diagnosis",
                    EXIT_UNSAFE,
                )
            action = {
                "schema_version": SCHEMA_VERSION,
                "event": "mutation_prepared",
                "action_id": action_id,
                "action": operation,
                "op": operation,
                "run_id": run.name,
                "fixer_id": fixer,
                "path": relative.as_posix(),
                "backup": backup,
                "after_sha256": _sha256(data) if data is not None else None,
                "started_at_ns": started_at_ns,
                "finished_at_ns": None,
                "ok": None,
                "rolled_back": False,
                **before,
            }
            action["before_hash"] = (
                f"sha256:{action['before_sha256']}"
                if action["before_sha256"] is not None
                else None
            )
            canonical_after = action["after_sha256"] or action["before_sha256"]
            action["after_hash"] = (
                f"sha256:{canonical_after}" if canonical_after is not None else None
            )
            if operation == "Rename":
                action["rename_to"] = quarantine_relative.as_posix()
            if _snapshot_at(quarantine_fd, quarantine.name)["exists"]:
                raise DoctorError(
                    f"quarantine target already exists: {quarantine}", EXIT_UNSAFE
                )
            _append_action(run, action)

            committed = False
            rolled_back = False
            temp_name = None
            rollback_error = None
            try:
                if operation == "WriteFile":
                    mode = (
                        before["before_mode"]
                        if before["before_mode"] is not None
                        else 0o644
                    )
                    temp_name = mutate_prepare_at(
                        parent_fd, relative.name, data or b"", mode, action_id
                    )
                    if not _parent_is_current(vault, relative, parent_fd):
                        raise DoctorError(
                            f"repair parent changed before commit: {target}",
                            EXIT_UNSAFE,
                        )
                    if not _same_snapshot(
                        before_snapshot, _snapshot_at(parent_fd, relative.name)
                    ):
                        raise DoctorError(
                            f"repair target changed before commit: {target}",
                            EXIT_UNSAFE,
                        )
                    mutate_replace_at(parent_fd, temp_name, relative.name)
                    temp_name = None
                    committed = True
                    mutate_set_metadata_at(
                        parent_fd,
                        relative.name,
                        result_mode,
                        result_mtime_ns,
                    )
                    after_snapshot = _snapshot_at(parent_fd, relative.name)
                    if after_snapshot["sha256"] != action["after_sha256"]:
                        raise DoctorError(
                            f"repair verification failed for {target}", EXIT_IOERR
                        )
                    action["after_mode"] = after_snapshot["mode"]
                    action["after_mtime_ns"] = after_snapshot["mtime_ns"]
                else:
                    if not _parent_is_current(vault, relative, parent_fd):
                        raise DoctorError(
                            f"repair parent changed before commit: {target}",
                            EXIT_UNSAFE,
                        )
                    if not _same_snapshot(
                        before_snapshot, _snapshot_at(parent_fd, relative.name)
                    ):
                        raise DoctorError(
                            f"repair target changed before commit: {target}",
                            EXIT_UNSAFE,
                        )
                    os.rename(
                        relative.name,
                        quarantine.name,
                        src_dir_fd=parent_fd,
                        dst_dir_fd=quarantine_fd,
                    )
                    os.fsync(parent_fd)
                    os.fsync(quarantine_fd)
                    committed = True
                    quarantined = _snapshot_at(quarantine_fd, quarantine.name)
                    if (
                        _snapshot_at(parent_fd, relative.name)["exists"]
                        or quarantined["sha256"] != before_snapshot["sha256"]
                    ):
                        raise DoctorError(
                            f"quarantine verification failed for {target}", EXIT_IOERR
                        )
                    action["after_mode"] = None
                    action["after_mtime_ns"] = None

                if not _parent_is_current(vault, relative, parent_fd):
                    raise DoctorError(
                        f"repair parent changed during commit: {target}", EXIT_UNSAFE
                    )
                action.update(
                    {
                        "event": "mutation_finished",
                        "finished_at_ns": time.time_ns(),
                        "ok": True,
                    }
                )
                _append_action(
                    run,
                    {
                        "schema_version": SCHEMA_VERSION,
                        "event": "mutation_finished",
                        "action_id": action_id,
                        "finished_at_ns": action["finished_at_ns"],
                        "ok": True,
                        "rolled_back": False,
                        "after_mode": action["after_mode"],
                        "after_mtime_ns": action["after_mtime_ns"],
                    },
                )
                return action
            except BaseException as exc:
                if temp_name is not None:
                    with contextlib.suppress(OSError):
                        os.remove(temp_name, dir_fd=parent_fd)
                # A Python signal can be delivered after the atomic syscall
                # succeeds but before the following assignment.  Infer that
                # narrow outcome from the pinned descriptors before deciding
                # whether compensation is needed.
                if not committed:
                    with contextlib.suppress(DoctorError, OSError):
                        current = _snapshot_at(parent_fd, relative.name)
                        if operation == "WriteFile":
                            committed = (
                                current["exists"]
                                and current["sha256"] == action["after_sha256"]
                            )
                        else:
                            quarantined = _snapshot_at(quarantine_fd, quarantine.name)
                            committed = (
                                not current["exists"]
                                and quarantined["sha256"] == before_snapshot["sha256"]
                            )
                if committed:
                    try:
                        if operation == "Rename":
                            if _snapshot_at(parent_fd, relative.name)["exists"]:
                                raise DoctorError(
                                    f"cannot roll back quarantine over new target: {target}",
                                    EXIT_UNSAFE,
                                )
                            os.rename(
                                quarantine.name,
                                relative.name,
                                src_dir_fd=quarantine_fd,
                                dst_dir_fd=parent_fd,
                            )
                            os.fsync(quarantine_fd)
                            os.fsync(parent_fd)
                        else:
                            mutate_restore_at(
                                parent_fd,
                                relative.name,
                                before_snapshot,
                                action_id,
                                quarantine_fd,
                                quarantine.name,
                            )
                        rolled_back = True
                    except BaseException as restore_exc:
                        rollback_error = _redact(str(restore_exc))
                else:
                    with contextlib.suppress(DoctorError, OSError):
                        rolled_back = _same_snapshot(
                            before_snapshot, _snapshot_at(parent_fd, relative.name)
                        )
                with contextlib.suppress(DoctorError):
                    terminal = {
                        "schema_version": SCHEMA_VERSION,
                        "event": "mutation_finished",
                        "action_id": action_id,
                        "finished_at_ns": time.time_ns(),
                        "ok": False,
                        "rolled_back": rolled_back,
                        "error": _redact(str(exc)),
                    }
                    if rollback_error is not None:
                        terminal["rollback_error"] = rollback_error
                    _append_action(run, terminal)
                raise
        finally:
            os.close(quarantine_fd)


@contextlib.contextmanager
def _mutation_lock(vault: Path):
    # Validate the user-supplied root before creating doctor.lock or any
    # evidence directory.  In particular, undo/gc input errors stay pure.
    _ensure_vault(vault)
    root = _doctor_root(vault)
    _validate_evidence_tree(vault, create=True)
    lock_path = root / "doctor.lock"
    try:
        if lock_path.is_symlink():
            raise DoctorError(
                f"refusing symlinked doctor lock: {lock_path}", EXIT_UNSAFE
            )
        flags = os.O_RDWR | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(lock_path, flags, 0o600)
        handle = os.fdopen(fd, "a+b")
    except DoctorError:
        raise
    except OSError as exc:
        raise DoctorError(f"cannot open doctor lock: {exc}", EXIT_CANTCREATE) from exc
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise DoctorError(
                "another darsay doctor mutation is already running", EXIT_CONCURRENT
            ) from exc
        hold_ms = os.environ.get("DARSAY_DOCTOR_TEST_HOLD_LOCK_MS")
        if hold_ms:
            with contextlib.suppress(ValueError):
                time.sleep(min(max(float(hold_ms), 0.0), 5000.0) / 1000.0)
        yield
    finally:
        handle.close()


def _finding(
    check_id: str,
    summary: str,
    path: Path | None = None,
    *,
    evidence: str | None = None,
    fixer: str | None | object = _DEFAULT_FIXER,
    severity: str | None = None,
    expected_before_exists: bool | None = None,
    expected_before_sha256: str | None = None,
) -> dict:
    check = CHECK_BY_ID[check_id]
    chosen_fixer = check.fixer if fixer is _DEFAULT_FIXER else fixer
    evidence_value = {}
    if path is not None:
        evidence_value["file"] = str(path)
    if evidence:
        evidence_value["detail"] = _redact(evidence)
    finding = {
        "id": f"{check_id}:{_sha256(str(path or summary).encode())[:10]}",
        "check_id": check_id,
        "severity": severity or check.severity,
        "priority": {
            "critical": "P0",
            "error": "P1",
            "warning": "P2",
            "info": "P3",
        }[severity or check.severity],
        "subsystem": check_id.split(".", 1)[0],
        "title": _redact(summary),
        "confidence": 1.0,
        "summary": _redact(summary),
        "path": str(path) if path is not None else None,
        "evidence": evidence_value,
        "auto_fixable": chosen_fixer is not None,
        "fixer_id": chosen_fixer,
        "recommended_action": (
            "Run `darsay doctor --fix`, then rerun the doctor."
            if chosen_fixer
            else "Inspect the named file; darsay will not guess or replace archival facts."
        ),
        "remediation": {
            "command": (
                f"darsay doctor --fix --only {check_id}" if chosen_fixer else None
            ),
            "explain_command": f"darsay doctor explain {check_id}",
            "auto_fixable": chosen_fixer is not None,
            "estimated_actions": 1 if chosen_fixer else 0,
        },
    }
    if expected_before_exists is not None:
        finding["expected_before_exists"] = expected_before_exists
        finding["expected_before_sha256"] = expected_before_sha256
        if expected_before_sha256 is not None:
            finding["evidence"]["hash"] = f"sha256:{expected_before_sha256}"
    return finding


def _stabilize_finding_ids(vault: Path, findings: list[dict]) -> None:
    """Make finding IDs independent of the vault's absolute location."""
    for finding in findings:
        raw_path = finding.get("path")
        if raw_path is None:
            identity = finding["summary"]
        else:
            path = Path(raw_path)
            try:
                identity = path.absolute().relative_to(vault.resolve()).as_posix()
            except ValueError:
                # Findings should be vault-local.  Keep an unexpected external path
                # stable without leaking or hashing its machine-specific prefix.
                identity = path.name
        digest_input = f"{finding['check_id']}\0{identity}".encode()
        finding["id"] = f"{finding['check_id']}:{_sha256(digest_input)[:10]}"


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


def _copied_lock(owner: dict, bundle: Path) -> bool:
    identity = owner.get("bundle")
    if not isinstance(identity, dict):
        return False
    try:
        actual = bundle.stat()
        return (int(identity["device"]), int(identity["inode"])) != (
            actual.st_dev,
            actual.st_ino,
        )
    except (KeyError, OSError, TypeError, ValueError):
        return False


def _transfer_lock_state(vault: Path, bundle: Path) -> str:
    """Classify a bundle's project lock without following path components."""
    relative = _relative_in_vault(vault, bundle / "transfer.lock")
    with _open_target_parent(vault, relative) as bundle_fd:
        snapshot = _snapshot_at(bundle_fd, relative.name)
        if not snapshot["exists"]:
            return "absent"
        try:
            owner = json.loads(snapshot["data"].decode("utf-8"))
            if not isinstance(owner, dict):
                owner = {}
        except (UnicodeError, json.JSONDecodeError):
            owner = {}
        try:
            pid = int(owner.get("pid") or 0)
        except (TypeError, ValueError):
            pid = 0
        bundle_info = os.fstat(bundle_fd)
        identity = owner.get("bundle")
        copied = False
        if isinstance(identity, dict):
            try:
                copied = (int(identity["device"]), int(identity["inode"])) != (
                    bundle_info.st_dev,
                    bundle_info.st_ino,
                )
            except (KeyError, TypeError, ValueError):
                copied = False
        same_host = owner.get("host") == socket.gethostname()
        return (
            "stale"
            if not owner or copied or (same_host and not _pid_alive(pid))
            else "live"
        )


def _dead_doctor_lock_snapshot(bundle_fd: int, snapshot: dict) -> bool:
    if not snapshot["exists"]:
        return False
    try:
        owner = json.loads(snapshot["data"].decode("utf-8"))
        pid = int(owner.get("pid") or 0)
        identity = owner["bundle"]
        bundle_info = os.fstat(bundle_fd)
        same_bundle = (int(identity["device"]), int(identity["inode"])) == (
            bundle_info.st_dev,
            bundle_info.st_ino,
        )
    except (
        KeyError,
        TypeError,
        ValueError,
        UnicodeError,
        json.JSONDecodeError,
    ):
        return False
    return (
        owner.get("purpose") == "darsay doctor repair"
        and owner.get("host") == socket.gethostname()
        and same_bundle
        and not _pid_alive(pid)
    )


def _dead_doctor_lock(vault: Path, bundle: Path) -> bool:
    """Return whether a stale lock is provably from an interrupted doctor."""
    relative = _relative_in_vault(vault, bundle / "transfer.lock")
    with _open_target_parent(vault, relative) as bundle_fd:
        return _dead_doctor_lock_snapshot(
            bundle_fd, _snapshot_at(bundle_fd, relative.name)
        )


@contextlib.contextmanager
def _bundle_mutation_lock(
    vault: Path,
    bundle: Path,
    *,
    owner_run_id: str,
    reclaim_interrupted: bool = False,
):
    """Own the project's per-bundle lock for a complete repair transaction."""
    relative = _relative_in_vault(vault, bundle / "transfer.lock")
    with _open_target_parent(vault, relative) as bundle_fd:
        bundle_info = os.fstat(bundle_fd)
        owner = {
            "pid": os.getpid(),
            "host": socket.gethostname(),
            "started": _utc_now(),
            "purpose": "darsay doctor repair",
            "doctor_run_id": owner_run_id,
            "bundle": {
                "path": str(bundle.resolve()),
                "device": bundle_info.st_dev,
                "inode": bundle_info.st_ino,
            },
        }
        payload = _json_bytes(owner)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        if reclaim_interrupted:
            stale = _snapshot_at(bundle_fd, relative.name)
            if stale["exists"]:
                if not _dead_doctor_lock_snapshot(bundle_fd, stale):
                    raise DoctorError(
                        f"bundle transfer lock blocks recovery: {bundle}",
                        EXIT_CONCURRENT,
                    )
                if not _same_snapshot(stale, _snapshot_at(bundle_fd, relative.name)):
                    raise DoctorError(
                        f"bundle transfer lock changed during recovery: {bundle}",
                        EXIT_CONCURRENT,
                    )
                os.remove(relative.name, dir_fd=bundle_fd)
                os.fsync(bundle_fd)
        try:
            fd = os.open(relative.name, flags, 0o600, dir_fd=bundle_fd)
        except FileExistsError as exc:
            raise DoctorError(
                f"bundle transfer lock blocks repair: {bundle}", EXIT_CONCURRENT
            ) from exc
        except OSError as exc:
            raise DoctorError(
                f"cannot acquire bundle repair lock for {bundle}: {exc}",
                EXIT_CONCURRENT,
            ) from exc
        try:
            info = os.fstat(fd)
            view = memoryview(payload)
            while view:
                written = os.write(fd, view)
                if written <= 0:
                    raise OSError("short write while acquiring bundle repair lock")
                view = view[written:]
            os.fsync(fd)
        except BaseException:
            os.close(fd)
            with contextlib.suppress(OSError):
                os.remove(relative.name, dir_fd=bundle_fd)
            raise
        os.close(fd)
        lock_identity = (info.st_dev, info.st_ino)
        lock_hash = _sha256(payload)
        body_error: BaseException | None = None
        try:
            yield
        except BaseException as exc:
            body_error = exc
            raise
        finally:
            try:
                current = _snapshot_at(bundle_fd, relative.name)
                if not current["exists"]:
                    raise DoctorError(
                        f"bundle repair lock disappeared: {bundle}", EXIT_UNSAFE
                    )
                if (
                    current["device"],
                    current["inode"],
                    current["sha256"],
                ) != (*lock_identity, lock_hash):
                    raise DoctorError(
                        f"bundle repair lock changed ownership: {bundle}", EXIT_UNSAFE
                    )
                os.remove(relative.name, dir_fd=bundle_fd)
                os.fsync(bundle_fd)
            except BaseException:
                if body_error is None:
                    raise


def _fix_bundle(finding: dict) -> Path:
    path = Path(finding["path"])
    return path.parent


def _preflight_bundle_locks(vault: Path, findings: list[dict]) -> list[Path]:
    """Validate every affected bundle before the first target mutation."""
    by_bundle: dict[Path, list[dict]] = {}
    for finding in findings:
        bundle = _fix_bundle(finding)
        _relative_in_vault(vault, bundle / "transfer.lock")
        by_bundle.setdefault(bundle, []).append(finding)

    acquire: list[Path] = []
    for bundle in sorted(by_bundle, key=lambda value: value.as_posix()):
        bundle_findings = by_bundle[bundle]
        has_lock_fixer = any(
            row["fixer_id"] == "transfer.lock.quarantine" for row in bundle_findings
        )
        has_other_fixer = any(
            row["fixer_id"] != "transfer.lock.quarantine" for row in bundle_findings
        )
        state = _transfer_lock_state(vault, bundle)
        if state == "live":
            raise DoctorError(
                f"live bundle transfer lock blocks repair: {bundle}", EXIT_CONCURRENT
            )
        if state == "stale":
            if not has_lock_fixer or has_other_fixer:
                raise DoctorError(
                    "stale bundle transfer lock must be repaired separately before "
                    f"other fixes: {bundle}",
                    EXIT_CONCURRENT,
                )
            continue
        if has_lock_fixer:
            raise DoctorError(
                f"transfer lock changed after diagnosis: {bundle}", EXIT_UNSAFE
            )
        if has_other_fixer:
            acquire.append(bundle)
    return acquire


def _preflight_undo_locks(
    vault: Path,
    plans: list[dict],
    *,
    recovering_interrupted: bool = False,
    recovery_bundles: set[Path] | None = None,
) -> list[Path]:
    """Return all bundle locks that must be held for a complete undo."""
    by_bundle: dict[Path, list[Path]] = {}
    for plan in plans:
        target = Path(plan["target"])
        _relative_in_vault(vault, target)
        by_bundle.setdefault(target.parent, []).append(target)
    # A process can die after journaling intent but before changing the target.
    # Such an action produces no inverse plan, but it still leaves the
    # doctor-owned project lease behind. Include those bundles so explicit
    # recovery can prove and reclaim the dead lease before marking the source
    # action recovered.
    for bundle in recovery_bundles or set():
        _relative_in_vault(vault, bundle / "transfer.lock")
        by_bundle.setdefault(bundle, [])

    acquire = []
    for bundle in sorted(by_bundle, key=lambda value: value.as_posix()):
        targets = by_bundle[bundle]
        restores_project_lock = any(path.name == "transfer.lock" for path in targets)
        if restores_project_lock:
            if len(targets) != 1:
                raise DoctorError(
                    "cannot combine transfer-lock restoration with other bundle "
                    f"undo actions: {bundle}",
                    EXIT_UNSAFE,
                )
            if _transfer_lock_state(vault, bundle) != "absent":
                raise DoctorError(
                    f"bundle transfer lock blocks undo: {bundle}", EXIT_CONCURRENT
                )
            continue
        state = _transfer_lock_state(vault, bundle)
        if state != "absent" and not (
            recovering_interrupted
            and state == "stale"
            and _dead_doctor_lock(vault, bundle)
        ):
            raise DoctorError(
                f"bundle transfer lock blocks undo: {bundle}", EXIT_CONCURRENT
            )
        acquire.append(bundle)
    return acquire


class _BudgetExpired(RuntimeError):
    pass


def _scan_payload(
    bundle: Path,
    manifest: dict,
    *,
    quick: bool,
    expired,
) -> tuple[list[dict], bool]:
    """Compare immutable payload state without writing verification history."""
    from .hashing import hash_file
    from .schema import payload_root

    findings = []
    root_name = payload_root(manifest)
    root_relative = Path(root_name)
    if (
        not root_name
        or root_relative.is_absolute()
        or root_name in (".", "..")
        or any(part in ("", ".", "..") for part in root_relative.parts)
    ):
        return [
            _finding(
                "bundle.paths",
                "manifest payload root is unsafe",
                bundle / "manifest.json",
                evidence=repr(root_name),
                fixer=None,
            )
        ], False
    payload = bundle / root_relative
    try:
        payload.resolve(strict=False).relative_to(bundle.resolve())
    except (OSError, ValueError):
        return [
            _finding(
                "bundle.paths",
                "manifest payload root escapes the bundle",
                bundle / "manifest.json",
                evidence=repr(root_name),
                fixer=None,
            )
        ], False
    parent = bundle
    for part in root_relative.parts:
        parent = parent / part
        if parent.is_symlink():
            return [
                _finding(
                    "bundle.paths",
                    "manifest payload root traverses a symlink",
                    bundle / "manifest.json",
                    evidence=parent.relative_to(bundle).as_posix(),
                    fixer=None,
                )
            ], False
    try:
        expected_rows = manifest["inventory"]["files"]
        if not isinstance(expected_rows, list):
            raise TypeError("inventory.files is not a list")
        expected = {}
        unsafe = []
        prefix = root_name.rstrip("/") + "/"
        for row in expected_rows:
            path = row["path"]
            if not isinstance(path, str) or not path.startswith(prefix):
                unsafe.append(repr(path))
                continue
            relative = Path(path[len(prefix) :])
            if relative.is_absolute() or not relative.parts or ".." in relative.parts:
                unsafe.append(path)
                continue
            expected[path] = row
    except (KeyError, TypeError) as exc:
        return [
            _finding(
                "bundle.manifest",
                "manifest inventory shape is invalid",
                bundle / "manifest.json",
                evidence=str(exc),
                fixer=None,
            )
        ], False

    if unsafe:
        findings.append(
            _finding(
                "bundle.paths",
                "manifest contains unsafe or non-payload inventory paths",
                bundle / "manifest.json",
                evidence=", ".join(unsafe[:5]),
                fixer=None,
            )
        )

    actual: dict[str, dict] = {}
    symlinks = []

    def interrupt_check() -> None:
        if expired():
            raise _BudgetExpired

    if payload.is_symlink():
        symlinks.append(str(payload))
    elif payload.is_dir():
        try:
            for path in sorted(payload.rglob("*")):
                if expired():
                    raise _BudgetExpired
                if path.is_symlink():
                    symlinks.append(str(path))
                    continue
                if not path.is_file():
                    continue
                relative = path.relative_to(payload).as_posix()
                if relative.split("/", 1)[0] == ".cache":
                    continue
                key = f"{root_name}/{relative}"
                row = {"size": path.stat().st_size}
                if not quick:
                    row["sha256"] = hash_file(
                        path,
                        with_blake3=False,
                        interrupt_check=interrupt_check,
                    )["sha256"]
                actual[key] = row
        except _BudgetExpired:
            return findings, True
        except OSError as exc:
            findings.append(
                _finding(
                    "bundle.payload",
                    "payload could not be read completely",
                    payload,
                    evidence=str(exc),
                    fixer=None,
                )
            )
            return findings, False

    if symlinks:
        findings.append(
            _finding(
                "bundle.paths",
                "payload contains symlinks; doctor refuses to follow them",
                payload,
                evidence=", ".join(symlinks[:5]),
                fixer=None,
            )
        )

    expected_paths = set(expected)
    actual_paths = set(actual)
    missing = sorted(expected_paths - actual_paths)
    extra = sorted(actual_paths - expected_paths)
    size_mismatch = sorted(
        path
        for path in expected_paths & actual_paths
        if expected[path].get("size") != actual[path]["size"]
    )
    digest_mismatch = (
        []
        if quick
        else sorted(
            path
            for path in expected_paths & actual_paths
            if expected[path].get("sha256") != actual[path].get("sha256")
        )
    )
    if missing or extra or size_mismatch or digest_mismatch:
        parts = []
        for label, paths in (
            ("missing", missing),
            ("extra", extra),
            ("size mismatch", size_mismatch),
            ("digest mismatch", digest_mismatch),
        ):
            if paths:
                parts.append(f"{label}: {', '.join(paths[:5])}")
        findings.append(
            _finding(
                "bundle.payload",
                "immutable payload differs from manifest inventory",
                payload,
                evidence="; ".join(parts),
                fixer=None,
            )
        )
    return findings, False


def _scan(
    vault: Path,
    *,
    quick: bool = False,
    budget: float | None = None,
    since_timestamp: float | None = None,
    checks: set[str] | None = None,
) -> tuple[list[dict], bool]:
    from .archiver import load_manifest
    from .config import resolved_settings
    from .readme_gen import render_bundle_readme
    from .vault import iter_bundle_dirs

    findings: list[dict] = []
    started = time.monotonic()
    enabled_checks = set(CHECK_BY_ID) if checks is None else checks

    def enabled(check_id: str) -> bool:
        return check_id in enabled_checks

    def expired() -> bool:
        return budget is not None and time.monotonic() - started >= budget

    if enabled("vault.access") and vault.exists() and not vault.is_dir():
        findings.append(
            _finding("vault.access", "vault root is not a directory", vault)
        )
        return findings, False
    if (
        enabled("vault.access")
        and vault.exists()
        and not os.access(vault, os.R_OK | os.X_OK)
    ):
        findings.append(_finding("vault.access", "vault root is not readable", vault))

    if enabled("config.parse"):
        config_errors = io.StringIO()
        try:
            with contextlib.redirect_stderr(config_errors):
                resolved_settings(vault)
        except SystemExit as exc:
            findings.append(
                _finding(
                    "config.parse",
                    "configuration cannot be loaded",
                    evidence=str(exc),
                )
            )

    if not vault.exists():
        return findings, False

    bundle_checks = {
        "bundle.manifest",
        "bundle.paths",
        "bundle.payload",
        "bundle.readme",
        "transfer.lock",
        "runtime.hydration",
    }
    bundles = iter_bundle_dirs(vault) if enabled_checks & bundle_checks else []
    for bundle in bundles:
        if expired():
            return sorted(
                findings, key=lambda f: (f["check_id"], f["path"] or "")
            ), True
        if since_timestamp is not None:
            try:
                newest = max(
                    [bundle.stat().st_mtime]
                    + [
                        path.lstat().st_mtime
                        for path in bundle.rglob("*")
                        if path.is_file() or path.is_symlink()
                    ]
                )
            except OSError:
                newest = since_timestamp
            if newest < since_timestamp:
                continue
        manifest_path = bundle / "manifest.json"
        manifest_checks = {
            "bundle.manifest",
            "bundle.paths",
            "bundle.payload",
            "bundle.readme",
        }
        if enabled_checks & manifest_checks and manifest_path.is_symlink():
            check_id = (
                "bundle.paths"
                if enabled("bundle.paths")
                else next(check for check in manifest_checks if enabled(check))
            )
            findings.append(
                _finding(
                    check_id,
                    "bundle manifest is a symlink and will not be followed",
                    manifest_path,
                    fixer=None,
                )
            )
        elif enabled_checks & manifest_checks and manifest_path.is_file():
            try:
                manifest = load_manifest(bundle)
            except SystemExit as exc:
                check_id = (
                    "bundle.manifest"
                    if enabled("bundle.manifest")
                    else next(check for check in manifest_checks if enabled(check))
                )
                findings.append(
                    _finding(
                        check_id,
                        "bundle manifest is unreadable or blocks the requested check",
                        manifest_path,
                        evidence=str(exc),
                        fixer=None,
                    )
                )
            else:
                payload_findings: list[dict] = []
                payload_partial = False
                if enabled_checks & {"bundle.paths", "bundle.payload"}:
                    payload_findings, payload_partial = _scan_payload(
                        bundle, manifest, quick=quick, expired=expired
                    )
                    if enabled("bundle.payload") and not enabled("bundle.paths"):
                        payload_findings = [
                            (
                                _finding(
                                    "bundle.payload",
                                    f"payload inspection refused: {finding['summary']}",
                                    Path(finding["path"]),
                                    evidence=finding["evidence"].get("detail"),
                                    fixer=None,
                                )
                                if finding["check_id"] == "bundle.paths"
                                else finding
                            )
                            for finding in payload_findings
                        ]
                    payload_findings = [
                        finding
                        for finding in payload_findings
                        if enabled(finding["check_id"])
                    ]
                findings.extend(payload_findings)
                if payload_partial:
                    return sorted(
                        findings, key=lambda f: (f["check_id"], f["path"] or "")
                    ), True
                readme_prerequisites_healthy = not any(
                    finding["check_id"] in {"bundle.manifest", "bundle.paths"}
                    for finding in payload_findings
                )
                readme = bundle / "README.md"
                curation = bundle / "curation.md"
                unsafe_control = next(
                    (path for path in (curation, readme) if path.is_symlink()), None
                )
                if enabled("bundle.readme") and unsafe_control is not None:
                    findings.append(
                        _finding(
                            "bundle.readme",
                            "README inputs or target contain a symlink",
                            unsafe_control,
                            fixer=None,
                        )
                    )
                    readme_prerequisites_healthy = False
                if (
                    enabled("bundle.readme")
                    and not quick
                    and readme_prerequisites_healthy
                ):
                    try:
                        expected = render_bundle_readme(bundle, manifest).encode(
                            "utf-8"
                        )
                        actual = readme.read_bytes() if readme.is_file() else None
                        if actual != expected:
                            summary = (
                                "generated README is missing"
                                if actual is None
                                else "generated README does not match its inputs"
                            )
                            findings.append(
                                _finding(
                                    "bundle.readme",
                                    summary,
                                    readme,
                                    evidence=f"expected sha256 {_sha256(expected)}",
                                    expected_before_exists=actual is not None,
                                    expected_before_sha256=(
                                        _sha256(actual) if actual is not None else None
                                    ),
                                )
                            )
                    except (OSError, KeyError, TypeError, UnicodeError) as exc:
                        findings.append(
                            _finding(
                                "bundle.readme",
                                "README cannot be regenerated safely from current inputs",
                                bundle / "README.md",
                                evidence=str(exc),
                                fixer=None,
                            )
                        )

        lock = bundle / "transfer.lock"
        if enabled("transfer.lock") and lock.is_symlink():
            findings.append(
                _finding(
                    "transfer.lock",
                    "transfer lock is a symlink and will not be followed",
                    lock,
                    fixer=None,
                )
            )
        elif enabled("transfer.lock") and lock.is_file():
            lock_bytes = None
            try:
                lock_bytes = lock.read_bytes()
                owner = json.loads(lock_bytes.decode("utf-8"))
                if not isinstance(owner, dict):
                    owner = {}
            except (OSError, json.JSONDecodeError, UnicodeError):
                with contextlib.suppress(OSError):
                    lock_bytes = lock.read_bytes()
                owner = {}
            same_host = owner.get("host") == socket.gethostname()
            try:
                pid = int(owner.get("pid") or 0)
            except (TypeError, ValueError):
                pid = 0
            copied = bool(owner) and _copied_lock(owner, bundle)
            stale = not owner or copied or (same_host and not _pid_alive(pid))
            if stale:
                kind = "copied" if copied else "stale or malformed"
                findings.append(
                    _finding(
                        "transfer.lock",
                        f"{kind} transfer lock blocks archive resume",
                        lock,
                        evidence=f"owner pid={pid or '?'} host={owner.get('host', '?')}",
                        fixer=(_DEFAULT_FIXER if lock_bytes is not None else None),
                        expected_before_exists=(
                            True if lock_bytes is not None else None
                        ),
                        expected_before_sha256=(
                            _sha256(lock_bytes) if lock_bytes is not None else None
                        ),
                    )
                )

        hydration = bundle / "hydration.json"
        if enabled("runtime.hydration") and hydration.is_symlink():
            findings.append(
                _finding(
                    "runtime.hydration",
                    "hydration record is a symlink and will not be followed",
                    hydration,
                    fixer=None,
                )
            )
        elif enabled("runtime.hydration") and hydration.is_file():
            problem = None
            hydration_bytes = None
            try:
                hydration_bytes = hydration.read_bytes()
                record = json.loads(hydration_bytes.decode("utf-8"))
                executable = Path(record["env"]["python_executable"])
                if not executable.is_file():
                    problem = f"recorded interpreter does not exist: {executable}"
            except (
                OSError,
                json.JSONDecodeError,
                KeyError,
                TypeError,
                UnicodeError,
            ) as exc:
                problem = f"record is unreadable: {exc}"
            if problem:
                findings.append(
                    _finding(
                        "runtime.hydration",
                        "disposable hydration record is stale",
                        hydration,
                        evidence=problem,
                        fixer=(_DEFAULT_FIXER if hydration_bytes is not None else None),
                        expected_before_exists=(
                            True if hydration_bytes is not None else None
                        ),
                        expected_before_sha256=(
                            _sha256(hydration_bytes)
                            if hydration_bytes is not None
                            else None
                        ),
                    )
                )

    return sorted(findings, key=lambda f: (f["check_id"], f["path"] or "")), False


def _selected(findings: list[dict], args) -> list[dict]:
    only = {
        ONLY_ALIASES.get(value, value)
        for value in _csv_values(getattr(args, "only", None))
    }
    skip = {
        ONLY_ALIASES.get(value, value)
        for value in _csv_values(getattr(args, "skip", None))
    }
    minimum = SEVERITY_ORDER[getattr(args, "severity", None) or "info"]
    selected = []
    for finding in findings:
        check_id = finding["check_id"]
        if only and check_id not in only and finding["id"] not in only:
            continue
        if check_id in skip or finding["id"] in skip:
            continue
        if SEVERITY_ORDER[finding["severity"]] < minimum:
            continue
        selected.append(finding)
    return selected


def _csv_values(values: list[str] | None) -> list[str]:
    return [
        item.strip()
        for value in values or []
        for item in value.split(",")
        if item.strip()
    ]


def _selection_scope(args) -> set[str]:
    """Validate selectors and return the checks that may execute."""
    only_values = [
        ONLY_ALIASES.get(value, value)
        for value in _csv_values(getattr(args, "only", None))
    ]
    skip_values = [
        ONLY_ALIASES.get(value, value)
        for value in _csv_values(getattr(args, "skip", None))
    ]

    def check_for(value: str) -> str:
        check_id = value.split(":", 1)[0]
        if check_id not in CHECK_BY_ID:
            raise DoctorError(f"unknown doctor check or finding: {value}", EXIT_USAGE)
        return check_id

    only_checks = {check_for(value) for value in only_values}
    for value in skip_values:
        check_for(value)
    skip_checks = {value for value in skip_values if ":" not in value}
    selected = only_checks or set(CHECK_BY_ID)
    selected -= skip_checks
    minimum = SEVERITY_ORDER[getattr(args, "severity", None) or "info"]
    return {
        check_id
        for check_id in selected
        if SEVERITY_ORDER[CHECK_BY_ID[check_id].severity] >= minimum
    }


def _apply_finding(vault: Path, run: Path, finding: dict) -> dict:
    from .archiver import load_manifest
    from .readme_gen import render_bundle_readme

    path = Path(finding["path"])
    fixer = finding["fixer_id"]
    expected_kwargs = {
        "expected_before_exists": finding.get("expected_before_exists"),
        "expected_before_sha256": finding.get("expected_before_sha256"),
    }
    if fixer == "bundle.readme.regenerate":
        bundle = path.parent
        for controlled in (bundle / "manifest.json", bundle / "curation.md", path):
            if controlled.is_symlink():
                raise DoctorError(
                    f"refusing README repair through symlink: {controlled}",
                    EXIT_UNSAFE,
                )
        try:
            manifest = load_manifest(bundle)
        except SystemExit as exc:
            raise DoctorError(
                f"README repair prerequisite changed: {exc}", EXIT_UNSAFE
            ) from exc
        prerequisite_findings, partial = _scan_payload(
            bundle, manifest, quick=True, expired=lambda: False
        )
        if partial or any(
            row["check_id"] in {"bundle.manifest", "bundle.paths"}
            for row in prerequisite_findings
        ):
            raise DoctorError(
                f"README repair prerequisites are unsafe in {bundle}", EXIT_UNSAFE
            )
        data = render_bundle_readme(bundle, manifest).encode("utf-8")
        return mutate(
            vault,
            run,
            path,
            operation="WriteFile",
            data=data,
            fixer=fixer,
            **expected_kwargs,
        )
    if fixer == "transfer.lock.quarantine":
        if path.is_symlink():
            raise DoctorError(
                f"refusing to quarantine a symlinked transfer lock: {path}",
                EXIT_UNSAFE,
            )
        try:
            current = path.read_bytes()
            owner = json.loads(current.decode("utf-8"))
            if not isinstance(owner, dict):
                owner = {}
        except (OSError, UnicodeError, json.JSONDecodeError):
            owner = {}
        same_host = owner.get("host") == socket.gethostname()
        try:
            pid = int(owner.get("pid") or 0)
        except (TypeError, ValueError):
            pid = 0
        copied = bool(owner) and _copied_lock(owner, path.parent)
        if owner and not copied and (not same_host or _pid_alive(pid)):
            raise DoctorError(
                f"refusing to quarantine a lock that is now live: {path}",
                EXIT_UNSAFE,
            )
        return mutate(
            vault,
            run,
            path,
            operation="Rename",
            fixer=fixer,
            **expected_kwargs,
        )
    if fixer == "runtime.hydration.quarantine":
        if path.is_symlink():
            raise DoctorError(
                f"refusing to quarantine a symlinked hydration record: {path}",
                EXIT_UNSAFE,
            )
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
            executable = Path(record["env"]["python_executable"])
            if executable.is_file():
                raise DoctorError(
                    f"refusing to quarantine a hydration record that is now healthy: {path}",
                    EXIT_UNSAFE,
                )
        except DoctorError:
            raise
        except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError):
            pass
        return mutate(
            vault,
            run,
            path,
            operation="Rename",
            fixer=fixer,
            **expected_kwargs,
        )
    raise DoctorError(f"unknown fixer: {fixer}", EXIT_UNSAFE)


def _report(
    vault: Path,
    run_id: str,
    created_at: str,
    command: str,
    findings: list[dict],
    actions: list[dict],
    *,
    partial: bool = False,
    dry_run: bool = False,
) -> dict:
    finished_at = _utc_now()
    try:
        duration_ms = round(
            (
                dt.datetime.fromisoformat(finished_at)
                - dt.datetime.fromisoformat(created_at)
            ).total_seconds()
            * 1000,
            3,
        )
    except ValueError:
        duration_ms = None
    counts = {severity: 0 for severity in SEVERITY_ORDER}
    for finding in findings:
        counts[finding["severity"]] += 1
    status = "partial" if partial else ("degraded" if findings else "healthy")
    doctor_command = f"darsay --vault {shlex.quote(str(vault))} doctor"
    next_commands = []
    if any(f["auto_fixable"] for f in findings):
        next_commands.append(f"{doctor_command} --fix")
    if any(not f["auto_fixable"] for f in findings):
        next_commands.append(f"{doctor_command} explain")
    if not findings:
        next_commands.append(f"{doctor_command} health")
    return {
        "schema_version": SCHEMA_VERSION,
        "tool": "darsay",
        "tool_version": __version__,
        "doctor_version": "1.0.0",
        "command": command,
        "run_id": run_id,
        "run_dir": str(_runs_root(vault) / run_id),
        "generated_at": created_at,
        "started_at": created_at,
        "finished_at": finished_at,
        "duration_ms": duration_ms,
        "target_sha": _target_version(),
        "target": {"kind": "vault", "path": str(vault)},
        "status": status,
        "dry_run": dry_run,
        "artifacts_created": True,
        "network_attempts": 0,
        "target_actions": len(actions),
        "summary": {
            "findings": len(findings),
            "total_findings": len(findings),
            "actions": len(actions),
            "actions_taken": len(actions),
            "by_severity": {
                "P0": counts["critical"],
                "P1": counts["error"],
                "P2": counts["warning"],
                "P3": counts["info"],
            },
            "auto_fixable": sum(f["auto_fixable"] for f in findings),
            "online_required": 0,
            **counts,
        },
        "findings": findings,
        "actions": actions,
        "next_commands": next_commands,
        "next_steps": [f"Run: {command}" for command in next_commands],
        "artifacts": str(_runs_root(vault) / run_id),
    }


def _report_markdown(report: dict) -> str:
    lines = [
        f"# darsay doctor — {report['status']}",
        "",
        f"- Run: `{report['run_id']}`",
        f"- Vault: `{report['target']['path']}`",
        f"- Findings: {report['summary']['findings']}",
        f"- Actions: {report['summary']['actions']}",
    ]
    if report["findings"]:
        lines += ["", "## Findings", ""]
        for finding in report["findings"]:
            lines.append(
                f"- **{finding['severity']}** `{finding['check_id']}` — {finding['summary']}"
            )
            if finding["path"]:
                lines.append(f"  - Path: `{finding['path']}`")
    if report["actions"]:
        lines += ["", "## Actions", ""]
        for action in report["actions"]:
            lines.append(f"- `{action['action']}` `{action['path']}`")
    if report["next_commands"]:
        lines += ["", "## Next", ""]
        lines.extend(f"- `{command}`" for command in report["next_commands"])
    return "\n".join(lines) + "\n"


def _save_report(vault: Path, run: Path, report: dict) -> None:
    try:
        _save_report_io(vault, run, report)
    except OSError as exc:
        raise DoctorError(f"cannot write doctor report: {exc}", EXIT_IOERR) from exc


def _save_report_io(vault: Path, run: Path, report: dict) -> None:
    payload = _json_bytes(report)
    mutate_artifact_write(run / "report.json", payload)
    mutate_artifact_write(run / "stdout.json", payload)
    mutate_artifact_write(run / "report.md", _report_markdown(report).encode("utf-8"))
    scorecard = {
        "schema_version": SCHEMA_VERSION,
        "run_id": report["run_id"],
        "status": report["status"],
        "checks_run": len(CHECKS),
        "findings": report["summary"]["findings"],
        "actions": report["summary"]["actions"],
    }
    mutate_artifact_write(run / "scorecard.json", _json_bytes(scorecard))
    undo = (
        "#!/bin/sh\nset -eu\n"
        f"exec darsay --vault {shlex.quote(str(vault))} doctor undo "
        f'{shlex.quote(report["run_id"])} "$@"\n'
    )
    mutate_artifact_write(run / "undo.sh", undo.encode("utf-8"), 0o700)
    history = _doctor_root(vault) / "scorecard_history.jsonl"
    line = json.dumps(scorecard, sort_keys=True) + "\n"
    _append_private_line(history, line)


def _emit(report: dict, args) -> None:
    if getattr(args, "json", False) or getattr(args, "robot_triage", False):
        print(json.dumps(report, indent=2, ensure_ascii=False, sort_keys=True))
        return
    if getattr(args, "quiet", False):
        return
    summary = report["summary"]
    print(
        f"darsay doctor: {report['status']} — {summary['findings']} finding(s), "
        f"{summary['actions']} action(s)"
    )
    for finding in report["findings"]:
        print(
            f"  {finding['severity'].upper():8} {finding['check_id']}: {finding['summary']}"
        )
        if finding["path"]:
            print(f"           {finding['path']}")
        if getattr(args, "verbose", False) or getattr(args, "explain", False):
            evidence = finding["evidence"]
            if evidence:
                print(f"           evidence: {json.dumps(evidence, sort_keys=True)}")
    print(f"  Run: {report['artifacts']}")
    for command in report["next_commands"]:
        print(f"  Next: {command}")


def _diagnose(args, vault: Path, *, force_fix: bool = False) -> int:
    if getattr(args, "online", False):
        raise DoctorError(
            "this doctor has no online checks; rerun without --online", EXIT_ONLINE
        )
    robot_triage = bool(getattr(args, "robot_triage", False))
    if robot_triage and (force_fix or bool(getattr(args, "fix", False))):
        raise DoctorError("--robot-triage cannot be combined with --fix", EXIT_USAGE)
    fix = force_fix or bool(getattr(args, "fix", False))
    dry_run = bool(getattr(args, "dry_run", False))
    since_timestamp = _parse_since(getattr(args, "since", None))
    checks = _selection_scope(args)
    if not vault.exists():
        raise DoctorError(f"vault root not found: {vault}", EXIT_NOINPUT)
    # Diagnosis creates an evidence run, so serialize it with mutations and
    # refuse to hide an interrupted prior transaction behind a healthy report.
    attempts = 1 if fix and not dry_run else 2
    for attempt in range(attempts):
        try:
            with _mutation_lock(vault):
                _assert_no_unresolved_runs(vault)
                return _diagnose_run(
                    args,
                    vault,
                    fix=fix,
                    dry_run=dry_run,
                    since_timestamp=since_timestamp,
                    checks=checks,
                )
        except DoctorError as exc:
            if exc.code != EXIT_CONCURRENT or attempt + 1 >= attempts:
                raise
            # A read-only diagnosis may wait once for a mutation already at
            # its atomic commit boundary. Mutating contenders still lose
            # immediately with exit 5.
            time.sleep(0.35)
    raise AssertionError("unreachable diagnostic lock retry")


def _diagnose_run(
    args,
    vault: Path,
    *,
    fix: bool,
    dry_run: bool,
    since_timestamp: float | None,
    checks: set[str],
) -> int:
    command = "fix" if fix else "diagnose"
    run_id, run, created_at = _new_run(vault, command)
    findings, budget_exhausted = _scan(
        vault,
        quick=bool(getattr(args, "quick", False)),
        budget=getattr(args, "budget", None),
        since_timestamp=since_timestamp,
        checks=checks,
    )
    _stabilize_finding_ids(vault, findings)
    findings = _selected(findings, args)
    findings_before = len(findings)
    actions: list[dict] = []
    if fix and not dry_run:
        fixable = [finding for finding in findings if finding["auto_fixable"]]
        if fixable:
            bundles = _preflight_bundle_locks(vault, fixable)
            # Acquire every project lock before the first action.  This keeps
            # a later busy bundle from leaving an earlier bundle half-fixed.
            with contextlib.ExitStack() as locks:
                for bundle in bundles:
                    locks.enter_context(
                        _bundle_mutation_lock(vault, bundle, owner_run_id=run.name)
                    )
                try:
                    for finding in sorted(
                        fixable,
                        key=lambda row: (
                            row["fixer_id"] != "transfer.lock.quarantine",
                            row["path"],
                        ),
                    ):
                        actions.append(_apply_finding(vault, run, finding))
                except DoctorError as exc:
                    if actions:
                        with contextlib.suppress(DoctorError, OSError):
                            _record_rollback(vault, run, exc)
                    raise
                except OSError as exc:
                    error = DoctorError(f"repair failed: {exc}", EXIT_FIX_FAILED)
                    if actions:
                        with contextlib.suppress(DoctorError, OSError):
                            _record_rollback(vault, run, error)
                    raise error from exc
        findings, rerun_partial = _scan(
            vault,
            quick=False,
            budget=None,
            since_timestamp=since_timestamp,
            checks=checks,
        )
        _stabilize_finding_ids(vault, findings)
        findings = _selected(findings, args)
        budget_exhausted = budget_exhausted or rerun_partial
    elif fix and dry_run:
        actions = [
            {
                "action": (
                    "WriteFile"
                    if f["fixer_id"] == "bundle.readme.regenerate"
                    else "Rename"
                ),
                "fixer_id": f["fixer_id"],
                "path": _relative_in_vault(vault, Path(f["path"])).as_posix(),
                "proposed": True,
            }
            for f in findings
            if f["auto_fixable"]
        ]
    report = _report(
        vault,
        run_id,
        created_at,
        command,
        findings,
        actions,
        partial=budget_exhausted,
        dry_run=dry_run,
    )
    report["summary"]["findings_before"] = findings_before
    report["summary"]["findings_after"] = len(findings)
    if getattr(args, "robot_triage", False):
        fixable = [finding for finding in findings if finding["auto_fixable"]]
        report["summary"]["ok"] = not findings
        report["quick_ref"] = [
            f"{priority}: {count} finding(s)"
            for priority, count in report["summary"]["by_severity"].items()
            if count
        ]
        report["quick_ref"].append(
            "All auto-fixable"
            if findings and len(fixable) == len(findings)
            else f"{len(fixable)} of {len(findings)} auto-fixable"
        )
        report["actions_planned"] = [
            {
                "fixer_id": finding["fixer_id"],
                "writes_to": [
                    _relative_in_vault(vault, Path(finding["path"])).as_posix()
                ],
                "estimated_bytes": None,
            }
            for finding in fixable
        ]
        doctor_command = f"darsay --vault {shlex.quote(str(vault))} doctor"
        report["recommended_command"] = (
            f"{doctor_command} --fix --only {fixable[0]['check_id']}"
            if fixable
            else f"{doctor_command} explain"
            if findings
            else f"{doctor_command} health"
        )
        report["capabilities_url"] = "darsay doctor capabilities --json"
        report["robot_docs_command"] = "darsay doctor robot-docs"
    if budget_exhausted or (fix and findings):
        exit_code = EXIT_PARTIAL
    else:
        exit_code = EXIT_FINDINGS if findings else EXIT_HEALTHY
    report["exit_code"] = exit_code
    report["ok"] = exit_code == EXIT_HEALTHY
    report["state"] = {
        EXIT_HEALTHY: "DONE_HEALTHY",
        EXIT_FINDINGS: "DONE_FINDINGS",
        EXIT_PARTIAL: "DONE_PARTIAL",
        EXIT_FIX_FAILED: "DONE_FIX_FAILED",
    }.get(exit_code, "DONE_ERROR")
    _save_report(vault, run, report)
    _emit(report, args)
    return exit_code


def _record_rollback(vault: Path, failed_run: Path, error: DoctorError) -> None:
    run_id, rollback_run, created_at = _new_run(vault, "rollback")
    restored = _undo_actions(vault, failed_run, _read_actions(failed_run), rollback_run)
    report = _report(vault, run_id, created_at, "rollback", [], restored)
    report.update(
        {
            "status": "failed",
            "state": "DONE_FIX_FAILED",
            "ok": False,
            "exit_code": error.code,
            "rolled_back_run": failed_run.name,
            "failure": _redact(str(error)),
        }
    )
    _save_report(vault, rollback_run, report)


def _parse_since(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise DoctorError(
            "--since must be an ISO-8601 timestamp such as 2026-08-30T12:00:00Z",
            EXIT_USAGE,
        ) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.timestamp()


def _resolve_run(vault: Path, ref: str) -> Path:
    _validate_evidence_tree(vault, create=False)
    runs = _runs_root(vault).resolve(strict=False)
    if ref == "latest":
        latest = _doctor_root(vault) / "latest"
        if latest.is_symlink():
            run = latest.resolve(strict=False)
        else:
            marker = _doctor_root(vault) / "latest.txt"
            if not marker.is_file():
                raise DoctorError("no doctor run named latest", EXIT_NOINPUT)
            run = runs / marker.read_text(encoding="utf-8").strip()
    else:
        if Path(ref).name != ref or ref in ("", ".", ".."):
            raise DoctorError(f"unsafe run reference: {ref}", EXIT_UNSAFE)
        run = runs / ref
    run = run.resolve(strict=False)
    try:
        run.relative_to(runs)
    except ValueError as exc:
        raise DoctorError(f"run escapes doctor history: {ref}", EXIT_UNSAFE) from exc
    if not run.is_dir():
        raise DoctorError(f"doctor run not found: {ref}", EXIT_NOINPUT)
    return run


def _read_actions(run: Path) -> list[dict]:
    path = run / "actions.jsonl"
    if path.is_symlink():
        raise DoctorError(f"action journal is a symlink: {path}", EXIT_UNSAFE)
    if not path.is_file():
        raise DoctorError(f"run has no action journal: {run.name}", EXIT_NOINPUT)
    actions = []
    by_id: dict[str, dict] = {}
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError("entry is not an object")
            event = value.get("event")
            action_id = value.get("action_id")
            if event == "recovery_finished":
                action_ids = value.get("action_ids")
                if (
                    not isinstance(action_ids, list)
                    or not action_ids
                    or any(
                        not isinstance(recovered_id, str) or recovered_id not in by_id
                        for recovered_id in action_ids
                    )
                ):
                    raise ValueError("recovery event has unknown prepared actions")
                for recovered_id in action_ids:
                    by_id[recovered_id].update(
                        {
                            "finished_at_ns": value.get("finished_at_ns"),
                            "ok": False,
                            "rolled_back": True,
                            "error": value.get("error"),
                        }
                    )
                continue
            if event == "mutation_finished":
                if not isinstance(action_id, str) or action_id not in by_id:
                    raise ValueError("completion event has no prepared action")
                by_id[action_id].update(
                    {
                        key: value[key]
                        for key in (
                            "finished_at_ns",
                            "ok",
                            "rolled_back",
                            "error",
                            "rollback_error",
                            "after_mode",
                            "after_mtime_ns",
                        )
                        if key in value
                    }
                )
                continue
            if not isinstance(action_id, str):
                # Schema-1 compatibility for runs written before two-phase
                # journal events were introduced.
                action_id = f"legacy-{len(actions)}"
                value["action_id"] = action_id
            if action_id in by_id:
                raise ValueError(f"duplicate prepared action {action_id}")
            actions.append(value)
            by_id[action_id] = value
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise DoctorError(
            f"malformed action journal {path}: {exc}", EXIT_UNSAFE
        ) from exc
    return actions


def _action_recovery_state(vault: Path, run: Path, action: dict) -> str:
    """Classify an interrupted action from durable journal and filesystem state."""
    relative = Path(str(action.get("path", "")))
    if relative.is_absolute() or not relative.parts or ".." in relative.parts:
        raise DoctorError(f"unsafe path in action journal: {relative}", EXIT_UNSAFE)
    fixer_id = action.get("fixer_id")
    operation = action.get("action") or action.get("op")
    if not isinstance(fixer_id, str) or not isinstance(operation, str):
        raise DoctorError("action journal is missing mutation scope", EXIT_UNSAFE)
    _validate_mutation_scope(relative, fixer_id, operation)
    target = vault / relative
    _relative_in_vault(vault, target)
    with _open_target_parent(vault, relative) as parent_fd:
        current = _snapshot_at(parent_fd, relative.name)

    before_exists = bool(action.get("before_exists"))
    before_sha = action.get("before_sha256")
    after_sha = action.get("after_sha256")
    if current["exists"] == before_exists and current["sha256"] == before_sha:
        state = "before"
    elif action.get("action") == "WriteFile" and current["sha256"] == after_sha:
        state = "after"
    elif action.get("action") == "Rename" and not current["exists"]:
        rename_to = action.get("rename_to")
        if not isinstance(rename_to, str):
            return "divergent"
        quarantined = _safe_artifact_relative(run, rename_to)
        if quarantined.is_symlink() or not quarantined.is_file():
            return "divergent"
        try:
            state = (
                "after"
                if _sha256(quarantined.read_bytes()) == before_sha
                else "divergent"
            )
        except OSError:
            state = "divergent"
    else:
        state = "divergent"

    if before_exists:
        backup_name = action.get("backup")
        if not isinstance(backup_name, str):
            return "divergent"
        backup = _safe_artifact_relative(run, backup_name)
        if backup.is_symlink() or not backup.is_file():
            return "divergent"
        try:
            if _sha256(backup.read_bytes()) != before_sha:
                return "divergent"
        except OSError:
            return "divergent"
    return state


def _unresolved_runs(vault: Path) -> list[dict]:
    """Return durable actions whose mutation outcome was never resolved."""
    _validate_evidence_tree(vault, create=False)
    root = _runs_root(vault)
    if not root.is_dir():
        return []
    unresolved = []
    for run in sorted(root.iterdir()):
        if run.is_symlink():
            raise DoctorError(f"doctor run is a symlink: {run}", EXIT_UNSAFE)
        if not run.is_dir() or not (run / "actions.jsonl").exists():
            continue
        actions = _read_actions(run)
        pending = [
            action
            for action in actions
            if action.get("ok") is None
            or (action.get("ok") is False and not action.get("rolled_back"))
        ]
        if pending:
            unresolved.append(
                {
                    "run": run,
                    "actions": [
                        {
                            "action": action,
                            "state": _action_recovery_state(vault, run, action),
                        }
                        for action in pending
                    ],
                }
            )
    return unresolved


def _assert_no_unresolved_runs(vault: Path, *, allow: Path | None = None) -> None:
    blocked = [
        item
        for item in _unresolved_runs(vault)
        if allow is None or item["run"].resolve() != allow.resolve()
    ]
    if not blocked:
        return
    item = blocked[0]
    states = ", ".join(row["state"] for row in item["actions"])
    run_id = item["run"].name
    command = (
        f"darsay --vault {shlex.quote(str(vault))} doctor undo "
        f"{shlex.quote(run_id)} --strict"
    )
    raise DoctorError(
        f"incomplete doctor mutation {run_id} ({states}); recover with: {command}",
        EXIT_UNSAFE,
    )


def _mark_actions_recovered(run: Path, actions: list[dict]) -> None:
    action_ids = [
        action["action_id"]
        for action in actions
        if action.get("ok") is None
        or (action.get("ok") is False and not action.get("rolled_back"))
    ]
    if not action_ids:
        return
    # One append makes recovery of a multi-action interrupted run all-or-none.
    # A partial final line is rejected by _read_actions, so it can never make
    # only a prefix of the source actions appear recovered.
    _append_action(
        run,
        {
            "schema_version": SCHEMA_VERSION,
            "event": "recovery_finished",
            "action_ids": action_ids,
            "finished_at_ns": time.time_ns(),
            "ok": False,
            "rolled_back": True,
            "error": "recovered by explicit doctor undo",
        },
    )


def _prepare_undo(vault: Path, source_run: Path, actions: list[dict]) -> list[dict]:
    """Validate the complete inverse transaction before changing any target."""
    plans: list[dict] = []
    virtual_hashes: dict[str, str | None] = {}
    for action in reversed(actions):
        if action.get("ok") is False and action.get("rolled_back"):
            continue
        relative = Path(str(action.get("path", "")))
        if relative.is_absolute() or not relative.parts or ".." in relative.parts:
            raise DoctorError(f"unsafe path in action journal: {relative}", EXIT_UNSAFE)
        fixer_id = action.get("fixer_id")
        operation = action.get("action") or action.get("op")
        if not isinstance(fixer_id, str) or not isinstance(operation, str):
            raise DoctorError("action journal is missing mutation scope", EXIT_UNSAFE)
        _validate_mutation_scope(relative, fixer_id, operation)
        target = vault / relative
        _relative_in_vault(vault, target)
        key = relative.as_posix()
        before_exists = bool(action.get("before_exists"))
        before_sha = action.get("before_sha256")
        after_sha = action.get("after_sha256")
        if key in virtual_hashes:
            current_sha = virtual_hashes[key]
        else:
            if target.exists() and not target.is_file():
                raise DoctorError(
                    f"refusing undo over non-file target: {target}", EXIT_UNSAFE
                )
            current_sha = _sha256(target.read_bytes()) if target.is_file() else None
        if before_exists and current_sha == before_sha:
            virtual_hashes[key] = before_sha
            continue
        if not before_exists and current_sha is None:
            virtual_hashes[key] = None
            continue
        if current_sha != after_sha:
            raise DoctorError(
                f"refusing undo because {target} changed after the repair",
                EXIT_UNSAFE,
            )
        if before_exists:
            backup_name = action.get("backup")
            if not isinstance(backup_name, str):
                raise DoctorError(f"missing backup for {target}", EXIT_UNSAFE)
            backup = _safe_artifact_relative(source_run, backup_name)
            if backup.is_symlink():
                raise DoctorError(f"backup is a symlink for {target}", EXIT_UNSAFE)
            if not backup.is_file():
                raise DoctorError(f"backup missing for {target}", EXIT_NOINPUT)
            data = backup.read_bytes()
            if _sha256(data) != before_sha:
                raise DoctorError(f"backup hash mismatch for {target}", EXIT_UNSAFE)
            plans.append(
                {
                    "operation": "WriteFile",
                    "target": target,
                    "data": data,
                    "expected_before_exists": current_sha is not None,
                    "expected_before_sha256": current_sha,
                    "result_mode": action.get("before_mode"),
                    "result_mtime_ns": action.get("before_mtime_ns"),
                }
            )
            virtual_hashes[key] = before_sha
        else:
            plans.append(
                {
                    "operation": "Rename",
                    "target": target,
                    "data": None,
                    "expected_before_exists": True,
                    "expected_before_sha256": current_sha,
                    "result_mode": None,
                    "result_mtime_ns": None,
                }
            )
            virtual_hashes[key] = None
    for plan in plans:
        mode = plan["result_mode"]
        mtime = plan["result_mtime_ns"]
        if mode is not None and (
            not isinstance(mode, int)
            or isinstance(mode, bool)
            or not 0 <= mode <= 0o7777
        ):
            raise DoctorError("unsafe mode in action journal", EXIT_UNSAFE)
        if mtime is not None and (
            not isinstance(mtime, int) or isinstance(mtime, bool) or mtime < 0
        ):
            raise DoctorError("unsafe mtime in action journal", EXIT_UNSAFE)
    return plans


def _execute_undo(vault: Path, undo_run: Path, plans: list[dict]) -> list[dict]:
    restored: list[dict] = []
    for plan in plans:
        restored.append(
            mutate(
                vault,
                undo_run,
                plan["target"],
                operation=plan["operation"],
                data=plan["data"],
                fixer=(
                    "doctor.undo.restore"
                    if plan["operation"] == "WriteFile"
                    else "doctor.undo.quarantine"
                ),
                expected_before_exists=plan["expected_before_exists"],
                expected_before_sha256=plan["expected_before_sha256"],
                result_mode=plan["result_mode"],
                result_mtime_ns=plan["result_mtime_ns"],
            )
        )
    return restored


def _undo_actions(
    vault: Path, source_run: Path, actions: list[dict], undo_run: Path
) -> list[dict]:
    return _execute_undo(vault, undo_run, _prepare_undo(vault, source_run, actions))


def _undo(args, vault: Path) -> int:
    with _mutation_lock(vault):
        source = _resolve_run(vault, args.run_ref)
        _assert_no_unresolved_runs(vault, allow=source)
        original = _read_actions(source)
        recovering_interrupted = any(
            action.get("ok") is None
            or (action.get("ok") is False and not action.get("rolled_back"))
            for action in original
        )
        plans = _prepare_undo(vault, source, original)
        recovery_bundles = (
            {
                (vault / Path(str(action["path"]))).parent
                for action in original
                if Path(str(action["path"])).name != "transfer.lock"
            }
            if recovering_interrupted
            else set()
        )
        bundles = _preflight_undo_locks(
            vault,
            plans,
            recovering_interrupted=recovering_interrupted,
            recovery_bundles=recovery_bundles,
        )
        run_id, run, created_at = _new_run(vault, "undo")
        with contextlib.ExitStack() as locks:
            for bundle in bundles:
                locks.enter_context(
                    _bundle_mutation_lock(
                        vault,
                        bundle,
                        owner_run_id=run.name,
                        reclaim_interrupted=recovering_interrupted,
                    )
                )
            try:
                restored = _execute_undo(vault, run, plans)
            except DoctorError as exc:
                with contextlib.suppress(DoctorError, OSError):
                    _record_rollback(vault, run, exc)
                raise
            except OSError as exc:
                error = DoctorError(f"undo failed: {exc}", EXIT_IOERR)
                with contextlib.suppress(DoctorError, OSError):
                    _record_rollback(vault, run, error)
                raise error from exc
            try:
                _mark_actions_recovered(source, original)
            except DoctorError as exc:
                # Recovery is part of the inverse transaction: if its durable
                # source marker cannot be written, compensate the already
                # completed target mutations before reporting failure.
                with contextlib.suppress(DoctorError, OSError):
                    _record_rollback(vault, run, exc)
                raise
        report = _report(vault, run_id, created_at, "undo", [], restored)
        report["undid_run"] = source.name
        report["exit_code"] = EXIT_HEALTHY
        report["ok"] = True
        report["state"] = "DONE_HEALTHY"
        _save_report(vault, run, report)
        _emit(report, args)
    return EXIT_HEALTHY


def _capabilities() -> dict:
    checks = [
        {
            "id": check.id,
            "description": check.description,
            "default": True,
            "severity": check.severity,
            "auto_fix": check.fixer is not None,
            "fixer_id": check.fixer,
            "risk": check.risk,
        }
        for check in CHECKS
    ]
    return {
        "schema_version": SCHEMA_VERSION,
        "contract_version": "1.0.0",
        "tool": "darsay",
        "tool_version": __version__,
        "doctor_version": "1.0.0",
        "identity": {"name": "darsay", "version": __version__},
        "platforms": ["linux", "macos"],
        "privilege": "current user only; privilege escalation is never attempted",
        "supported_subcommands": [
            "diagnose",
            "fix",
            "undo",
            "explain",
            "capabilities",
            "health",
            "robot-docs",
            "ls",
            "diff",
            "gc",
        ],
        "checks": checks,
        "detectors": checks,
        "fixers": [
            {"id": fixer, "check_id": check, "default": True, "risk": "low"}
            for fixer, check in sorted(FIXERS.items())
        ],
        "side_effects": {
            "diagnose": ["creates private evidence under <vault>/.doctor"],
            "fix": [
                "regenerates derived bundle README.md files",
                "quarantines stale transfer.lock and hydration.json files",
            ],
            "never": [
                "changes payload bytes",
                "changes curation.md",
                "uses the network",
                "escalates privileges",
            ],
        },
        "write_scope": [
            "<vault>/.doctor/**",
            "<vault>/*/*/README.md",
            "<vault>/*/*/transfer.lock",
            "<vault>/*/*/hydration.json",
        ],
        "network": {"default": "disabled", "checks": []},
        "environment_variables": [
            {
                "name": "DARSAY_HOME",
                "purpose": "default vault path",
                "secret": False,
            },
            {
                "name": "DARSAY_CONFIG",
                "purpose": "explicit operator configuration path",
                "secret": False,
            },
            {
                "name": "XDG_CONFIG_HOME",
                "purpose": "base for default operator configuration",
                "secret": False,
            },
        ],
        "report_schema": {
            "version": SCHEMA_VERSION,
            "required": [
                "schema_version",
                "run_id",
                "status",
                "summary",
                "findings",
                "exit_code",
                "artifacts_created",
                "network_attempts",
                "target_actions",
            ],
        },
        "run_artifacts": [
            "run.json",
            "report.json",
            "report.md",
            "scorecard.json",
            "actions.jsonl",
            "backups/",
            "quarantine/",
            "stdout.json",
            "stderr.log",
            "undo.sh",
        ],
        "timeouts": {"default_budget_seconds": None, "health_target_ms": 200},
        "exit_codes": {
            "0": "healthy or requested mutation completed",
            "1": "findings detected",
            "2": "partial diagnosis or unresolved findings after fix",
            "3": "fix failed and rollback was attempted",
            "4": "unsafe mutation or undo refused",
            "5": "another doctor mutation holds the lock",
            "6": "online diagnostics unavailable",
            "64": "usage error",
            "66": "input or run not found",
            "73": "cannot create artifact or lock",
            "74": "I/O error",
        },
    }


def _emit_value(value: Any, args) -> None:
    if getattr(args, "json", False):
        print(json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True))
    elif getattr(args, "quiet", False):
        return
    elif isinstance(value, str):
        print(value)
    else:
        print(json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True))


def _health(args, vault: Path) -> int:
    started = time.monotonic()
    problem = None
    doctor_root = _doctor_root(vault)
    runs_root = _runs_root(vault)
    doctor_lock = doctor_root / "doctor.lock"
    if not vault.exists():
        problem = "vault root does not exist"
    elif not vault.is_dir():
        problem = "vault root is not a directory"
    elif not os.access(vault, os.R_OK | os.X_OK):
        problem = "vault root is not readable"
    elif doctor_root.is_symlink() or runs_root.is_symlink():
        problem = "doctor evidence path contains a symlink"
    elif doctor_root.exists() and not doctor_root.is_dir():
        problem = "doctor root is not a directory"
    elif runs_root.exists() and not runs_root.is_dir():
        problem = "doctor runs root is not a directory"
    elif doctor_lock.is_symlink():
        problem = "doctor lock is a symlink"
    elif doctor_lock.exists() and not doctor_lock.is_file():
        problem = "doctor lock is not a regular file"
    value = {
        "schema_version": SCHEMA_VERSION,
        "status": "degraded" if problem else "healthy",
        "exit_code": EXIT_FINDINGS if problem else EXIT_HEALTHY,
        "target": str(vault),
        "detail": problem,
        "artifacts_created": False,
        "network_attempts": 0,
        "target_actions": 0,
        "elapsed_ms": round((time.monotonic() - started) * 1000, 3),
    }
    if getattr(args, "json", False):
        _emit_value(value, args)
    elif not getattr(args, "quiet", False):
        print(
            f"darsay doctor health: {value['status']}"
            + (f" — {problem}" if problem else "")
        )
    return EXIT_FINDINGS if problem else EXIT_HEALTHY


def _explain(args) -> int:
    wanted = getattr(args, "check_id", None)
    checks = [CHECK_BY_ID[wanted]] if wanted in CHECK_BY_ID else list(CHECKS)
    if wanted and wanted not in CHECK_BY_ID:
        raise DoctorError(f"unknown doctor check: {wanted}", EXIT_USAGE)
    value = [
        {
            "id": check.id,
            "description": check.description,
            "severity": check.severity,
            "auto_fixable": check.fixer is not None,
            "fixer_id": check.fixer,
            "risk": check.risk,
        }
        for check in checks
    ]
    if getattr(args, "json", False):
        _emit_value(value, args)
    elif not getattr(args, "quiet", False):
        for row in value:
            print(f"{row['id']} [{row['severity']}]: {row['description']}")
            print(
                "  Repair: "
                + (
                    f"{row['fixer_id']} ({row['risk']} risk)"
                    if row["auto_fixable"]
                    else "manual only"
                )
            )
    return EXIT_HEALTHY


def _robot_docs(args) -> int:
    text = """# darsay doctor robot contract (schema_version 1)

COMMANDS
  darsay doctor [diagnose]       Offline diagnosis; writes private evidence only.
  darsay doctor fix              Apply allowlisted low-risk repairs.
  darsay doctor undo RUN_ID      Byte-exact guarded inverse; latest is accepted.
  darsay doctor explain [CHECK]  Explain checks and repair policy.
  darsay doctor capabilities     Reflect checks, fixers, scope, and schemas.
  darsay doctor health           Shallow liveness probe; creates no artifacts.
  darsay doctor robot-docs       Print this contract.
  darsay doctor ls               List evidence runs.
  darsay doctor diff [RUN_ID]    Compare finding IDs with latest.
  darsay doctor gc --before DATE --yes  Explicitly delete old evidence only.

DIAGNOSE/FIX FLAGS
  --json --quiet --verbose --no-color --no-progress
  --fix --dry-run --only CHECK --skip CHECK --since ISO_TIMESTAMP
  --online --explain --severity LEVEL --budget SECONDS --quick
  --force --yes --robot-triage

AUTOMATION
  Use `darsay doctor capabilities --json` for discovery.
  Use `darsay doctor --json`; exit 1 means findings.
  Use `darsay doctor --robot-triage` for a JSON repair plan; it NEVER fixes.
  Inspect auto_fixable/fixer_id, then use `darsay doctor --fix --json`.
  Repairs are offline, locked, journaled, and reversible.
  Exit 4 with "incomplete doctor mutation" requires the printed strict undo
  command before new diagnosis, repair, or evidence garbage collection.

EXAMPLES
  darsay doctor health --json
  darsay doctor --json
  darsay doctor --robot-triage
  darsay doctor --fix --only bundle.readme --json
  darsay doctor undo latest --strict --json
  darsay doctor explain bundle.payload --json

EXIT CODES
  0 healthy or requested mutation completed
  1 findings detected
  2 partial diagnosis or unresolved findings after fix
  3 fix failed and rollback was attempted
  4 unsafe mutation or undo refused
  5 another doctor mutation holds the lock; retry later
  6 requested online diagnostics are unavailable
  64 usage error; 66 input/run not found; 73 cannot create; 74 I/O error

NEVER do anything to payload bytes or curation.md, follow symlinks for repair,
use network access by default, run destructive shell commands, write outside
the declared vault scope, or escalate privileges. The explicit `gc --yes`
command is the only evidence-deletion exception. Do not parse human output.
"""
    _emit_value(text.rstrip(), args)
    return EXIT_HEALTHY


def _list_runs(args, vault: Path) -> int:
    _validate_evidence_tree(vault, create=False)
    rows = []
    for run in (
        sorted(_runs_root(vault).glob("*"), reverse=True)
        if _runs_root(vault).is_dir()
        else []
    ):
        if not run.is_dir():
            continue
        try:
            meta = json.loads((run / "run.json").read_text(encoding="utf-8"))
            report = json.loads((run / "report.json").read_text(encoding="utf-8"))
            status = report.get("status")
        except (OSError, json.JSONDecodeError):
            meta, status = {}, "incomplete"
        rows.append(
            {
                "run_id": run.name,
                "created_at": meta.get("created_at"),
                "command": meta.get("command"),
                "status": status,
            }
        )
    if getattr(args, "json", False):
        _emit_value(rows, args)
    elif not getattr(args, "quiet", False):
        if not rows:
            print("No doctor runs.")
        for row in rows:
            print(f"{row['run_id']}  {row['command'] or '?':8}  {row['status']}")
    return EXIT_HEALTHY


def _diff(args, vault: Path) -> int:
    runs = [path for path in sorted(_runs_root(vault).glob("*")) if path.is_dir()]
    if not runs:
        raise DoctorError("no doctor runs to compare", EXIT_NOINPUT)
    newest = _resolve_run(vault, "latest")
    if args.run_ref:
        older = _resolve_run(vault, args.run_ref)
    else:
        candidates = [path for path in runs if path.resolve() != newest.resolve()]
        if not candidates:
            raise DoctorError("need at least two doctor runs to diff", EXIT_NOINPUT)
        older = candidates[-1]
    try:
        old_report = json.loads((older / "report.json").read_text(encoding="utf-8"))
        new_report = json.loads((newest / "report.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DoctorError(f"cannot read doctor reports: {exc}", EXIT_NOINPUT) from exc
    old_ids = {f["id"] for f in old_report.get("findings", [])}
    new_ids = {f["id"] for f in new_report.get("findings", [])}
    value = {
        "schema_version": SCHEMA_VERSION,
        "from": older.name,
        "to": newest.name,
        "new": sorted(new_ids - old_ids),
        "resolved": sorted(old_ids - new_ids),
        "persisting": sorted(old_ids & new_ids),
    }
    _emit_value(value, args)
    return EXIT_FINDINGS if value["new"] or value["persisting"] else EXIT_HEALTHY


def mutate_gc(run: Path) -> None:
    """Destructive evidence-retention chokepoint, gated by ``gc --yes``."""
    shutil.rmtree(run)


def _gc(args, vault: Path) -> int:
    _validate_evidence_tree(vault, create=False)
    _assert_no_unresolved_runs(vault)
    if not args.yes:
        raise DoctorError("doctor gc requires --yes", EXIT_UNSAFE)
    try:
        before = dt.datetime.fromisoformat(args.before).replace(tzinfo=dt.timezone.utc)
    except ValueError as exc:
        raise DoctorError(
            "--before must be an ISO date such as 2026-01-31", EXIT_USAGE
        ) from exc
    latest = None
    with contextlib.suppress(DoctorError):
        latest = _resolve_run(vault, "latest").resolve()
    removed = []
    for run in (
        sorted(_runs_root(vault).glob("*")) if _runs_root(vault).is_dir() else []
    ):
        if not run.is_dir() or (latest is not None and run.resolve() == latest):
            continue
        try:
            meta = json.loads((run / "run.json").read_text(encoding="utf-8"))
            created = dt.datetime.fromisoformat(meta["created_at"])
        except (OSError, json.JSONDecodeError, KeyError, ValueError):
            continue
        if created < before:
            mutate_gc(run)
            removed.append(run.name)
    _emit_value(
        {"removed": removed, "kept_latest": latest.name if latest else None}, args
    )
    return EXIT_HEALTHY


def run(args, vault: Path) -> int:
    """Dispatch a parsed ``darsay doctor`` namespace."""
    vault = vault.expanduser().resolve(strict=False)
    command = getattr(args, "doctor_command", None) or "diagnose"
    if command == "diagnose":
        return _diagnose(args, vault)
    if command == "fix":
        return _diagnose(args, vault, force_fix=True)
    if command == "undo":
        return _undo(args, vault)
    if command == "capabilities":
        _emit_value(_capabilities(), args)
        return EXIT_HEALTHY
    if command == "health":
        return _health(args, vault)
    if command == "explain":
        return _explain(args)
    if command == "robot-docs":
        return _robot_docs(args)
    if command == "ls":
        return _list_runs(args, vault)
    if command == "diff":
        return _diff(args, vault)
    if command == "gc":
        with _mutation_lock(vault):
            return _gc(args, vault)
    raise DoctorError(f"unknown doctor command: {command}", EXIT_USAGE)

"""The far side: hash a payload on the host that owns its disk.

A vault on a network mount is a folder here and a disk somewhere else.
Reading it back to hash it pulls every byte over the wire — the thing a
follow-up to rsync must never do silently. When the vault's own
``config.toml`` names the host that owns the disk and the vault's path
there (``[host] ssh`` / ``path``), darsay hashes *on that host* instead:
one ssh call, a POSIX shell script on stdin, one ``sha256  size  path``
line back per file. The host needs ``sh``, ``find``, and one of
``sha256sum`` / ``shasum`` / ``sha256`` / ``openssl`` — what a NAS, a
BSD, or a Mac has. Not Python, not rsync, not darsay.

``hash_where_it_lives`` is the one door: ``verify``, ``mv``, and ``cp``
ask it, and it decides between here and there from the vault's config.
A far side that cannot be reached is a refusal that names the fix, never
a silent fall back to the wire. (``assemble --rehash`` hashes through the
transfer pipeline and still reads here.)
"""

from __future__ import annotations

import posixpath
import re
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path

# Files at least this large announce themselves while hashing, so a
# multi-gigabyte shard is not minutes of silence.
_ANNOUNCE_BYTES = 256 * 1024**2

# What runs on the far side. Sent on stdin to ``sh -s``; DIR then optional
# FILEs relative to it (none = every file, tool caches skipped). A file
# that is not there is not printed, so the caller sees it as missing.
FAR_SIDE_SH = r"""#!/bin/sh
# darsay's far side: hash payload files where the disk is.
# usage: sh -s -- DIR [FILE...]   prints: <sha256> TAB <size> TAB <path>
set -eu
cd "$1"
shift
if command -v sha256sum >/dev/null 2>&1; then hasher() { sha256sum "$1" | cut -c1-64; }
elif command -v shasum >/dev/null 2>&1; then hasher() { shasum -a 256 "$1" | cut -c1-64; }
elif command -v sha256 >/dev/null 2>&1; then hasher() { sha256 -q "$1"; }
elif command -v openssl >/dev/null 2>&1; then hasher() { openssl dgst -sha256 "$1" | sed 's/.*= //'; }
else echo "no sha256 tool on this host" >&2; exit 3; fi
one() { printf '%s\t%s\t%s\n' "$(hasher "$1")" "$(wc -c < "$1" | tr -d ' ')" "$1"; }
if [ "$#" -gt 0 ]; then
  for f in "$@"; do [ -f "$f" ] && one "$f"; done
else
  find . -path ./.cache -prune -o -type f -print | sed 's|^\./||' | LC_ALL=C sort | while IFS= read -r f; do one "$f"; done
fi
"""

_NO_SHA256_TOOL = 3
_SSH_FAILED = 255
_LINE = re.compile(r"^([0-9a-f]{64})\t(\d+)\t(.+)$")


@dataclass(frozen=True)
class FarSide:
    """The host that owns a vault's disk, and the vault's path there."""

    ssh: str
    path: str

    def where(self, vault: Path, local_dir: Path) -> str:
        """``local_dir``, which is under ``vault`` here, as a path on the host."""
        rel = Path(local_dir).resolve().relative_to(Path(vault).resolve()).as_posix()
        return posixpath.join(self.path, rel) if rel != "." else self.path


def far_side_for(vault: Path | None) -> FarSide | None:
    """The far side the vault's ``config.toml`` names, or ``None``."""
    if vault is None:
        return None
    from .config import setting, vault_config_path

    ssh = setting("host", "ssh", vault)
    path = setting("host", "path", vault)
    if not ssh and not path:
        return None
    if not ssh or not path:
        missing = "path" if ssh else "ssh"
        raise SystemExit(
            f"error: [host] in {vault_config_path(vault)} needs both ssh and path; "
            f"host.{missing} is not set\n"
            f"  hint: darsay --vault {shlex.quote(str(vault))} config "
            "host.ssh=root@nas host.path=/volume1/darsay/vault"
        )
    return FarSide(ssh=str(ssh), path=str(path))


def far_side_hash(
    far: FarSide,
    dir_on_host: str,
    files: list[str] | None = None,
    *,
    vault: Path | None = None,
    progress=print,
) -> dict[str, dict]:
    """sha256 and size per file under ``dir_on_host``, read on the far side.

    Keys are paths relative to the directory. A file the host cannot find
    is absent. Any failure to run there is a refusal naming the fix.
    """
    from .readme_gen import human_size

    argv = [
        "ssh",
        "-o",
        "ConnectTimeout=15",
        far.ssh,
        "sh",
        "-s",
        "--",
        shlex.quote(dir_on_host),
        *(shlex.quote(name) for name in files or ()),
    ]
    try:
        proc = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except FileNotFoundError:
        raise SystemExit(
            f"error: cannot hash on {far.ssh}: ssh is not installed here"
        ) from None
    assert proc.stdin is not None and proc.stdout is not None
    proc.stdin.write(FAR_SIDE_SH)
    proc.stdin.close()
    out: dict[str, dict] = {}
    for line in proc.stdout:
        match = _LINE.match(line.rstrip("\n"))
        if match is None:
            proc.kill()
            raise SystemExit(
                f"error: cannot hash on {far.ssh}: unexpected output from the far "
                f"side: {line.rstrip()!r}"
            )
        digest, size, rel = match.group(1), int(match.group(2)), match.group(3)
        out[rel] = {"sha256": digest, "size": size}
        if size >= _ANNOUNCE_BYTES:
            progress(f"  {rel}  ({human_size(size)})  hashed on {far.ssh}")
    stderr = proc.stderr.read().strip() if proc.stderr is not None else ""
    code = proc.wait()
    if code == 0:
        return out
    if code == _NO_SHA256_TOOL:
        why = f"{far.ssh} has no sha256sum, shasum, sha256, or openssl"
    elif code == _SSH_FAILED:
        why = stderr.splitlines()[-1] if stderr else "ssh could not connect"
    else:
        why = f"the far side exited {code}" + (f": {stderr}" if stderr else "")
    from .config import vault_config_path

    where = (
        f"[host] in {vault_config_path(vault)} names it as the host that owns the "
        "disk. Fix that, or drop the table to hash over the wire."
        if vault is not None
        else "Fix the connection, or drop [host] from the vault's config.toml."
    )
    raise SystemExit(f"error: cannot hash on {far.ssh}: {why}\n  {where}")


def hash_where_it_lives(
    vault: Path | None,
    payload_dir: Path,
    files: list[str] | None = None,
    *,
    progress=print,
) -> dict[str, dict]:
    """sha256 and size per payload file, read wherever the disk actually is.

    Keys are paths relative to ``payload_dir``; ``files`` narrows the pass
    to those paths (a file that is not there is absent from the result).
    On the host the vault's config names when it names one, else here.
    """
    from .hashing import hash_file, iter_payload_files
    from .readme_gen import human_size

    far = far_side_for(vault)
    if far is not None:
        return far_side_hash(
            far, far.where(vault, payload_dir), files, vault=vault, progress=progress
        )
    if files is None:
        pairs = iter_payload_files(payload_dir)
    else:
        pairs = [(rel, payload_dir / rel) for rel in files]
    out: dict[str, dict] = {}
    for rel, path in pairs:
        if not path.is_file():
            continue
        size = path.stat().st_size
        if size >= _ANNOUNCE_BYTES:
            progress(f"  {rel}  ({human_size(size)})  hashing")
        out[rel] = {
            "sha256": hash_file(path, with_blake3=False)["sha256"],
            "size": size,
        }
    return out


def far_side_label(vault: Path | None) -> str | None:
    """``"on root@nas"`` when the vault names a far side, for a plan line."""
    far = far_side_for(vault)
    return f"on {far.ssh}" if far is not None else None


def far_side_guess(source: str | None) -> str | None:
    """An ssh destination read off a network mount's source, or ``None``.

    ``//jeremy@pixel._smb._tcp.local/darsay`` → ``jeremy@pixel``;
    ``nas:/export/vault`` → ``nas``. A hint for the config line, never a
    fact darsay acts on.
    """
    if not source:
        return None
    if source.startswith("//"):
        host = source[2:].split("/", 1)[0]
    elif ":" in source and not source.startswith("/"):
        host = source.split(":", 1)[0]
    else:
        return None
    host = re.sub(r"\._smb\._tcp\.local$", "", host)
    return host or None

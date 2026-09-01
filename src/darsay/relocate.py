"""Relocate or replicate a registered bundle into another vault: ``mv`` / ``cp``.

rsync (or ``cp -a``, a USB stick, a restic restore) into the two-level
vault layout is, and will remain, a first-class copy of a bundle — see
``docs/INCREMENTAL.md`` §1. These two verbs are that same contract with
the bookkeeping folded in: copy, re-hash the copy *where it landed*,
rewrite the manifest, and only then touch anything else. ``mv`` removes
the source afterwards (and is a rename on one filesystem, where nothing
is rewritten and so nothing is re-hashed); ``cp`` keeps the source and
records the new copy as a replica in both manifests.

Both act on *registered* bundles only. A partial's verified bytes cross
vaults with ``assemble --handoff`` (per file, leaving a skeleton); that
verb never touches a registered payload and these never touch a partial.
"""

from __future__ import annotations

import errno
import os
import shutil
import socket
from pathlib import Path

# Bundle-root files that describe *this* vault or *this* process, not the
# bundle. They stay behind; everything else (payload, manifest, curation,
# generated views, the disposable-but-portable transfer ledger, export log)
# travels.
LEAVE_BEHIND = frozenset({"hydration.json", "transfer.lock"})

# Files at least this large announce themselves while copying, so a
# multi-gigabyte shard is not minutes of silence.
_ANNOUNCE_BYTES = 256 * 1024**2

_VERBS = {
    "mv": {"would": "Would move", "doing": "Moving", "does": "relocates"},
    "cp": {"would": "Would copy", "doing": "Copying", "does": "copies"},
}


def _copy_file(source: Path, destination: Path) -> str:
    """One file, clonefile where the filesystem offers it; returns the method."""
    from .transfer import _copy_local_file

    return _copy_local_file(source, destination)


def bundle_files(bundle_dir: Path) -> list[tuple[str, Path]]:
    """Every regular file the verbs carry, as ``(relative posix path, path)``.

    Symlinks are refused rather than silently materialized or dropped: a
    bundle never contains one, and a hand-made one is a decision for the
    curator (``rsync -a`` keeps it as a link).
    """
    files = []
    for path in sorted(bundle_dir.rglob("*")):
        rel = path.relative_to(bundle_dir).as_posix()
        if path.is_symlink():
            raise SystemExit(
                f"error: {path} is a symlink — mv/cp copy regular files only; "
                "relocate this bundle with rsync -a if the link is intended"
            )
        if not path.is_file() or path.name == ".DS_Store" or rel in LEAVE_BEHIND:
            continue
        files.append((rel, path))
    return files


def _same_device(a: Path, b: Path) -> bool:
    """Whether a rename between ``a`` and the nearest existing parent of ``b`` can work."""
    probe = b.resolve()
    while not probe.exists():
        probe = probe.parent
    try:
        return os.stat(a).st_dev == os.stat(probe).st_dev
    except OSError:
        return False


def relocation_plan(
    bundle_dir: Path, dest_vault: Path, *, force: bool = False, op: str = "mv"
) -> dict:
    """Resolve where a bundle would land and how, running every refusal.

    Raises ``SystemExit`` for the cases the real command would refuse, so a
    dry run and the command itself say the same thing.
    """
    from .archiver import load_manifest
    from .config import free_space_floor
    from .transfer import disk_verdict, is_network_filesystem

    words = _VERBS[op]
    bundle_dir = Path(bundle_dir)
    if not (bundle_dir / "manifest.json").is_file():
        raise SystemExit(
            f"error: {bundle_dir} is a partial, not a registered bundle — {op} "
            f"{words['does']} registered bundles.\n"
            "  Continue a partial in the other vault after an rsync "
            "(`darsay --vault OTHER archive SOURCE`), or hand its verified "
            f"bytes over with `darsay --vault OTHER assemble {bundle_dir} --handoff`."
        )
    manifest = load_manifest(bundle_dir)
    bundle_id = manifest["bundle_id"]
    name, _, rev = bundle_id.partition("@")
    if not rev:
        raise SystemExit(f"error: malformed bundle_id in manifest: {bundle_id}")

    dest_vault = Path(dest_vault).expanduser()
    if not dest_vault.is_dir():
        raise SystemExit(
            f"error: destination vault does not exist: {dest_vault}\n"
            f"  {op} does not create vaults: an unmounted disk must not become a "
            "folder on this one. Create it, or mount it, and rerun."
        )
    dest = dest_vault / name / rev
    source_real = bundle_dir.resolve()
    dest_real = dest.resolve()
    if dest_real == source_real:
        raise SystemExit(f"error: {bundle_id} is already in {dest_vault}")
    if dest_real.is_relative_to(source_real) or source_real.is_relative_to(dest_real):
        raise SystemExit(
            f"error: {dest} and {bundle_dir} nest inside each other — refusing"
        )
    dest_exists = dest.exists()
    if dest_exists and not force:
        raise SystemExit(f"error: {dest} already exists (use --force to replace)")

    files = bundle_files(bundle_dir)
    total = sum(path.stat().st_size for _rel, path in files)
    method = "rename" if op == "mv" and _same_device(bundle_dir, dest) else "copy"
    floor = free_space_floor(dest_vault)
    probe = dest_real
    while not probe.exists():
        probe = probe.parent
    free = shutil.disk_usage(probe).free
    needed = 0 if method == "rename" else total
    return {
        "op": op,
        "bundle_id": bundle_id,
        "source": bundle_dir,
        "dest": dest,
        "dest_exists": dest_exists,
        "force": force,
        "method": method,
        "files": len(files),
        "bytes": total,
        "payload_files": len(manifest["inventory"]["files"]),
        "leaves_behind": sorted(
            left for left in LEAVE_BEHIND if (bundle_dir / left).exists()
        ),
        "network": bool(is_network_filesystem(dest_vault)),
        "disk": {
            "checked_path": str(probe),
            "free_bytes": free,
            "needed_bytes": needed,
            "min_free_bytes": floor,
            "verdict": disk_verdict(free, needed, floor),
        },
    }


def move_plan(bundle_dir: Path, dest_vault: Path, *, force: bool = False) -> dict:
    return relocation_plan(bundle_dir, dest_vault, force=force, op="mv")


def copy_plan(bundle_dir: Path, dest_vault: Path, *, force: bool = False) -> dict:
    return relocation_plan(bundle_dir, dest_vault, force=force, op="cp")


def print_relocation_plan(plan: dict, progress=print, *, dry_run: bool) -> None:
    from .readme_gen import human_size

    words = _VERBS[plan["op"]]
    progress(f"{words['would'] if dry_run else words['doing']} {plan['bundle_id']}")
    progress(f"  from:     {plan['source']}")
    dest_note = "  (exists — --force replaces it)" if plan["dest_exists"] else "  (new)"
    progress(f"  to:       {plan['dest']}{dest_note}")
    if plan["method"] == "rename":
        progress(
            "  how:      rename in place — same filesystem; bytes untouched, "
            "not re-hashed"
        )
    else:
        tail = (
            "then remove the source"
            if plan["op"] == "mv"
            else "and keep the source; both manifests record the replica"
        )
        progress(
            f"  how:      copy {plan['files']} files ({human_size(plan['bytes'])}), "
            f"re-hash the {plan['payload_files']} payload files at the "
            f"destination, {tail}"
        )
        disk = plan["disk"]
        floor = disk.get("min_free_bytes")
        floor_note = f" ({human_size(floor)} floor)" if floor else ""
        progress(
            f"  disk:     needs {human_size(disk['needed_bytes'])}, "
            f"free {human_size(disk['free_bytes'])}{floor_note} at "
            f"{disk['checked_path']} — {disk['verdict'].upper()}"
        )
    if plan["leaves_behind"]:
        progress(
            f"  leaves:   {', '.join(plan['leaves_behind'])} — vault-local; "
            "`darsay hydrate` again at the destination"
        )
    if plan["network"] and plan["method"] == "copy":
        then_rm = ", then `darsay rm` the source" if plan["op"] == "mv" else ""
        progress(
            "  warning:  the destination is on a network mount, so verifying "
            "the copy reads every byte back over the wire. For a large bundle "
            "prefer: rsync it, `darsay verify` on the host that owns the disk"
            f"{then_rm}."
        )


print_move_plan = print_relocation_plan


def _refuse_if_insufficient(plan: dict) -> None:
    if plan["method"] == "copy" and plan["disk"]["verdict"] == "insufficient":
        raise SystemExit(
            "error: not enough free space at the destination for the copy "
            "(the free-space floor is `darsay config`'s transfer.min_free)"
        )


def _try_rename(source: Path, dest: Path, *, force: bool) -> bool:
    """Rename ``source`` onto ``dest``; ``False`` when the filesystems differ."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    if force and dest.exists():
        shutil.rmtree(dest)
    try:
        os.rename(source, dest)
    except OSError as exc:
        if exc.errno == errno.EXDEV:
            return False
        raise
    return True


def _copy_and_verify(
    source: Path, dest: Path, plan: dict, *, force: bool, progress, stamp
) -> tuple[dict, int]:
    """Copy into a staging directory beside ``dest``, verify there, then rename.

    The staging directory is one level deeper than the vault's
    ``<name>/<rev>`` layout, so ``darsay list`` never sees a half-copied
    bundle; a failure removes it and leaves the source untouched.
    ``stamp(staging)`` rewrites the copy's manifest once it has verified,
    right before it is renamed into place. Returns the verification report
    and how many files arrived as copy-on-write clones.
    """
    from .readme_gen import human_size
    from .verify import verify_bundle

    rev = dest.name
    staging_root = dest.parent / f".{plan['op']}-{rev}"
    staging = staging_root / rev
    shutil.rmtree(staging_root, ignore_errors=True)
    staging.mkdir(parents=True)
    cloned = 0
    try:
        progress(
            f"Transferring {plan['files']} files ({human_size(plan['bytes'])}) ..."
        )
        for rel, path in bundle_files(source):
            size = path.stat().st_size
            if size >= _ANNOUNCE_BYTES:
                progress(f"  {rel}  ({human_size(size)})")
            if _copy_file(path, staging / rel) == "clonefile":
                cloned += 1
        progress("Verifying the copy at the destination ...")
        report = verify_bundle(staging, progress=progress)
        checksum = report["checksum"]
        if checksum["status"] != "pass":
            raise SystemExit(
                "error: verification FAILED at the destination — nothing "
                f"{'moved' if plan['op'] == 'mv' else 'copied'}, source untouched "
                f"({len(checksum['mismatched'])} modified, "
                f"{len(checksum['missing'])} missing, {len(checksum['extra'])} extra)"
            )
        stamp(staging)
        if force and dest.exists():
            shutil.rmtree(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        staging.rename(dest)
    finally:
        shutil.rmtree(staging_root, ignore_errors=True)
    return report, cloned


def _stamp_new_home(
    bundle_dir: Path, home: Path, came_from: Path, *, method: str
) -> None:
    """Record a move in the manifest and regenerate the views that name the path."""
    from .archiver import load_manifest, utc_now, write_manifest
    from .readme_gen import write_bundle_readme

    manifest = load_manifest(bundle_dir)
    now = utc_now()
    archive = manifest["archive"]
    archive.setdefault("moves", []).append(
        {
            "at": now,
            "from_location": str(came_from.resolve()),
            "method": method,
        }
    )
    archive["location"] = str(home.resolve())
    archive["host"] = socket.gethostname()
    archive["last_accessed"] = now
    write_manifest(bundle_dir, manifest)
    write_bundle_readme(bundle_dir, manifest)


def _stamp_replica(bundle_dir: Path, *, home: Path, other: Path, now: str) -> None:
    """Record that a verified copy of this bundle lives at ``other``.

    Written on both sides of a ``cp``: the copy learns where it came from,
    the source learns where its replica went. Entries are keyed by
    location — refreshing a backup with ``--force`` updates the timestamp
    rather than listing the same disk twice.
    """
    from .archiver import load_manifest, write_manifest
    from .readme_gen import write_bundle_readme

    manifest = load_manifest(bundle_dir)
    archive = manifest["archive"]
    home_loc = str(home.resolve())
    other_loc = str(other.resolve())
    replicas = [
        entry
        for entry in (archive.get("replicas") or [])
        if entry.get("location") not in (home_loc, other_loc)
    ]
    replicas.append({"at": now, "location": other_loc, "host": socket.gethostname()})
    archive["replicas"] = replicas
    archive["backup_status"] = "replicated"
    archive["location"] = home_loc
    archive["host"] = socket.gethostname()
    archive["last_accessed"] = now
    write_manifest(bundle_dir, manifest)
    write_bundle_readme(bundle_dir, manifest)


def _remove_source(bundle_dir: Path) -> None:
    from .vault import prune_empty_parent

    shutil.rmtree(bundle_dir)
    prune_empty_parent(bundle_dir)


def move_bundle(
    bundle_dir: Path,
    dest_vault: Path,
    *,
    force: bool = False,
    progress=print,
    dry_run: bool = False,
) -> Path:
    """Move a registered bundle into ``dest_vault``; returns its new directory.

    Same filesystem: a rename, then the manifest's ``archive.location`` and
    the generated views are refreshed. Different filesystems: copy into a
    staging directory, re-hash every payload file there against the
    manifest, stamp the new home, rename into place, and only then remove
    the source. A failed verification removes the staging copy and leaves
    the source exactly as it was. ``dry_run`` resolves and reports and
    touches nothing.
    """
    from .transfer import transfer_lock
    from .vault import prune_empty_parent
    from .verify import refresh_verification_md

    bundle_dir = Path(bundle_dir)
    plan = relocation_plan(bundle_dir, dest_vault, force=force, op="mv")
    print_relocation_plan(plan, progress, dry_run=dry_run)
    dest = plan["dest"]
    if dry_run:
        return dest
    _refuse_if_insufficient(plan)

    with transfer_lock(bundle_dir, progress=progress):
        renamed = plan["method"] == "rename" and _try_rename(
            bundle_dir, dest, force=force
        )
        if renamed:
            # Our own lock travelled with the directory; it is released here.
            (dest / "transfer.lock").unlink(missing_ok=True)
            for name in LEAVE_BEHIND:
                (dest / name).unlink(missing_ok=True)
            _stamp_new_home(dest, dest, bundle_dir, method="rename")
            refresh_verification_md(dest)
            prune_empty_parent(bundle_dir)
            progress(f"Moved {plan['bundle_id']} → {dest}  (renamed; bytes untouched)")
            return dest
        report, _cloned = _copy_and_verify(
            bundle_dir,
            dest,
            plan,
            force=force,
            progress=progress,
            stamp=lambda staging: _stamp_new_home(
                staging, dest, bundle_dir, method="copy"
            ),
        )
        refresh_verification_md(dest)
        _remove_source(bundle_dir)
    progress(
        f"Moved {plan['bundle_id']} → {dest}  "
        f"({report['checksum']['files_checked']} payload files verified at the "
        "destination before the source was removed)"
    )
    return dest


def copy_bundle(
    bundle_dir: Path,
    dest_vault: Path,
    *,
    force: bool = False,
    progress=print,
    dry_run: bool = False,
) -> Path:
    """Copy a registered bundle into ``dest_vault``; returns the copy's directory.

    Copy into a staging directory (copy-on-write clones where the
    filesystem offers them), re-hash every payload file there against the
    manifest, rename into place, then record the replica in *both*
    manifests (``archive.replicas``, ``backup_status: replicated``). The
    source payload is never touched; a failed verification removes the
    staging copy and records nothing anywhere. ``dry_run`` resolves and
    reports and touches nothing.
    """
    from .archiver import utc_now
    from .transfer import transfer_lock
    from .verify import refresh_verification_md

    bundle_dir = Path(bundle_dir)
    plan = relocation_plan(bundle_dir, dest_vault, force=force, op="cp")
    print_relocation_plan(plan, progress, dry_run=dry_run)
    dest = plan["dest"]
    if dry_run:
        return dest
    _refuse_if_insufficient(plan)

    now = utc_now()
    with transfer_lock(bundle_dir, progress=progress):
        report, cloned = _copy_and_verify(
            bundle_dir,
            dest,
            plan,
            force=force,
            progress=progress,
            stamp=lambda staging: _stamp_replica(
                staging, home=dest, other=bundle_dir, now=now
            ),
        )
        refresh_verification_md(dest)
        _stamp_replica(bundle_dir, home=bundle_dir, other=dest, now=now)
    clone_note = (
        " as copy-on-write clones — no extra space until they diverge"
        if cloned and cloned == plan["files"]
        else ""
    )
    progress(
        f"Copied {plan['bundle_id']} → {dest}  "
        f"({report['checksum']['files_checked']} payload files verified at the "
        f"destination{clone_note}; source kept, replica recorded in both manifests)"
    )
    return dest

"""Relocate or replicate a registered bundle into another vault: ``mv`` / ``cp``.

rsync (or ``cp -a``, a USB stick, a restic restore) into the two-level
vault layout is, and will remain, a first-class copy of a bundle — see
``docs/INCREMENTAL.md`` §1. These two verbs are that same contract with
the bookkeeping folded in: copy, re-hash the copy *where it landed*,
rewrite the manifest, and only then touch anything else. ``mv`` removes
the source afterwards (and is a rename on one filesystem, where nothing
is rewritten and so nothing is re-hashed); ``cp`` keeps the source and
records the new copy as a replica in both manifests.

A destination that already holds the bundle — an earlier rsync, a backup
being refreshed, a partial someone started there — is landed on, never
refused and never copied over from scratch. Every payload file already
there at its recorded size is hashed in place and kept when it matches;
only what is missing or differs is copied; the record travels from the
source. That is *adoption*, the same first step ``archive`` and
``assemble`` take over bytes they find on disk, and it is what makes
"rsync, then ``darsay mv``" cost one read of the destination and nothing
else.

Both act on *registered* bundles only. A partial's verified bytes cross
vaults with ``assemble --handoff`` (per file, leaving a skeleton); that
verb never touches a registered payload and these never touch a partial.
"""

from __future__ import annotations

import errno
import os
import shlex
import shutil
import socket
from pathlib import Path

# Bundle-root files that describe *this* vault or *this* process, not the
# bundle. They stay behind; everything else (payload, manifest, curation,
# generated views, the disposable-but-portable transfer ledger, export log)
# travels.
LEAVE_BEHIND = frozenset({"hydration.json", "transfer.lock"})

# What an rsync of a bundle should not carry: the two files above, and the
# Finder droppings that would count as an unlisted payload file at the
# destination. Never ``--delete``: a stray file at the destination is
# refused by name, and ``--delete`` pointed one directory too high empties
# a vault.
RSYNC_EXCLUDES = ("hydration.json", "transfer.lock", ".DS_Store")

# Files at least this large announce themselves while copying or hashing,
# so a multi-gigabyte shard is not minutes of silence.
_ANNOUNCE_BYTES = 256 * 1024**2

# How many of a destination's unlisted payload files a refusal names.
_SHOW_EXTRAS = 6

_VERBS = {
    "mv": {"would": "Would move", "doing": "Moving", "does": "relocates"},
    "cp": {"would": "Would copy", "doing": "Copying", "does": "copies"},
}


def _copy_file(source: Path, destination: Path) -> str:
    """One file, clonefile where the filesystem offers it; returns the method."""
    from .transfer import _copy_local_file

    return _copy_local_file(source, destination)


def rsync_command(source: Path, dest: Path) -> str:
    """The rsync line that puts ``source`` at ``dest`` the way ``mv`` would.

    ``-a`` keeps mtimes so a rerun skips what already arrived; ``-P`` keeps
    a shard that was cut off and shows progress; the excludes are the
    files a bundle does not carry across vaults. Trailing slashes on both
    sides: the contents of one directory into the other, whatever the
    destination's name.
    """
    excludes = " ".join(f"--exclude={name}" for name in RSYNC_EXCLUDES)
    return (
        f"rsync -aP {excludes} "
        f"{shlex.quote(str(Path(source).resolve()))}/ {shlex.quote(str(dest))}/"
    )


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


def _landing(source: Path, dest: Path, manifest: dict) -> dict:
    """What a destination that already holds this bundle has of it.

    A stat walk — path and size against the record — not a hash: the plan
    must stay cheap. Hashing happens once, in place, when the verb runs.
    """
    from .hashing import iter_payload_files
    from .schema import payload_root

    root = payload_root(manifest)
    expected = {r["path"]: r["size"] for r in manifest["inventory"]["files"]}
    there = (
        {
            f"{root}/{rel}": path.stat().st_size
            for rel, path in iter_payload_files(dest / root)
        }
        if (dest / root).is_dir()
        else {}
    )
    present = [p for p in expected if p in there and there[p] == expected[p]]
    resized = [p for p in expected if p in there and there[p] != expected[p]]
    missing = [p for p in expected if p not in there]
    curation = None
    theirs = dest / "curation.md"
    ours = source / "curation.md"
    if theirs.is_file():
        if not ours.is_file():
            curation = "kept"
        elif ours.read_bytes() != theirs.read_bytes():
            curation = "replaced"
    return {
        "present": len(present),
        "present_bytes": sum(expected[p] for p in present),
        "resized": len(resized),
        "missing": len(missing),
        "copy_bytes": sum(expected[p] for p in resized + missing),
        "extra": [(p, there[p]) for p in sorted(set(there) - set(expected))],
        "curation": curation,
    }


def _extras_refusal(
    dest: Path, dest_vault: Path, bundle_id: str, extra: list[tuple[str, int]]
) -> str:
    from .readme_gen import human_size

    plural = "s" if len(extra) != 1 else ""
    lines = [
        f"error: {dest} already holds {len(extra)} payload file{plural} this "
        "bundle's record does not list:"
    ]
    lines += [f"  {path}  ({human_size(size)})" for path, size in extra[:_SHOW_EXTRAS]]
    if len(extra) > _SHOW_EXTRAS:
        lines.append(f"  … and {len(extra) - _SHOW_EXTRAS} more")
    lines.append(
        "  That is another pin of the same revision, or a file put there by hand. "
        f"Keep one: `darsay --vault {dest_vault} rm {bundle_id} --yes` clears the "
        "destination and a rerun lands this bundle there; `darsay rm` here keeps "
        "that one."
    )
    return "\n".join(lines)


def relocation_plan(bundle_dir: Path, dest_vault: Path, *, op: str = "mv") -> dict:
    """Resolve where a bundle would land and how, running every refusal.

    Raises ``SystemExit`` for the cases the real command would refuse, so a
    dry run and the command itself say the same thing.
    """
    from .archiver import load_manifest
    from .config import free_space_floor
    from .farside import far_side_label
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
    if dest.exists() and not dest.is_dir():
        raise SystemExit(f"error: {dest} exists and is not a directory")
    dest_exists = dest.is_dir() and any(dest.iterdir())
    landing = None
    if dest_exists:
        landing = _landing(bundle_dir, dest, manifest)
        if landing["extra"]:
            raise SystemExit(
                _extras_refusal(dest, dest_vault, bundle_id, landing["extra"])
            )

    files = bundle_files(bundle_dir)
    total = sum(path.stat().st_size for _rel, path in files)
    inventory_paths = {r["path"] for r in manifest["inventory"]["files"]}
    beside_payload = sum(
        path.stat().st_size for rel, path in files if rel not in inventory_paths
    )
    if dest_exists:
        method = "adopt"
        needed = landing["copy_bytes"] + beside_payload
    elif op == "mv" and _same_device(bundle_dir, dest):
        method = "rename"
        needed = 0
    else:
        method = "copy"
        needed = total
    floor = free_space_floor(dest_vault)
    probe = dest_real
    while not probe.exists():
        probe = probe.parent
    free = shutil.disk_usage(probe).free
    return {
        "op": op,
        "bundle_id": bundle_id,
        "source": bundle_dir,
        "dest_vault": dest_vault,
        "dest": dest,
        "dest_exists": dest_exists,
        "landing": landing,
        "method": method,
        "files": len(files),
        "bytes": total,
        "payload_files": len(inventory_paths),
        "leaves_behind": sorted(
            left for left in LEAVE_BEHIND if (bundle_dir / left).exists()
        ),
        "network": bool(is_network_filesystem(dest_vault)),
        "far_side": far_side_label(dest_vault),
        "disk": {
            "checked_path": str(probe),
            "free_bytes": free,
            "needed_bytes": needed,
            "min_free_bytes": floor,
            "verdict": disk_verdict(free, needed, floor),
        },
    }


def move_plan(bundle_dir: Path, dest_vault: Path) -> dict:
    return relocation_plan(bundle_dir, dest_vault, op="mv")


def copy_plan(bundle_dir: Path, dest_vault: Path) -> dict:
    return relocation_plan(bundle_dir, dest_vault, op="cp")


def _how_adopt(plan: dict, tail: str) -> str:
    from .readme_gen import human_size

    landing = plan["landing"]
    total = plan["payload_files"]
    to_copy = landing["missing"] + landing["resized"]
    cost = human_size(landing["copy_bytes"])
    where = plan["far_side"] or "in place"
    if landing["present"] and to_copy:
        return (
            f"{landing['present']} of {total} payload files are already there at "
            f"the recorded size — hash them {where}, copy the other {to_copy} "
            f"({cost}), {tail}"
        )
    if landing["present"]:
        return (
            f"all {total} payload files are already there at the recorded size — "
            f"hash them {where}, copy nothing, {tail}"
        )
    return (
        f"none of the {total} payload files is there at the recorded size — "
        f"copy all {total} ({cost}), {tail}"
    )


def print_relocation_plan(plan: dict, progress=print, *, dry_run: bool) -> None:
    from .readme_gen import human_size

    words = _VERBS[plan["op"]]
    progress(f"{words['would'] if dry_run else words['doing']} {plan['bundle_id']}")
    progress(f"  from:     {plan['source']}")
    dest_note = "  (exists)" if plan["dest_exists"] else "  (new)"
    progress(f"  to:       {plan['dest']}{dest_note}")
    tail = (
        "then remove the source"
        if plan["op"] == "mv"
        else "and keep the source; both manifests record the replica"
    )
    if plan["method"] == "rename":
        progress(
            "  how:      rename in place — same filesystem; bytes untouched, "
            "not re-hashed"
        )
    elif plan["method"] == "adopt":
        progress(f"  how:      {_how_adopt(plan, tail)}")
    else:
        there = f" {plan['far_side']}" if plan["far_side"] else ""
        progress(
            f"  how:      copy {plan['files']} files ({human_size(plan['bytes'])}), "
            f"re-hash the {plan['payload_files']} payload files at the "
            f"destination{there}, {tail}"
        )
    if plan["method"] != "rename":
        disk = plan["disk"]
        floor = disk.get("min_free_bytes")
        floor_note = f" ({human_size(floor)} floor)" if floor else ""
        progress(
            f"  disk:     needs {human_size(disk['needed_bytes'])}, "
            f"free {human_size(disk['free_bytes'])}{floor_note} at "
            f"{disk['checked_path']} — {disk['verdict'].upper()}"
        )
    curation = (plan["landing"] or {}).get("curation")
    if curation == "replaced":
        progress(
            "  notes:    the destination's curation.md differs and is replaced by "
            "this bundle's"
        )
    elif curation == "kept":
        progress(
            "  notes:    the destination's curation.md is kept — this bundle has none"
        )
    if plan["leaves_behind"]:
        progress(
            f"  leaves:   {', '.join(plan['leaves_behind'])} — vault-local; "
            "`darsay hydrate` again at the destination"
        )
    if plan["method"] != "rename" and plan["far_side"]:
        from .config import vault_config_path

        progress(
            f"  hash:     {plan['far_side']} — [host] in "
            f"{vault_config_path(plan['dest_vault'])}; nothing is read back over "
            "the wire"
        )
    elif plan["network"] and plan["method"] != "rename":
        for line in _over_the_wire(plan, dry_run=dry_run):
            progress(line)


def _over_the_wire(plan: dict, *, dry_run: bool) -> list[str]:
    """The network-mount warning: what the wire will carry, and the local way.

    A verb that verifies at the destination reads the payload back; over
    SMB or NFS that is a trip of every byte. The lines end with the exact
    commands that hash the bytes where the disk is instead — rsync carries
    what is not there yet (with a copy already there, only the record),
    ``verify`` runs on the host that owns the disk, and ``mv`` finishes with
    ``rm`` here.
    """
    from .readme_gen import human_size

    landing = plan["landing"]
    if plan["method"] == "adopt":
        cost = (
            f"hashing the {landing['present']} payload files already there reads "
            f"{human_size(landing['present_bytes'])} back over the wire"
        )
        why_rsync = "the record; payload files already there are skipped"
    else:
        cost = (
            f"copying {human_size(plan['bytes'])} over the wire and reading it "
            "all back to verify is two trips"
        )
        why_rsync = "one trip"
    lines = [
        f"  warning:  {plan['dest_vault']} is a network mount: {cost}. To hash "
        "the bytes where the disk is instead:",
        f"              {rsync_command(plan['source'], plan['dest'])}    # {why_rsync}",
        f"              darsay verify {shlex.quote(str(plan['dest']))}    "
        "# on the host that owns the disk, by its own path for that directory",
    ]
    if plan["op"] == "mv":
        lines.append(
            f"              darsay rm {plan['bundle_id']} --yes    # here, once that passed"
        )
    else:
        lines.append(
            "              (that copy is not recorded as a replica; the bytes are "
            "just as good)"
        )
    from .farside import far_side_guess
    from .transfer import mount_source

    guess = far_side_guess(mount_source(plan["dest_vault"])) or "USER@HOST"
    lines += [
        "            Or name the host that owns the disk once, and every verb "
        "hashes there instead of reading the mount back:",
        f"              darsay --vault {shlex.quote(str(plan['dest_vault']))} config "
        f"host.ssh={guess} host.path=/this/vault/on/that/host",
    ]
    if not dry_run:
        lines.append(
            "            Continuing over the wire; Ctrl-C at any point leaves the "
            "source untouched."
        )
    return lines


print_move_plan = print_relocation_plan


def _refuse_if_insufficient(plan: dict) -> None:
    if plan["method"] != "rename" and plan["disk"]["verdict"] == "insufficient":
        raise SystemExit(
            "error: not enough free space at the destination for the copy "
            "(the free-space floor is `darsay config`'s transfer.min_free)"
        )


def _try_rename(source: Path, dest: Path) -> bool:
    """Rename ``source`` onto ``dest``; ``False`` when the filesystems differ."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.rename(source, dest)
    except OSError as exc:
        if exc.errno == errno.EXDEV:
            return False
        raise
    return True


def _copy_and_verify(
    source: Path, dest: Path, plan: dict, *, progress, stamp
) -> tuple[dict, int]:
    """Copy into a staging directory beside ``dest``, verify there, then rename.

    The staging directory is one level deeper than the vault's
    ``<name>/<rev>`` layout, so ``darsay list`` never sees a half-copied
    bundle; a failure removes it and leaves the source untouched.
    ``stamp(staging)`` rewrites the copy's manifest once it has verified,
    right before it is renamed into place. Returns the verification report
    and how many files arrived as copy-on-write clones.
    """
    from .archiver import load_manifest
    from .readme_gen import human_size
    from .verify import hash_payload, record_verification

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
        actual = hash_payload(
            staging, load_manifest(staging), progress=progress, vault=plan["dest_vault"]
        )
        report = record_verification(staging, actual, progress=progress, at=dest)
        checksum = report["checksum"]
        if checksum["status"] != "pass":
            raise SystemExit(
                "error: verification FAILED at the destination — nothing "
                f"{'moved' if plan['op'] == 'mv' else 'copied'}, source untouched "
                f"({len(checksum['mismatched'])} modified, "
                f"{len(checksum['missing'])} missing, {len(checksum['extra'])} extra)"
            )
        stamp(staging)
        dest.parent.mkdir(parents=True, exist_ok=True)
        staging.rename(dest)
    finally:
        shutil.rmtree(staging_root, ignore_errors=True)
    return report, cloned


def _land_onto(
    source: Path, dest: Path, plan: dict, *, progress, stamp
) -> tuple[dict, dict]:
    """Land the bundle on a destination that already holds some of it.

    Two passes over the record's inventory, each hashed wherever the
    destination's disk actually is (``farside.hash_where_it_lives``):
    first every file already there at its recorded size — kept when it
    matches (adopted); then, after anything missing, at another size, or
    hashing wrong has been copied from the source, the copies. Then every
    other file the bundle carries — the record, the views, the notes, the
    ledger — replaces the destination's, the verification is recorded,
    and ``stamp(dest, landed)`` writes the new home. The source is not
    touched; a failure leaves the destination holding at most more good
    files than it had. Returns the verification report and the landing
    counts.
    """
    from .archiver import load_manifest
    from .farside import hash_where_it_lives
    from .hashing import iter_payload_files
    from .readme_gen import human_size
    from .schema import payload_root
    from .transfer import transfer_lock
    from .verify import record_verification

    manifest = load_manifest(source)
    root = payload_root(manifest)
    prefix = f"{root}/"
    expected = {r["path"]: r for r in manifest["inventory"]["files"]}
    landing = plan["landing"]
    vault = plan["dest_vault"]
    where = plan["far_side"] or "in place"
    verb = "moved" if plan["op"] == "mv" else "copied"

    def hashed(rels: list[str]) -> dict[str, dict]:
        if not rels:
            return {}
        found = hash_where_it_lives(
            vault, dest / root, [rel[len(prefix) :] for rel in rels], progress=progress
        )
        return {prefix + rel: entry for rel, entry in found.items()}

    with transfer_lock(dest, progress=progress):
        progress(
            f"Landing on {dest}: hashing {landing['present']} payload files "
            f"{where}, copying {landing['missing'] + landing['resized']} "
            f"({human_size(landing['copy_bytes'])}) ..."
        )
        there = [
            rel
            for rel in sorted(expected)
            if (dest / rel).is_file()
            and (dest / rel).stat().st_size == expected[rel]["size"]
        ]
        first = hashed(there)
        actual = {
            rel: first[rel]
            for rel in there
            if rel in first and first[rel]["sha256"] == expected[rel]["sha256"]
        }
        replaced = [rel for rel in there if rel not in actual]
        for rel in replaced:
            progress(
                f"  {rel}  differs from the record at the destination — "
                "copying from the source"
            )
        to_copy = [rel for rel in sorted(expected) if rel not in actual]
        for rel in to_copy:
            size = expected[rel]["size"]
            if size >= _ANNOUNCE_BYTES:
                progress(f"  {rel}  ({human_size(size)})  copying")
            _copy_file(source / rel, dest / rel)
        second = hashed(to_copy)
        for rel in to_copy:
            if second.get(rel, {}).get("sha256") != expected[rel]["sha256"]:
                raise SystemExit(
                    f"error: {rel} does not match the record after copying to the "
                    f"destination — nothing {verb}; the source is untouched, but "
                    f"check it: darsay verify {source}"
                )
            actual[rel] = second[rel]
        # Anything under the payload root the record does not list is
        # refused at plan time; one that appeared since is recorded as extra.
        if (dest / root).is_dir():
            extras = [
                prefix + rel
                for rel, _path in iter_payload_files(dest / root)
                if prefix + rel not in actual
            ]
            actual.update(hashed(extras))
        for rel, path in bundle_files(source):
            if rel not in expected:
                _copy_file(path, dest / rel)
        report = record_verification(dest, actual, progress=progress)
        checksum = report["checksum"]
        if checksum["status"] != "pass":
            raise SystemExit(
                f"error: verification FAILED at the destination — nothing {verb}, "
                f"source untouched ({len(checksum['mismatched'])} modified, "
                f"{len(checksum['missing'])} missing, {len(checksum['extra'])} extra)"
            )
        landed = {
            "adopted": len(there) - len(replaced),
            "copied": len(to_copy),
            "replaced": replaced,
        }
        stamp(dest, landed)
    return report, landed


def _stamp_new_home(
    bundle_dir: Path,
    home: Path,
    came_from: Path,
    *,
    method: str,
    landed: dict | None = None,
) -> None:
    """Record a move in the manifest and regenerate the views that name the path."""
    from .archiver import load_manifest, utc_now, write_manifest
    from .readme_gen import write_bundle_readme

    manifest = load_manifest(bundle_dir)
    now = utc_now()
    archive = manifest["archive"]
    move = {
        "at": now,
        "from_location": str(came_from.resolve()),
        "method": method,
    }
    if landed is not None:
        move["adopted"] = landed["adopted"]
        move["copied"] = landed["copied"]
        if landed["replaced"]:
            move["replaced"] = list(landed["replaced"])
    archive.setdefault("moves", []).append(move)
    archive["location"] = str(home.resolve())
    archive["host"] = socket.gethostname()
    archive["last_accessed"] = now
    write_manifest(bundle_dir, manifest)
    write_bundle_readme(bundle_dir, manifest)


def _stamp_replica(bundle_dir: Path, *, home: Path, other: Path, now: str) -> None:
    """Record that a verified copy of this bundle lives at ``other``.

    Written on both sides of a ``cp``: the copy learns where it came from,
    the source learns where its replica went. Entries are keyed by
    location — a second ``cp`` to the same disk updates the timestamp
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


def _landed_note(report: dict, landed: dict) -> str:
    checked = report["checksum"]["files_checked"]
    return (
        f"{checked} payload files verified at the destination — "
        f"{landed['adopted']} already there, {landed['copied']} copied"
    )


def move_bundle(
    bundle_dir: Path,
    dest_vault: Path,
    *,
    progress=print,
    dry_run: bool = False,
) -> Path:
    """Move a registered bundle into ``dest_vault``; returns its new directory.

    Same filesystem: a rename, then the manifest's ``archive.location`` and
    the generated views are refreshed. Different filesystems: copy into a
    staging directory, re-hash every payload file there against the
    manifest, stamp the new home, rename into place, and only then remove
    the source. A destination already holding the bundle is landed on:
    what is there is hashed in place, what is missing or differs is
    copied, and the record travels. A failed verification leaves the
    source exactly as it was. ``dry_run`` resolves and reports and touches
    nothing.
    """
    from .transfer import transfer_lock
    from .vault import prune_empty_parent
    from .verify import refresh_verification_md

    bundle_dir = Path(bundle_dir)
    plan = relocation_plan(bundle_dir, dest_vault, op="mv")
    print_relocation_plan(plan, progress, dry_run=dry_run)
    dest = plan["dest"]
    if dry_run:
        return dest
    _refuse_if_insufficient(plan)

    with transfer_lock(bundle_dir, progress=progress):
        if plan["method"] == "adopt":
            report, landed = _land_onto(
                bundle_dir,
                dest,
                plan,
                progress=progress,
                stamp=lambda home, landed: _stamp_new_home(
                    home, home, bundle_dir, method="adopt", landed=landed
                ),
            )
            _remove_source(bundle_dir)
            progress(
                f"Moved {plan['bundle_id']} → {dest}  "
                f"({_landed_note(report, landed)} — before the source was removed)"
            )
            return dest
        renamed = plan["method"] == "rename" and _try_rename(bundle_dir, dest)
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
    progress=print,
    dry_run: bool = False,
) -> Path:
    """Copy a registered bundle into ``dest_vault``; returns the copy's directory.

    Copy into a staging directory (copy-on-write clones where the
    filesystem offers them), re-hash every payload file there against the
    manifest, rename into place, then record the replica in *both*
    manifests (``archive.replicas``, ``backup_status: replicated``). A
    destination already holding the bundle — a backup being refreshed —
    is landed on: what is there is hashed in place, what is missing or
    differs is copied. The source payload is never touched; a failed
    verification records nothing anywhere. ``dry_run`` resolves and
    reports and touches nothing.
    """
    from .archiver import utc_now
    from .transfer import transfer_lock
    from .verify import refresh_verification_md

    bundle_dir = Path(bundle_dir)
    plan = relocation_plan(bundle_dir, dest_vault, op="cp")
    print_relocation_plan(plan, progress, dry_run=dry_run)
    dest = plan["dest"]
    if dry_run:
        return dest
    _refuse_if_insufficient(plan)

    now = utc_now()
    with transfer_lock(bundle_dir, progress=progress):
        if plan["method"] == "adopt":
            report, landed = _land_onto(
                bundle_dir,
                dest,
                plan,
                progress=progress,
                stamp=lambda home, _landed: _stamp_replica(
                    home, home=home, other=bundle_dir, now=now
                ),
            )
            _stamp_replica(bundle_dir, home=bundle_dir, other=dest, now=now)
            progress(
                f"Copied {plan['bundle_id']} → {dest}  "
                f"({_landed_note(report, landed)}; source kept, replica recorded "
                "in both manifests)"
            )
            return dest
        report, cloned = _copy_and_verify(
            bundle_dir,
            dest,
            plan,
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

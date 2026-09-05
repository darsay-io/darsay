"""Vault inventory and bundle addressing.

``darsay list`` is a walk of this tree (the vault as a catalog view).
Bundle-taking commands accept a filesystem path, a bundle id
(``name@revision12``), or a unique prefix of either — HAVE in ``list``
is a usable handle.
"""

from __future__ import annotations

import json
import os
import sys
from contextlib import suppress
from pathlib import Path

DEFAULT_VAULT_NAME = "darsay"
RESERVED_DIRS = {".runtime", "catalogs"}


def default_vault() -> Path:
    """``$DARSAY_HOME`` if set, otherwise ``~/darsay``."""
    env = os.environ.get("DARSAY_HOME")
    if env:
        return Path(env).expanduser()
    return Path.home() / DEFAULT_VAULT_NAME


def using_implicit_vault(vault_flag: str | None) -> bool:
    return not vault_flag and not os.environ.get("DARSAY_HOME")


def announce_vault(vault: Path, *, implicit: bool) -> None:
    """Tell the user where the vault is when they did not pick one."""
    if implicit:
        print(
            f"Vault: {vault}  (default; set $DARSAY_HOME or --vault to override)",
            file=sys.stderr,
        )


def dir_size(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file())


def bundle_records(vault: Path) -> list[dict]:
    """Inventory rows for ``list`` / ``du`` / completion."""
    from .readme_gen import human_size
    from .schema import payload_root_for
    from .transfer import LedgerError, load_ledger, transfer_plan

    rows = []
    for bundle_dir in iter_bundle_dirs(vault):
        on_disk = dir_size(bundle_dir)
        manifest_path = bundle_dir / "manifest.json"
        if manifest_path.is_file():
            try:
                m = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                rows.append(
                    {
                        "bundle_id": bundle_id_for(bundle_dir),
                        "path": str(bundle_dir),
                        "license": "?",
                        "on_disk_bytes": on_disk,
                        "payload_bytes": None,
                        "size": human_size(on_disk),
                        "integrity": "unreadable manifest",
                        "archived": "?",
                        "partial": False,
                        "status": "have",
                        "source_address": None,
                        "revision": None,
                        "revision_ref": None,
                        "include": None,
                        "policy": None,
                        "remaining_bytes": None,
                        "handed_off_bytes": None,
                        "percent": None,
                    }
                )
                continue
            payload_bytes = m["inventory"]["total_size_bytes"]
            src = m.get("source") or {}
            subset = src.get("subset") or {}
            include = subset.get("include")
            policy = subset.get("policy")
            schema, record_note = _record_standing(m)
            rows.append(
                {
                    "bundle_id": m["bundle_id"],
                    "path": str(bundle_dir),
                    "license": m["licensing"]["spdx_id"] or "?",
                    "on_disk_bytes": on_disk,
                    "payload_bytes": payload_bytes,
                    "size": human_size(payload_bytes),
                    "integrity": m["security"]["integrity_status"],
                    "archived": m["archive"]["date_archived"][:10],
                    "partial": False,
                    "artifact_type": m.get("artifact_type"),
                    "status": "migrate" if record_note else "have",
                    "schema_version": schema,
                    "note": record_note,
                    "source_address": _source_address_from_manifest(m),
                    "revision": src.get("revision"),
                    "revision_ref": src.get("revision_ref"),
                    "include": include,
                    "policy": policy,
                    "remaining_bytes": 0,
                    "handed_off_bytes": 0,
                    "percent": None,
                }
            )
            continue
        try:
            ledger = load_ledger(bundle_dir)
            root = payload_root_for(ledger["repo_type"])
            plan = transfer_plan(bundle_dir / root, ledger)
            sizes = plan["bytes"]
            files = plan["files"]
            handed_off_bytes = sizes.get("handed_off", 0)
            # Percent = how far along the pin is anywhere: bytes verified here,
            # banked in partials, or verified in another vault (a skeleton).
            banked = sizes["verified"] + sizes["partial"] + handed_off_bytes
            percent = int(banked * 100 / sizes["total"]) if sizes["total"] else 0
            handed_off_note = (
                f", {human_size(handed_off_bytes)} handed off"
                if handed_off_bytes
                else ""
            )
            status = (
                f"archiving: {percent}% "
                f"({human_size(banked)}/{human_size(sizes['total'])}, "
                f"{files['verified']}/{files['total']} files verified{handed_off_note})"
            )
            card = ledger.get("metadata", {}).get("card_data", {})
            license_id = card.get("license") if isinstance(card, dict) else None
            subset_rec = ledger.get("subset") or {}
            include = subset_rec.get("include")
            rows.append(
                {
                    "bundle_id": bundle_id_for(bundle_dir),
                    "path": str(bundle_dir),
                    "license": license_id or "?",
                    "on_disk_bytes": on_disk,
                    "payload_bytes": sizes["total"],
                    "size": human_size(sizes["total"]),
                    "integrity": status,
                    "archived": ledger["pinned_at"][:10],
                    "partial": True,
                    "artifact_type": ledger.get("repo_type"),
                    "status": "partial",
                    "source_address": _source_address_from_ledger(ledger),
                    "revision": ledger.get("revision"),
                    "revision_ref": ledger.get("revision_ref"),
                    "include": include,
                    "policy": subset_rec.get("policy"),
                    "remaining_bytes": sizes["remaining_network"],
                    "handed_off_bytes": handed_off_bytes,
                    "percent": percent,
                }
            )
        except (LedgerError, KeyError, OSError, TypeError, ValueError):
            rows.append(
                {
                    "bundle_id": bundle_id_for(bundle_dir),
                    "path": str(bundle_dir),
                    "license": "?",
                    "on_disk_bytes": on_disk,
                    "payload_bytes": None,
                    "size": human_size(on_disk) if on_disk else "?",
                    "integrity": "archiving: unreadable transfer ledger",
                    "archived": "?",
                    "partial": True,
                    "status": "partial",
                    "source_address": None,
                    "revision": None,
                    "revision_ref": None,
                    "include": None,
                    "policy": None,
                    "remaining_bytes": None,
                    "handed_off_bytes": None,
                    "percent": None,
                }
            )
    return rows


def _record_standing(manifest: dict) -> tuple[str | None, str | None]:
    """The record's schema, and a note when this darsay cannot read it.

    ``list`` walks every record so an operator sees what arrived; a
    record of an earlier major is on disk and intact but every other
    verb refuses it until ``darsay migrate`` brings it forward.
    """
    from .migrate import record_status
    from .schema import MANIFEST_SCHEMA_MAJOR

    version = manifest.get("schema_version")
    try:
        standing = record_status(version)
    except ValueError:
        return (str(version) if version is not None else None), None
    if standing == "older":
        return str(version), f"schema {version} · darsay migrate"
    if standing == "newer":
        return str(version), (
            f"schema {version} · newer than this darsay ({MANIFEST_SCHEMA_MAJOR}.x)"
        )
    return str(version), None


def prune_empty_parent(bundle_dir: Path) -> None:
    """Drop the ``<vault>/<name>/`` shell a removed bundle left behind.

    Bundles sit two levels deep, so removing the last revision of a name
    leaves an empty directory that shows up in every ``find`` and confuses
    "is it gone?". Best effort: a name that still holds another revision (or
    a parent that will not go) is left exactly as it is.
    """
    parent = bundle_dir.parent
    with suppress(OSError):
        if parent.is_dir() and not any(parent.iterdir()):
            parent.rmdir()


def iter_bundle_dirs(vault: Path) -> list[Path]:
    """Registered bundles and in-progress archives (ledger, no manifest)."""
    found = {
        path.parent
        for pattern in ("manifest.json", "transfer.json")
        for path in vault.glob(f"*/*/{pattern}")
        if path.parent.parent.name not in RESERVED_DIRS
    }
    return sorted(found)


# Where removable and external disks mount, so an absent directory there
# reads as "not plugged in", not "empty".
MOUNT_ROOTS = ("/Volumes", "/media", "/mnt", "/run/media")


def _mount_root_of(path: Path) -> str | None:
    p = str(Path(path).expanduser().resolve())
    for root in MOUNT_ROOTS:
        if p == root or p.startswith(root + "/"):
            return root
    return None


def vault_absence(vault: Path) -> str | None:
    """Why ``vault`` is not a usable directory, or ``None`` when it is fine.

    On a laptop a missing directory under ``/Volumes`` (or ``/media`` etc.)
    is almost always a disk that is not mounted, and an *empty* directory
    that sits where a volume mounts but is not itself a mount point is the
    stub the OS leaves after an eject — writing there fills the boot disk.
    Both are named so, apart from a plain typo.
    """
    v = Path(vault).expanduser()
    root = _mount_root_of(v)
    if not v.exists():
        if root:
            return (
                "the vault directory does not exist; if it is on a removable disk, "
                f"that disk may not be mounted under {root}"
            )
        return "the vault directory does not exist"
    if not v.is_dir():
        return "the vault path is not a directory"
    if root and not os.path.ismount(v):
        try:
            empty = not any(v.iterdir())
        except OSError:
            empty = False
        if empty:
            return (
                f"this is an empty folder under {root}, not a mounted volume — the "
                "disk may not be plugged in (writing here would fill the boot disk)"
            )
    return None


def command_prefix(vault: Path) -> list[str]:
    """``["darsay"]``, plus ``--vault <vault>`` when a bare command looks elsewhere.

    A pasted ``darsay <verb> <id>`` searches the default vault
    (``$DARSAY_HOME`` or ``~/darsay``); when this vault is not that one the
    command needs ``--vault`` to resolve the id, so a hint printed under
    ``--vault`` prints it back.
    """
    try:
        same = Path(vault).resolve() == default_vault().resolve()
    except OSError:
        same = False
    return ["darsay"] if same else ["darsay", "--vault", str(vault)]


def registered_in(vault: Path, bundle_dir: Path) -> bool:
    """Whether ``bundle_dir`` is one of the vault's own ``<name>/<rev>`` rows.

    An id (``name@rev12``) is a search of the vault, so it names a bundle
    only from there; a bundle addressed by a path somewhere else — an
    arrival still where rsync left it — is named by that path.
    """
    try:
        resolved = Path(bundle_dir).resolve()
        return (
            resolved.parent.parent == Path(vault).resolve()
            and resolved.parent.name not in RESERVED_DIRS
        )
    except OSError:
        return False


def _source_address_from_manifest(manifest: dict) -> str | None:
    """Canonical source address from the manifest."""
    return (manifest.get("source") or {}).get("address")


def _source_address_from_ledger(ledger: dict) -> str | None:
    from .sources import source_from_ledger

    try:
        return source_from_ledger(ledger).canonical
    except (SystemExit, KeyError, TypeError):
        return ledger.get("address")


def bundle_id_for(bundle_dir: Path) -> str:
    """Stable id from the manifest, or ``<name>@<rev12>`` for a partial."""
    manifest_path = bundle_dir / "manifest.json"
    if manifest_path.is_file():
        try:
            return json.loads(manifest_path.read_text(encoding="utf-8"))["bundle_id"]
        except (OSError, json.JSONDecodeError, KeyError, TypeError):
            pass
    return f"{bundle_dir.parent.name}@{bundle_dir.name}"


def _match_keys(bundle_dir: Path, bundle_id: str) -> list[str]:
    """Lowercased identities a spec may match."""
    rel = f"{bundle_dir.parent.name}/{bundle_dir.name}"
    return [
        bundle_id.lower(),
        bundle_dir.name.lower(),
        bundle_dir.parent.name.lower(),
        rel.lower(),
        str(bundle_dir).lower(),
        bundle_dir.as_posix().lower(),
    ]


def is_forced_path(spec: str) -> bool:
    """Absolute, home, or explicit relative paths are not vault-id searches."""
    return spec.startswith(".") or spec.startswith("~") or Path(spec).is_absolute()


def _dedupe(rows: list[tuple[Path, str]]) -> list[tuple[Path, str]]:
    seen: set[Path] = set()
    out = []
    for bundle_dir, bundle_id in rows:
        if bundle_dir not in seen:
            seen.add(bundle_dir)
            out.append((bundle_dir, bundle_id))
    return out


def resolve_bundle(
    vault: Path,
    spec: str,
    *,
    require_manifest: bool = True,
) -> Path:
    """Resolve a bundle spec to a directory.

    Accepts an existing directory (cwd- or vault-relative, absolute, ``~/…``),
    a bundle id (``name@revision12``), or a unique prefix of the id / name /
    revision. A unique substring of the id is a last resort (3+ characters)
    so a one-letter typo does not match the whole vault.
    """
    raw = (spec or "").strip()
    if not raw:
        raise SystemExit("error: empty bundle spec")

    path = Path(raw).expanduser()
    if path.is_dir():
        return _require(path, require_manifest=require_manifest)
    vault_relative = vault / raw
    if (
        not is_forced_path(raw)
        and vault_relative.is_dir()
        and (
            (vault_relative / "manifest.json").is_file()
            or (vault_relative / "transfer.json").is_file()
        )
    ):
        return _require(vault_relative, require_manifest=require_manifest)

    if is_forced_path(raw):
        raise SystemExit(
            f"error: no manifest.json in {path} — not a darsay bundle"
            if require_manifest
            else f"error: {path} is not a darsay bundle"
        )

    needle = raw.lower().replace("\\", "/")
    exact: list[tuple[Path, str]] = []
    prefix: list[tuple[Path, str]] = []
    substring: list[tuple[Path, str]] = []
    for bundle_dir in iter_bundle_dirs(vault):
        bundle_id = bundle_id_for(bundle_dir)
        keys = _match_keys(bundle_dir, bundle_id)
        rec = (bundle_dir, bundle_id)
        if any(key == needle for key in keys):
            exact.append(rec)
        elif any(key.startswith(needle) for key in keys):
            prefix.append(rec)
        elif len(needle) >= 3 and any(needle in key for key in keys):
            substring.append(rec)

    for pool, kind in (
        (exact, "exact"),
        (_dedupe(prefix), "prefix"),
        (_dedupe(substring), "substring"),
    ):
        hits = _dedupe(pool)
        if len(hits) == 1:
            return _require(hits[0][0], require_manifest=require_manifest)
        if len(hits) > 1:
            listed = "\n".join(
                f"  {bundle_id}  {directory}" for directory, bundle_id in hits
            )
            raise SystemExit(
                f"error: {raw!r} matches {len(hits)} bundles ({kind}):\n{listed}\n"
                "  use a longer prefix, the bundle id from `darsay list`, or a path from `darsay list --json` / `darsay info`"
            )
    extra = ""
    folded = raw.strip().casefold()
    if (
        folded
        and all(c.isalnum() or c in "._-" for c in folded)
        and folded[0].isalpha()
    ):
        catalog_file = vault / "catalogs" / folded / "catalog.json"
        if catalog_file.is_file():
            extra = (
                f"\n  hint: darsay list {folded}"
                f"\n  hint: darsay archive --next {folded}"
            )
    absent = vault_absence(vault)
    if absent:
        raise SystemExit(
            f"error: {absent}: {vault}\n"
            "  hint: mount the disk, or point --vault at a vault that exists"
        )
    raise SystemExit(
        f"error: no bundle matching {raw!r} in {vault}/\n"
        f"  hint: darsay list   (or darsay --vault {vault} list)"
        f"{extra}"
    )


def _require(bundle_dir: Path, *, require_manifest: bool) -> Path:
    if require_manifest:
        if not (bundle_dir / "manifest.json").is_file():
            raise SystemExit(
                f"error: no manifest.json in {bundle_dir} — not a darsay bundle"
            )
        return bundle_dir
    if not (
        (bundle_dir / "manifest.json").is_file()
        or (bundle_dir / "transfer.json").is_file()
    ):
        raise SystemExit(f"error: {bundle_dir} is not a darsay bundle")
    return bundle_dir

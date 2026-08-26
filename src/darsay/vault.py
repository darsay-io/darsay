"""Vault inventory and bundle addressing.

``darsay list`` is a walk of this tree. Bundle-taking commands accept a
filesystem path, a bundle id (``name@revision12``), or a unique prefix of
either — so the first column of ``list`` is a usable handle.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

DEFAULT_VAULT_NAME = "darsay"


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
                rows.append({
                    "bundle_id": bundle_id_for(bundle_dir),
                    "path": str(bundle_dir),
                    "license": "?",
                    "size_bytes": on_disk,
                    "size": human_size(on_disk),
                    "integrity": "unreadable manifest",
                    "archived": "?",
                    "partial": False,
                })
                continue
            rows.append({
                "bundle_id": m["bundle_id"],
                "path": str(bundle_dir),
                "license": m["licensing"]["spdx_id"] or "?",
                "size_bytes": on_disk,
                "payload_bytes": m["inventory"]["total_size_bytes"],
                "size": human_size(m["inventory"]["total_size_bytes"]),
                "integrity": m["security"]["integrity_status"],
                "archived": m["archive"]["date_archived"][:10],
                "partial": False,
                "artifact_type": m.get("artifact_type"),
            })
            continue
        try:
            ledger = load_ledger(bundle_dir)
            root = payload_root_for(ledger["repo_type"])
            plan = transfer_plan(bundle_dir / root, ledger)
            sizes = plan["bytes"]
            files = plan["files"]
            banked = sizes["verified"] + sizes["partial"]
            percent = int(banked * 100 / sizes["total"]) if sizes["total"] else 0
            status = (
                f"archiving: {percent}% "
                f"({human_size(banked)}/{human_size(sizes['total'])}, "
                f"{files['verified']}/{files['total']} files verified)"
            )
            card = ledger.get("metadata", {}).get("card_data", {})
            license_id = card.get("license") if isinstance(card, dict) else None
            rows.append({
                "bundle_id": bundle_id_for(bundle_dir),
                "path": str(bundle_dir),
                "license": license_id or "?",
                "size_bytes": on_disk,
                "payload_bytes": sizes["total"],
                "size": human_size(sizes["total"]),
                "integrity": status,
                "archived": ledger["pinned_at"][:10],
                "partial": True,
            })
        except (LedgerError, KeyError, OSError, TypeError, ValueError):
            rows.append({
                "bundle_id": bundle_id_for(bundle_dir),
                "path": str(bundle_dir),
                "license": "?",
                "size_bytes": on_disk,
                "size": human_size(on_disk) if on_disk else "?",
                "integrity": "archiving: unreadable transfer ledger",
                "archived": "?",
                "partial": True,
            })
    return rows


def iter_bundle_dirs(vault: Path) -> list[Path]:
    """Registered bundles and in-progress archives (ledger, no manifest)."""
    found = {
        path.parent
        for pattern in ("manifest.json", "transfer.json")
        for path in vault.glob(f"*/*/{pattern}")
    }
    return sorted(found)


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


def _looks_like_path(spec: str) -> bool:
    return (
        "/" in spec
        or "\\" in spec
        or spec.startswith(".")
        or spec.startswith("~")
        or Path(spec).is_absolute()
    )


def resolve_bundle(
    vault: Path,
    spec: str,
    *,
    require_manifest: bool = True,
) -> Path:
    """Resolve a bundle spec to a directory.

    Accepts:
    - an existing directory (absolute, relative, or ``~/…``)
    - a bundle id (``name@revision12``)
    - a unique prefix or substring of the id, directory name, or ``name/rev``
    """
    raw = (spec or "").strip()
    if not raw:
        raise SystemExit("error: empty bundle spec")

    path = Path(raw).expanduser()
    if path.is_dir():
        return _require(path, require_manifest=require_manifest)

    if _looks_like_path(raw):
        raise SystemExit(
            f"error: no manifest.json in {path} — not a darsay bundle"
            if require_manifest
            else f"error: {path} is not a darsay bundle"
        )

    needle = raw.lower()
    matches: list[tuple[Path, str]] = []
    seen: set[Path] = set()
    for bundle_dir in iter_bundle_dirs(vault):
        bundle_id = bundle_id_for(bundle_dir)
        keys = _match_keys(bundle_dir, bundle_id)
        if any(key == needle or key.startswith(needle) or needle in key for key in keys):
            if bundle_dir not in seen:
                matches.append((bundle_dir, bundle_id))
                seen.add(bundle_dir)

    if len(matches) == 1:
        return _require(matches[0][0], require_manifest=require_manifest)
    if not matches:
        raise SystemExit(
            f"error: no bundle matching {raw!r} in {vault}/\n"
            f"  hint: darsay list   (or darsay --vault {vault} list)"
        )
    listed = "\n".join(f"  {bundle_id}  {directory}" for directory, bundle_id in matches)
    raise SystemExit(
        f"error: {raw!r} matches {len(matches)} bundles:\n{listed}\n"
        "  use a longer prefix, the bundle id, or the PATH from `darsay list`"
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

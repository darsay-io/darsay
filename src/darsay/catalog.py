"""Catalogs: shareable want-lists the vault is a view of.

A catalog is a curated list of sources. Overlay against ``bundle_records``
yields have / partial / want / unknown. Possession is a view; ``archive``
does not rewrite catalog.json.
"""

from __future__ import annotations

import json
import re
import shlex
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .archiver import utc_now
from .readme_gen import _curation_body, human_size
from .sources import parse_source

CATALOG_SCHEMA_VERSION = "1.0.0"
CATALOG_KIND = "darsay.catalog"
STALE_AFTER_DAYS = 7
CATALOGS_DIRNAME = "catalogs"
SLUG_RE = re.compile(r"^[a-z][a-z0-9._-]{0,63}$")
DESIRE_MIN, DESIRE_MAX = 1, 9
MIN_REV_PREFIX = 12
_HEX_REV = re.compile(r"^[0-9a-f]+$", re.IGNORECASE)
DIGEST_KEYS = frozenset({
    "as_of", "artifact_type", "revision", "revision_ref",
    "payload_bytes", "file_count", "license", "gated",
    "parameters", "dominant_dtype", "unknown_size_count",
})
_CATALOG_TOP_KEYS = (
    "catalog_schema_version", "kind", "id", "title", "curator", "note",
    "created", "updated", "entries",
)
_HIDE_IF_EMPTY = frozenset({"DESIRE", "NOTE"})


def catalogs_dir(vault: Path) -> Path:
    return vault / CATALOGS_DIRNAME


def fold_slug(spec: str) -> str:
    return (spec or "").strip().casefold()


def iter_catalogs(vault: Path) -> list[dict]:
    """Load vault-named catalogs (skip unreadable with a stderr warning)."""
    root = catalogs_dir(vault)
    if not root.is_dir():
        return []
    rows = []
    for child in sorted(p for p in root.iterdir() if p.is_dir()):
        path = child / "catalog.json"
        if not path.is_file():
            continue
        try:
            catalog = load_catalog(path)
        except SystemExit as exc:
            print(f"warning: {warning_detail(exc)}", file=sys.stderr)
            continue
        rows.append(catalog)
    return rows


def warning_detail(exc: BaseException) -> str:
    """Strip a leading ``error: `` so a SystemExit can be reprinted as a warning."""
    text = str(exc)
    return text[7:] if text.startswith("error: ") else text


def try_parse_source(ref: str):
    """Parse a source. Unknown provider/host → None. Known-provider parse errors still raise."""
    try:
        return parse_source(ref)
    except SystemExit as exc:
        text = str(exc)
        if "unknown source provider" in text or "no source provider for host" in text:
            return None
        raise


def include_key(include: list[str] | None) -> tuple[str, ...]:
    return tuple(sorted(include or ()))


def entry_key(source: str, revision: str | None, include: list[str] | None) -> tuple:
    """Uniqueness for add/drop. Unknown-provider rows key on the raw source string."""
    parsed = try_parse_source(source)
    canonical = parsed.canonical if parsed is not None else source
    return (canonical, (revision or "").strip(), include_key(include))


def revisions_match(got: str, want: str) -> bool:
    """True iff one hex string is a prefix of the other, both length >= 12."""
    g, w = (got or "").lower(), (want or "").lower()
    if len(g) < MIN_REV_PREFIX or len(w) < MIN_REV_PREFIX:
        return False
    if not (_HEX_REV.fullmatch(g) and _HEX_REV.fullmatch(w)):
        return g == w
    return g == w or g.startswith(w) or w.startswith(g)


def estimate_digest(est: dict) -> dict:
    """Projection of estimate() onto DIGEST_KEYS. Not a subset of the live dict."""
    params = est.get("parameters") or {}
    digest = {
        "as_of": est["as_of"],
        "artifact_type": est["artifact_type"],
        "revision": est["source"]["revision"],
        "revision_ref": est["source"]["revision_ref"],
        "payload_bytes": est["payload"]["total_size_bytes"],
        "file_count": est["payload"]["file_count"],
        "license": est["source"]["license"],
        "gated": est["source"]["gated"],
        "parameters": params.get("total") if isinstance(params, dict) else None,
        "dominant_dtype": params.get("dominant_dtype") if isinstance(params, dict) else None,
        "unknown_size_count": est["payload"]["unknown_size_count"],
    }
    assert set(digest) == DIGEST_KEYS
    return digest


def project_stored_estimate(est) -> dict | None:
    """Keep only DIGEST_KEYS. Live ``estimate()`` dicts are digested; disk paths never persist."""
    if not isinstance(est, dict):
        return None
    if isinstance(est.get("payload"), dict) and isinstance(est.get("source"), dict):
        try:
            return estimate_digest(est)
        except (AssertionError, KeyError, TypeError):
            pass
    return {k: est[k] for k in DIGEST_KEYS if k in est}


def _looks_like_catalog(data) -> bool:
    return isinstance(data, dict) and (
        data.get("kind") == CATALOG_KIND or "catalog_schema_version" in data
    )


def try_resolve_catalog(vault: Path, spec: str) -> Path | None:
    """Return catalog.json if spec names an existing catalog, else None.

    Slug-shaped specs resolve only under ``catalogs/``. Filesystem specs
    require ``./``, ``~/``, or an absolute path (same as bundle addressing).
    """
    from .vault import is_forced_path

    raw = (spec or "").strip()
    if not raw:
        return None
    if is_forced_path(raw):
        path = Path(raw).expanduser()
        if path.is_file():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                return None
            return path if _looks_like_catalog(data) else None
        if path.is_dir() and (path / "catalog.json").is_file():
            return path / "catalog.json"
        return None
    folded = fold_slug(raw)
    if SLUG_RE.fullmatch(folded):
        candidate = catalogs_dir(vault) / folded / "catalog.json"
        if candidate.is_file():
            return candidate
    return None


def resolve_catalog(vault: Path, spec: str) -> Path:
    """Return the catalog.json path or SystemExit with hints."""
    raw = (spec or "").strip()
    if not raw:
        raise SystemExit("error: empty catalog spec")
    found = try_resolve_catalog(vault, raw)
    if found is not None:
        catalog = load_catalog(found)
        parent_name = found.parent.name
        if found.parent != Path(raw).expanduser() and SLUG_RE.fullmatch(fold_slug(raw)):
            if fold_slug(parent_name) != fold_slug(catalog["id"]):
                raise SystemExit(
                    f"error: catalog directory {parent_name!r} does not match "
                    f"id {catalog['id']!r}"
                )
        return found
    path = Path(raw).expanduser()
    if raw.startswith((".", "~")) or path.is_absolute():
        raise SystemExit(f"error: no catalog at {path}")
    bundle_hint = _matching_bundle_hint(vault, raw)
    if bundle_hint:
        raise SystemExit(
            f"error: no catalog matching {raw!r} in {catalogs_dir(vault)}/\n"
            f"  {bundle_hint}\n"
            f"  hint: darsay list ./path/to/catalog.json"
        )
    raise SystemExit(
        f"error: no catalog matching {raw!r} in {catalogs_dir(vault)}/\n"
        f"  hint: darsay catalog new {fold_slug(raw) or raw}\n"
        f"  hint: darsay list ./path/to/catalog.json"
    )


def _matching_bundle_hint(vault: Path, spec: str) -> str | None:
    """If spec would resolve as a bundle, tell the user which command to use."""
    from .vault import resolve_bundle

    try:
        resolve_bundle(vault, spec, require_manifest=False)
    except SystemExit:
        return None
    return f"hint: {spec!r} is a bundle — darsay info {spec}"


def catalog_is_vault_named(vault: Path, path: Path) -> bool:
    try:
        rel = path.resolve().relative_to(catalogs_dir(vault).resolve())
    except ValueError:
        return False
    return len(rel.parts) == 2 and rel.parts[1] == "catalog.json"


def require_writable(vault: Path, path: Path, write_flag: bool, *, hint: str | None = None) -> None:
    if catalog_is_vault_named(vault, path):
        return
    if write_flag:
        return
    shown = str(path.parent if path.name == "catalog.json" else path)
    extra = hint or f"darsay catalog adopt <name> {shown}"
    raise SystemExit(
        f"error: catalog at {shown} is read-only (not in this vault’s catalogs/)\n"
        f"  hint: {extra}\n"
        f"  hint: pass --write to mutate this file"
    )


def load_catalog(path: Path) -> dict:
    """Read + validate. Major-newer → SystemExit."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise SystemExit(f"error: no catalog at {path}") from None
    except OSError as exc:
        raise SystemExit(f"error: unreadable catalog at {path}: {exc}") from None
    except json.JSONDecodeError as exc:
        raise SystemExit(f"error: unreadable catalog at {path}: {exc}") from None
    if not isinstance(data, dict):
        raise SystemExit(f"error: unreadable catalog at {path}: not a JSON object")
    version = data.get("catalog_schema_version")
    if not version:
        raise SystemExit(f"error: unreadable catalog at {path}: catalog_schema_version missing")
    try:
        major = int(str(version).split(".", 1)[0])
    except ValueError:
        raise SystemExit(f"error: unreadable catalog at {path}: catalog_schema_version {version!r}") from None
    if major > 1:
        raise SystemExit(
            f"error: catalog schema {version} is newer than this darsay (supports 1.x)"
        )
    if data.get("kind") != CATALOG_KIND:
        raise SystemExit(f"error: unreadable catalog at {path}: kind is not {CATALOG_KIND!r}")
    ident = data.get("id")
    if not isinstance(ident, str) or not SLUG_RE.fullmatch(fold_slug(ident)):
        raise SystemExit(f"error: unreadable catalog at {path}: invalid id")
    entries = data.get("entries")
    if entries is None:
        entries = []
    if not isinstance(entries, list):
        raise SystemExit(f"error: unreadable catalog at {path}: entries must be an array")
    cleaned = []
    for i, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise SystemExit(f"error: unreadable catalog at {path}: entry {i} is not an object")
        source = entry.get("source")
        if not isinstance(source, str) or not source.strip():
            raise SystemExit(f"error: unreadable catalog at {path}: entry {i} missing source")
        desire = entry.get("desire")
        if desire is not None and (not isinstance(desire, int) or desire < DESIRE_MIN or desire > DESIRE_MAX):
            raise SystemExit(
                f"error: unreadable catalog at {path}: entry {i} desire must be {DESIRE_MIN}–{DESIRE_MAX} or null"
            )
        include = entry.get("include")
        if include is not None:
            if not isinstance(include, list) or not all(isinstance(g, str) for g in include):
                raise SystemExit(
                    f"error: unreadable catalog at {path}: entry {i} include must be a list of strings or null"
                )
        cleaned.append({
            "source": source,
            "revision": entry.get("revision"),
            "include": include,
            "desire": desire,
            "note": entry.get("note"),
            "added": entry.get("added"),
            "estimate": project_stored_estimate(entry.get("estimate")),
        })
        try:
            try_parse_source(source)
        except SystemExit as exc:
            raise SystemExit(
                f"error: unreadable catalog at {path}: entry {i} {warning_detail(exc)}"
            ) from None
    loaded = {
        "catalog_schema_version": str(version),
        "kind": CATALOG_KIND,
        "id": fold_slug(ident),
        "title": data.get("title") or ident,
        "curator": data.get("curator"),
        "note": data.get("note"),
        "created": data.get("created"),
        "updated": data.get("updated"),
        "entries": cleaned,
        "_path": str(path),
    }
    for key, value in data.items():
        if key not in _CATALOG_TOP_KEYS and key != "_path":
            loaded[key] = value
    return loaded


def save_catalog(path: Path, catalog: dict) -> None:
    """Write catalog.json. Does not touch curation.md."""
    payload = {
        "catalog_schema_version": catalog.get("catalog_schema_version") or CATALOG_SCHEMA_VERSION,
        "kind": CATALOG_KIND,
        "id": catalog["id"],
        "title": catalog.get("title") or catalog["id"],
        "curator": catalog.get("curator"),
        "note": catalog.get("note"),
        "created": catalog.get("created"),
        "updated": catalog.get("updated"),
        "entries": [
            {
                "source": e["source"],
                "revision": e.get("revision"),
                "include": e.get("include"),
                "desire": e.get("desire"),
                "note": e.get("note"),
                "added": e.get("added"),
                "estimate": project_stored_estimate(e.get("estimate")),
            }
            for e in catalog.get("entries") or []
        ],
    }
    for key, value in catalog.items():
        if key not in _CATALOG_TOP_KEYS and not str(key).startswith("_"):
            payload[key] = value
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
    try:
        path.write_text(text, encoding="utf-8")
    except OSError as exc:
        raise SystemExit(f"error: cannot write {path} ({exc})") from None


def new_catalog(
    vault: Path,
    name: str,
    *,
    title: str | None = None,
    curator: str | None = None,
    note: str | None = None,
) -> dict:
    slug = fold_slug(name)
    if not SLUG_RE.fullmatch(slug):
        raise SystemExit(
            f"error: invalid catalog id {name!r} — use a lowercase letter, then "
            "letters, digits, '.', '_' or '-' (max 64)"
        )
    dest = catalogs_dir(vault) / slug
    path = dest / "catalog.json"
    if path.exists():
        raise SystemExit(
            f"error: catalog {slug!r} already exists at {dest}\n"
            f"  hint: darsay list {slug}"
        )
    now = utc_now()
    catalog = {
        "catalog_schema_version": CATALOG_SCHEMA_VERSION,
        "kind": CATALOG_KIND,
        "id": slug,
        "title": title or slug,
        "curator": curator,
        "note": note,
        "created": now,
        "updated": now,
        "entries": [],
    }
    dest.mkdir(parents=True, exist_ok=True)
    save_catalog(path, catalog)
    _write_catalog_curation_template(dest, catalog)
    write_catalog_readme(dest, catalog)
    catalog["_path"] = str(path)
    return catalog


def _write_catalog_curation_template(catalog_dir: Path, catalog: dict) -> None:
    path = catalog_dir / "curation.md"
    if path.exists():
        return
    path.write_text(
        f"""# Curation notes — {catalog['title']}

_This is the curator's file: edit it freely. `darsay catalog regen` folds it
into README.md; nothing here is machine-generated after this template._

## Why these sources

_What this list is for._

## Personal notes

_Anything else worth remembering._
""",
        encoding="utf-8",
    )


def upsert_entry(
    catalog: dict,
    source: str,
    *,
    desire: int | None = None,
    note: str | None = None,
    revision: str | None = None,
    include: list[str] | None = None,
) -> tuple[dict, str]:
    """Insert or update by entry key. Returns (entry, 'added'|'updated'|'unchanged')."""
    ref = parse_source(source)
    key = entry_key(ref.canonical, revision, include)
    now = utc_now()
    for existing in catalog["entries"]:
        if entry_key(existing["source"], existing.get("revision"), existing.get("include")) == key:
            changed = False
            if desire is not None and existing.get("desire") != desire:
                existing["desire"] = desire
                changed = True
            if note is not None and existing.get("note") != note:
                existing["note"] = note
                changed = True
            if not changed:
                return existing, "unchanged"
            catalog["updated"] = now
            return existing, "updated"
    entry = {
        "source": ref.canonical,
        "revision": revision or None,
        "include": list(include) if include else None,
        "desire": desire,
        "note": note,
        "added": now,
        "estimate": None,
    }
    catalog["entries"].append(entry)
    catalog["updated"] = now
    return entry, "added"


def drop_entry(
    catalog: dict,
    source: str,
    *,
    revision: str | None = None,
    include: list[str] | None = None,
    include_given: bool = False,
    revision_given: bool = False,
) -> dict:
    ref = parse_source(source)
    canonical = ref.canonical
    source_matches = []
    for i, existing in enumerate(catalog["entries"]):
        parsed = try_parse_source(existing["source"])
        got = parsed.canonical if parsed is not None else existing["source"]
        if got == canonical or existing["source"] == source:
            source_matches.append(i)
    if not source_matches:
        raise SystemExit(f"error: {canonical} is not in this catalog")
    candidates = source_matches
    if include_given:
        candidates = [
            i for i in candidates
            if include_key(catalog["entries"][i].get("include")) == include_key(include)
        ]
    if revision_given:
        candidates = [
            i for i in candidates
            if (catalog["entries"][i].get("revision") or "") == (revision or "")
        ]
    if not include_given and not revision_given and len(source_matches) > 1:
        lines = []
        for i in source_matches:
            inc = catalog["entries"][i].get("include")
            rev = catalog["entries"][i].get("revision")
            extra = []
            if inc:
                extra.append("include=" + ",".join(inc))
            if rev:
                extra.append(f"revision={rev}")
            lines.append("  " + (" ".join(extra) if extra else "(full repo)"))
        raise SystemExit(
            f"error: {canonical} matches {len(source_matches)} entries in {catalog['id']}:\n"
            + "\n".join(lines)
            + "\n  pass --include GLOB to choose a subset, or --full for the full-repo row"
        )
    if len(candidates) != 1:
        raise SystemExit(f"error: {canonical} does not match a unique entry in {catalog['id']}")
    removed = catalog["entries"].pop(candidates[0])
    catalog["updated"] = utc_now()
    return removed


def adopt_entries(dest: dict, other: dict) -> tuple[int, int]:
    """Copy missing entries from other into dest. Returns (adopted, skipped)."""
    existing = {
        entry_key(e["source"], e.get("revision"), e.get("include"))
        for e in dest["entries"]
    }
    adopted = 0
    skipped = 0
    for entry in other["entries"]:
        parsed = try_parse_source(entry["source"])
        key = entry_key(entry["source"], entry.get("revision"), entry.get("include"))
        if key in existing:
            skipped += 1
            continue
        dest["entries"].append({
            "source": parsed.canonical if parsed is not None else entry["source"],
            "revision": entry.get("revision"),
            "include": list(entry["include"]) if entry.get("include") else None,
            "desire": entry.get("desire"),
            "note": entry.get("note"),
            "added": utc_now(),
            "estimate": entry.get("estimate"),
        })
        existing.add(key)
        adopted += 1
    if adopted:
        dest["updated"] = utc_now()
    return adopted, skipped


def row_matches_entry(row: dict, entry: dict) -> bool:
    if not row.get("source_address") or not entry.get("source"):
        return False
    row_ref = try_parse_source(row["source_address"])
    ent_ref = try_parse_source(entry["source"])
    if row_ref is None or ent_ref is None:
        return False
    if row_ref.canonical != ent_ref.canonical:
        return False
    want_rev = (entry.get("revision") or "").strip()
    if want_rev:
        got = row.get("revision") or ""
        got_ref = row.get("revision_ref") or ""
        if _HEX_REV.fullmatch(want_rev) and len(want_rev) >= MIN_REV_PREFIX:
            if not revisions_match(got, want_rev):
                return False
        elif want_rev != got and want_rev != got_ref:
            return False
    return include_key(entry.get("include")) == include_key(row.get("include"))


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def estimate_is_stale(as_of: str | None, *, now: datetime | None = None) -> bool:
    then = _parse_ts(as_of)
    if then is None:
        return False
    now = now or datetime.now(timezone.utc)
    return now - then > timedelta(days=STALE_AFTER_DAYS)


def _best_possession(entry: dict, matches: list[dict]) -> dict:
    est = entry.get("estimate") or {}
    stale = estimate_is_stale(est.get("as_of") if isinstance(est, dict) else None)
    gated = bool(est.get("gated")) if isinstance(est, dict) else False
    if not matches:
        payload = est.get("payload_bytes") if isinstance(est, dict) else None
        return {
            **entry,
            "status": "want",
            "bundle_id": None,
            "path": None,
            "partial": False,
            "integrity": None,
            "payload_bytes": payload,
            "on_disk_bytes": 0,
            "remaining_bytes": payload,
            "license": est.get("license") if isinstance(est, dict) else None,
            "gated": gated,
            "percent": None,
            "matched_revision": None,
            "matched_include": None,
            "estimate_stale": stale,
        }
    haves = [r for r in matches if not r.get("partial")]
    pool = haves or matches
    best = max(pool, key=lambda r: r.get("archived") or "")
    status = "have" if not best.get("partial") else "partial"
    payload = best.get("payload_bytes")
    if status != "have" and payload is None and isinstance(est, dict):
        payload = est.get("payload_bytes")
    remaining = 0 if status == "have" else best.get("remaining_bytes")
    return {
        **entry,
        "status": status,
        "bundle_id": best.get("bundle_id"),
        "path": best.get("path"),
        "partial": bool(best.get("partial")),
        "integrity": best.get("integrity"),
        "payload_bytes": payload,
        "on_disk_bytes": best.get("on_disk_bytes"),
        "remaining_bytes": remaining,
        "license": best.get("license") or (est.get("license") if isinstance(est, dict) else None),
        "gated": gated,
        "percent": best.get("percent"),
        "matched_revision": best.get("revision"),
        "matched_include": best.get("include"),
        "estimate_stale": stale,
    }


def overlay(catalog: dict, records: list[dict], *, progress=None) -> list[dict]:
    """View-rows: entry fields + status + matched bundle identity."""
    warn = progress or (lambda *a, **k: print(*a, **k))
    rows = []
    for entry in catalog["entries"]:
        ref = try_parse_source(entry["source"])
        if ref is None:
            warn(
                f"warning: unknown source provider in {entry['source']!r} — listing as unmatched",
                file=sys.stderr,
            )
            rows.append({
                **entry,
                "status": "unknown",
                "bundle_id": None,
                "path": None,
                "partial": False,
                "integrity": None,
                "payload_bytes": None,
                "on_disk_bytes": 0,
                "remaining_bytes": None,
                "license": None,
                "gated": False,
                "percent": None,
                "matched_revision": None,
                "matched_include": None,
                "estimate_stale": False,
            })
            continue
        matches = [r for r in records if row_matches_entry(r, entry)]
        rows.append(_best_possession(entry, matches))
    return rows


def vault_as_rows(records: list[dict]) -> list[dict]:
    """Project inventory records onto the catalog table columns."""
    rows = []
    for rec in records:
        status = rec.get("status") or ("partial" if rec.get("partial") else "have")
        remaining = rec.get("remaining_bytes")
        if status == "have":
            remaining = 0
        rows.append({
            "status": status,
            "desire": None,
            "source": rec.get("source_address") or "—",
            "revision": rec.get("revision"),
            "include": rec.get("include"),
            "note": None,
            "bundle_id": rec.get("bundle_id"),
            "path": rec.get("path"),
            "partial": rec.get("partial"),
            "integrity": rec.get("integrity"),
            "payload_bytes": rec.get("payload_bytes"),
            "on_disk_bytes": rec.get("on_disk_bytes"),
            "remaining_bytes": remaining,
            "license": rec.get("license"),
            "gated": False,
            "percent": rec.get("percent"),
            "matched_revision": rec.get("revision"),
            "matched_include": rec.get("include"),
            "estimate_stale": False,
            "estimate": None,
        })
    return rows


def next_entry(rows: list[dict], *, desire: bool) -> dict | None:
    """Unfinished = want|partial only (unknown is skipped).

    ``desire=True`` (catalog ``--next``): partials before wants, then higher
    desire. ``desire=False`` (vault ``--next``): largest remaining partial.
    """
    unfinished = [r for r in rows if r.get("status") in ("want", "partial")]
    if not unfinished:
        return None
    if desire:
        def key(row):
            d = row.get("desire")
            return (
                0 if row.get("status") == "partial" else 1,
                d is None,
                -(d or 0),
                row.get("source") or "",
            )
        return sorted(unfinished, key=key)[0]
    partials = [r for r in unfinished if r.get("status") == "partial"]
    if not partials:
        return None
    return max(partials, key=lambda r: r.get("remaining_bytes") or 0)


def next_idle_message(catalog: dict, rows: list[dict]) -> tuple[str, bool]:
    """Why ``next_entry`` is None. Returns (message without ``error:``, is_error)."""
    ident = catalog["id"]
    if not catalog.get("entries"):
        return (
            f"catalog {ident} is empty\n"
            f"  hint: darsay catalog add {ident} huggingface:owner/name --desire 8",
            True,
        )
    unknowns = [r for r in rows if r.get("status") == "unknown"]
    unfinished = [r for r in rows if r.get("status") in ("want", "partial")]
    haves = sum(1 for r in rows if r.get("status") == "have")
    if not unfinished and unknowns:
        return (
            f"cannot archive from {ident} — remaining entries use an unknown source provider",
            True,
        )
    return (
        f"nothing missing in catalog {ident} — {haves} have, every entry is in the vault",
        False,
    )


def sort_rows(rows: list[dict], sort: str) -> list[dict]:
    if sort == "next":
        def key(row):
            status = row.get("status")
            group = {"partial": 0, "want": 1, "have": 2}.get(status, 3)
            d = row.get("desire")
            return (
                group,
                d is None,
                -(d or 0),
                (row.get("source") or "").lower(),
            )
        return sorted(rows, key=key)
    if sort == "desire":
        return sorted(
            rows,
            key=lambda r: (r.get("desire") is None, -(r.get("desire") or 0), (r.get("source") or "").lower()),
        )
    if sort == "size":
        return sorted(rows, key=lambda r: (-(r.get("remaining_bytes") if r.get("remaining_bytes") is not None else -1), (r.get("source") or "").lower()))
    if sort == "name":
        return sorted(rows, key=lambda r: (r.get("source") or "").lower())
    if sort == "status":
        order = {"have": 0, "partial": 1, "want": 2, "unknown": 3}
        return sorted(
            rows,
            key=lambda r: (
                order.get(r.get("status"), 9),
                r.get("desire") is None,
                -(r.get("desire") or 0),
                (r.get("source") or "").lower(),
            ),
        )
    return list(rows)


def filter_want(rows: list[dict]) -> list[dict]:
    return [r for r in rows if r.get("status") in ("want", "partial")]


def overlay_stats(rows: list[dict]) -> dict:
    have = sum(1 for r in rows if r.get("status") == "have")
    partial = sum(1 for r in rows if r.get("status") == "partial")
    want = sum(1 for r in rows if r.get("status") == "want")
    unknown = sum(1 for r in rows if r.get("status") == "unknown")
    remaining = 0
    remaining_unknown = False
    on_disk = 0
    as_ofs = []
    for row in rows:
        on_disk += row.get("on_disk_bytes") or 0
        est = row.get("estimate") if isinstance(row.get("estimate"), dict) else None
        if est and est.get("as_of"):
            as_ofs.append(est["as_of"])
        if row.get("status") in ("want", "partial"):
            rem = row.get("remaining_bytes")
            if rem is None:
                remaining_unknown = True
            else:
                remaining += rem
            if est and (est.get("unknown_size_count") or 0) > 0:
                remaining_unknown = True
    oldest = min(as_ofs) if as_ofs else None
    return {
        "sources": len(rows),
        "have": have,
        "partial": partial,
        "want": want,
        "unknown": unknown,
        "remaining_bytes": remaining,
        "remaining_unknown": remaining_unknown,
        "on_disk_bytes": on_disk,
        "oldest_estimate_as_of": oldest,
    }


def _estimate_age_label(rows: list[dict], stats: dict) -> str:
    as_ofs = []
    for row in rows:
        est = row.get("estimate") if isinstance(row.get("estimate"), dict) else None
        if est and est.get("as_of"):
            as_ofs.append(est["as_of"])
    if not as_ofs:
        return "no estimates"
    if any(estimate_is_stale(a) for a in as_ofs):
        then = _parse_ts(stats.get("oldest_estimate_as_of"))
        if then is None:
            return "stale"
        days = max(0, (datetime.now(timezone.utc) - then).days)
        return f"{days}d old"
    return "fresh"


def _remaining_label(stats: dict) -> str:
    known = stats.get("remaining_bytes") or 0
    unknown = stats.get("remaining_unknown")
    if known and unknown:
        return f"{human_size(known)} + ? remaining"
    if unknown and not known:
        return "? remaining"
    return f"{human_size(known)} remaining"


def _show_remaining(stats: dict) -> bool:
    return bool(
        stats.get("partial")
        or stats.get("want")
        or stats.get("remaining_bytes")
        or stats.get("remaining_unknown")
    )


def catalog_header_line(catalog: dict, stats: dict, rows: list[dict]) -> str:
    unknown = f" · {stats['unknown']} unknown" if stats.get("unknown") else ""
    age = _estimate_age_label(rows, stats)
    remaining = f"  ·  {_remaining_label(stats)}" if _show_remaining(stats) else ""
    return (
        f"Catalog {catalog['id']}  ·  {catalog.get('title') or catalog['id']}  ·  "
        f"{stats['sources']} sources  ·  {stats['have']} have · {stats['partial']} partial · "
        f"{stats['want']} want{unknown}{remaining}  ·  estimates {age}"
    )


def vault_header_line(vault: Path, stats: dict) -> str:
    on_disk = human_size(stats.get("on_disk_bytes") or 0)
    remaining = f"  ·  {_remaining_label(stats)}" if stats.get("partial") else ""
    return (
        f"Vault {vault}  ·  {stats['have']} have · {stats['partial']} partial  ·  "
        f"{on_disk} on disk{remaining}"
    )


def format_source_cell(row: dict) -> str:
    src = row.get("source") or "—"
    extras = []
    rev = row.get("revision")
    if rev:
        extras.append("@" + str(rev)[:12])
    include = row.get("include") or []
    if include:
        extras.append("[" + ", ".join(include) + "]")
    if not extras:
        return src
    return src + "  " + " ".join(extras)


def format_have_cell(row: dict) -> str:
    if row.get("status") in ("want", "unknown") or not row.get("bundle_id"):
        return "—"
    have = row["bundle_id"]
    if row.get("status") == "partial" and row.get("percent") is not None:
        return f"{have}  {row['percent']}%"
    return have


def format_size_cell(row: dict) -> str:
    payload = row.get("payload_bytes")
    if payload is None and row.get("status") != "have":
        size = "?"
    else:
        size = human_size(payload)
        if row.get("estimate_stale"):
            size += "*"
    if row.get("gated"):
        size += "  GATED"
    return size


def format_desire_cell(row: dict) -> str:
    d = row.get("desire")
    return str(d) if d is not None else "—"


def format_note_cell(row: dict) -> str:
    note = row.get("note")
    return note if note else "—"


def _print_table(header: tuple[str, ...], cells: list[tuple[str, ...]]) -> None:
    rows = [header, *cells]
    widths = [max(len(str(r[i])) for r in rows) for i in range(len(header))]
    for row in rows:
        print("  ".join(str(v).ljust(w) for v, w in zip(row, widths)))


def print_catalog_table(rows: list[dict], *, header_line: str | None = None) -> None:
    if header_line:
        print()
        print(header_line)
        print()
    specs = (
        ("STATUS", lambda r: r.get("status") or "—"),
        ("DESIRE", format_desire_cell),
        ("SOURCE", format_source_cell),
        ("HAVE", format_have_cell),
        ("SIZE", format_size_cell),
        ("NOTE", format_note_cell),
    )
    raw = [[fmt(r) for _, fmt in specs] for r in rows]
    keep = []
    for i, (name, _) in enumerate(specs):
        if name in _HIDE_IF_EMPTY and (not raw or all(row[i] in ("—", "") for row in raw)):
            continue
        keep.append(i)
    header = tuple(specs[i][0] for i in keep)
    cells = [tuple(row[i] for i in keep) for row in raw]
    _print_table(header, cells)


def print_catalog_index(catalogs: list[dict]) -> None:
    header = ("CATALOG", "TITLE", "CURATOR", "SOURCES", "UPDATED")
    cells = []
    for cat in catalogs:
        updated = (cat.get("updated") or "")[:10] or "—"
        cells.append((
            cat["id"],
            cat.get("title") or cat["id"],
            cat.get("curator") or "—",
            str(len(cat.get("entries") or [])),
            updated,
        ))
    _print_table(header, cells)


def realize_from_overlay(row: dict, entry: dict | None = None) -> tuple[str, str | None, list[str] | None]:
    """Partial → matched row's full revision + include. Want → entry fields."""
    entry = entry or row
    source = entry["source"]
    if row.get("status") == "partial":
        return source, row.get("matched_revision"), row.get("matched_include")
    rev = entry.get("revision") or None
    inc = entry.get("include")
    return source, rev, list(inc) if inc else None


def format_archive_command(
    source: str,
    revision: str | None = None,
    include: list[str] | None = None,
    *,
    vault: str | Path | None = None,
) -> str:
    """Copy-pasteable ``darsay archive`` line, including pin and include globs."""
    parts = ["darsay"]
    if vault is not None:
        parts += ["--vault", str(vault)]
    parts += ["archive", source]
    if revision:
        parts += ["--revision", str(revision)]
    for glob in include or []:
        parts += ["--include", glob]
    return shlex.join(parts)


def overlay_envelope(catalog: dict, vault: Path, rows: list[dict]) -> dict:
    stats = overlay_stats(rows)
    return {
        "catalog": {
            "id": catalog["id"],
            "title": catalog.get("title"),
            "path": catalog.get("_path"),
            "curator": catalog.get("curator"),
        },
        "vault": str(vault),
        "stats": stats,
        "entries": rows,
    }


def write_catalog_readme(catalog_dir: Path, catalog: dict) -> None:
    """Generated view of intent + cached estimates. Never overlay."""
    title = catalog.get("title") or catalog["id"]
    n = len(catalog.get("entries") or [])
    curator = catalog.get("curator")
    curator_line = f" Curator: {curator}." if curator else ""
    lines = [
        f"# {title}",
        "",
        f"A darsay catalog (`{catalog['id']}`). {n} source{'s' if n != 1 else ''}.{curator_line}",
        "",
    ]
    if catalog.get("note"):
        lines += [f"> {catalog['note']}", ""]
    lines += [
        "| Desire | Source | Type | Size (cached) | License | Note |",
        "|---|---|---|---|---|---|",
    ]
    for entry in catalog.get("entries") or []:
        est = entry.get("estimate") if isinstance(entry.get("estimate"), dict) else {}
        desire = entry.get("desire")
        desire_s = str(desire) if desire is not None else "—"
        src = entry["source"]
        href = src
        parsed = try_parse_source(src)
        if parsed is not None:
            href = f"[{src}]({parsed.url})"
        extras = []
        if entry.get("revision"):
            extras.append("@" + str(entry["revision"])[:12])
        if entry.get("include"):
            extras.append("`" + ", ".join(entry["include"]) + "`")
        if extras:
            href = href + " " + " ".join(extras)
        artifact = est.get("artifact_type") or "—"
        if est.get("payload_bytes") is None:
            size = "?"
        else:
            as_of = (est.get("as_of") or "")[:10]
            size = f"{human_size(est['payload_bytes'])}" + (f" (as of {as_of})" if as_of else "")
        license_s = est.get("license") or "?"
        if est.get("gated"):
            license_s = f"{license_s} (gated)" if license_s != "?" else "? (gated)"
        note = entry.get("note") or ""
        lines.append(f"| {desire_s} | {href} | {artifact} | {size} | {license_s} | {note} |")
    lines += [
        "",
        "Sizes are last-estimated facts, not live Hub queries. Overlay against a vault:",
        "",
        "    darsay list ./catalog.json",
        "    darsay archive --next ./catalog.json",
        "",
    ]
    body = _curation_body(catalog_dir)
    if body:
        lines += ["## Curation", "", body, ""]
    (catalog_dir / "README.md").write_text("\n".join(lines), encoding="utf-8")

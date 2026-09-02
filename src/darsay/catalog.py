"""Catalogs: shareable want-lists the vault is a view of.

A catalog is a curated list of works. Overlay against ``bundle_records``
yields have / partial / want / closed. Possession is a view; ``archive``
does not rewrite catalog.json.

A work's ``source`` is a provider ref darsay can fetch, or — for a work
with no downloadable release yet (an API-only model, an announced
release, a host with no provider) — its home URL. The second kind is a
**closed** work: it holds its place in a family with no price and nothing
to fetch, until the address becomes a source ref.
"""

from __future__ import annotations

import json
import re
import shlex
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .archiver import utc_now
from .lineage import display_generation, group_by_family, lineage_of_source
from .readme_gen import _curation_body, human_size
from .sources import parse_source

CATALOG_SCHEMA_VERSION = "2.0.0"
CATALOG_SCHEMA_MAJOR = 2
CATALOG_KIND = "darsay.catalog"
STALE_AFTER_DAYS = 7
CATALOGS_DIRNAME = "catalogs"
SLUG_RE = re.compile(r"^[a-z][a-z0-9._-]{0,63}$")
DESIRE_MIN, DESIRE_MAX = 1, 9
MIN_REV_PREFIX = 12
_HEX_REV = re.compile(r"^[0-9a-f]+$", re.IGNORECASE)
DIGEST_KEYS = frozenset(
    {
        "as_of",
        "artifact_type",
        "revision",
        "revision_ref",
        "payload_bytes",
        "file_count",
        "license",
        "gated",
        "parameters",
        "dominant_dtype",
        "unknown_size_count",
        "hints",
        "policy",
        # Since 2.0.0: the release precision and what it spends per weight,
        # the architecture, and parent edges as upstream declares them.
        "precision",
        "bytes_per_param",
        "architecture",
        "parents",
    }
)
# ``large``: the priced payload is at least this big — more than one sitting,
# and often more than one disk. darsay.io's board draws the same line.
LARGE_PAYLOAD_BYTES = 20 * 1024**3
# The closed vocabulary of ``estimate.hints``. Sorted on write; readers ignore
# names they do not know. The digest's ``policy`` key is ``"negatives"`` when
# the stored price is the default acquisition — the negative set.
HINTS = ("gated", "large", "quant", "redundant", "subset")
_FULL_FIDELITY_DTYPES = frozenset({"F64", "F32", "F16", "BF16"})
_QUANT_FORMATS = frozenset({"gguf"})
# Bytes per parameter for the redundancy expectation; an unlisted dtype
# contributes no expectation (never a guess).
_DTYPE_WIDTHS = {
    "F64": 8,
    "I64": 8,
    "F32": 4,
    "I32": 4,
    "F16": 2,
    "BF16": 2,
    "I16": 2,
    "U16": 2,
    "F8_E4M3": 1,
    "F8_E5M2": 1,
    "I8": 1,
    "U8": 1,
    "BOOL": 1,
}
# Weight bytes at or above this multiple of one copy smell like several
# weight sets in one repo; an exact second copy is 2.0x.
REDUNDANT_FACTOR = 1.75
_CATALOG_TOP_KEYS = (
    "catalog_schema_version",
    "kind",
    "id",
    "title",
    "curator",
    "note",
    "created",
    "updated",
    "entries",
)
_HIDE_IF_EMPTY = frozenset({"DESIRE", "NOTE", "HINTS", "FAMILY", "PRECISION"})


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


def is_home(source: str) -> bool:
    """A closed work's address: an https URL on a host with no provider."""
    s = (source or "").strip()
    return s.lower().startswith(("https://", "http://")) and try_parse_source(s) is None


def canonical_source(source: str) -> str:
    """A provider ref's canonical form; a home URL stripped of its fragment."""
    if is_home(source):
        return source.strip().split("#", 1)[0].rstrip("/")
    return parse_source(source).canonical


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


def expected_weight_bytes(params_by_dtype) -> int | None:
    """One copy's weight bytes from published parameter counts, or None.

    Any dtype without a known width makes the expectation unknowable —
    the ``redundant`` hint then stays off rather than guess.
    """
    if not isinstance(params_by_dtype, dict) or not params_by_dtype:
        return None
    total = 0
    for dtype, count in params_by_dtype.items():
        width = _DTYPE_WIDTHS.get(str(dtype).upper())
        if width is None or not isinstance(count, int) or isinstance(count, bool):
            return None
        total += count * width
    return total or None


def _hints(
    *,
    payload_bytes,
    gated,
    subset: bool,
    dominant_dtype,
    dominant_format,
    weights_bytes=None,
    params_by_dtype=None,
) -> list[str]:
    out: set[str] = set()
    if gated:
        out.add("gated")
    if (
        isinstance(payload_bytes, int)
        and not isinstance(payload_bytes, bool)
        and payload_bytes >= LARGE_PAYLOAD_BYTES
    ):
        out.add("large")
    if subset:
        out.add("subset")
    if (
        isinstance(dominant_format, str) and dominant_format.lower() in _QUANT_FORMATS
    ) or (
        isinstance(dominant_dtype, str)
        and dominant_dtype.upper() not in _FULL_FIDELITY_DTYPES
    ):
        out.add("quant")
    expected = expected_weight_bytes(params_by_dtype)
    if (
        expected
        and isinstance(weights_bytes, int)
        and not isinstance(weights_bytes, bool)
        and weights_bytes >= expected * REDUNDANT_FACTOR
    ):
        out.add("redundant")
    return sorted(out)


def _clean_hints(raw) -> list[str]:
    """Keep only names in HINTS, deduplicated and sorted. Unknown names are ignored."""
    if not isinstance(raw, list):
        return []
    return sorted({h for h in raw if isinstance(h, str) and h in HINTS})


def hints_for(est: dict) -> list[str]:
    """Closed-vocabulary facts about a priced source, from a live ``estimate()`` dict.

    Returns a sorted list drawn from ``HINTS`` — ``gated``, ``large``,
    ``quant``, ``redundant``, ``subset`` — possibly empty. Empty means
    "nothing notable"; it is not the same as a missing digest
    (``estimate: null``), which means unknown. Nothing here is guessed
    from a repo name.

    - ``gated``  — ``source.gated`` is truthy.
    - ``large``  — ``payload.total_size_bytes`` ≥ ``LARGE_PAYLOAD_BYTES``
      (20 GiB). The priced payload is the selection when ``--include``
      or the negatives policy applied. An unknown size is never large.
    - ``quant``  — a published quantized artifact in the sense of
      QUANTIZATION.md: the weight bytes are mostly GGUF
      (``payload.dominant_format``), or the dominant safetensors dtype is
      not a full-fidelity float (F64 / F32 / F16 / BF16). Unknown dtype and
      format ⇒ no hint.
    - ``redundant`` — the priced weight bytes are ≥ ``REDUNDANT_FACTOR``×
      one copy at the published per-dtype parameter counts: the repo
      likely holds several weight sets. Unknown params or dtype widths ⇒
      no hint. Live estimates only; never re-derived from a digest.
    - ``subset`` — the estimate was priced with an explicit ``--include``.
      A negatives-policy selection is the default acquisition, not a
      curator subset: it sets the digest's ``policy`` key instead.
    """
    payload = est.get("payload") if isinstance(est.get("payload"), dict) else {}
    source = est.get("source") if isinstance(est.get("source"), dict) else {}
    params = est.get("parameters") if isinstance(est.get("parameters"), dict) else {}
    subset = est.get("subset") if isinstance(est.get("subset"), dict) else None
    weights = payload.get("weights") if isinstance(payload.get("weights"), dict) else {}
    return _hints(
        payload_bytes=payload.get("total_size_bytes"),
        gated=source.get("gated"),
        subset=subset is not None and not subset.get("policy"),
        dominant_dtype=params.get("dominant_dtype"),
        dominant_format=payload.get("dominant_format"),
        weights_bytes=weights.get("bytes"),
        params_by_dtype=params.get("by_dtype"),
    )


def derive_hints(digest, entry: dict | None = None) -> list[str]:
    """Hints for a stored digest. Stored ``hints`` win; older digests are derived.

    A 1.0.0 digest has no ``hints``: ``large`` and ``gated`` come out exactly,
    ``subset`` from the entry's include globs, and ``quant`` only from
    ``dominant_dtype`` — the GGUF signal lives in the live estimate, so a
    GGUF row shows ``quant`` after ``darsay estimate CATALOG`` refreshes it.
    """
    if not isinstance(digest, dict):
        return []
    if isinstance(digest.get("hints"), list):
        return _clean_hints(digest["hints"])
    include = entry.get("include") if isinstance(entry, dict) else None
    return _hints(
        payload_bytes=digest.get("payload_bytes"),
        gated=digest.get("gated"),
        subset=bool(include),
        dominant_dtype=digest.get("dominant_dtype"),
        dominant_format=None,
    )


def entry_hints(entry: dict) -> list[str]:
    """Stored hints when the digest carries them, else derived from what it has."""
    if not isinstance(entry, dict):
        return []
    return derive_hints(entry.get("estimate"), entry)


def estimate_digest(est: dict) -> dict:
    """Projection of estimate() onto DIGEST_KEYS. Not a subset of the live dict."""
    params = est.get("parameters") or {}
    precision = est.get("precision") if isinstance(est.get("precision"), dict) else {}
    lineage = est.get("lineage") if isinstance(est.get("lineage"), dict) else {}
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
        "dominant_dtype": params.get("dominant_dtype")
        if isinstance(params, dict)
        else None,
        "unknown_size_count": est["payload"]["unknown_size_count"],
        "hints": hints_for(est),
        # The negatives marker: the CLI classified this row and the stored
        # price is its negative set — whether or not a print was skipped.
        # Absent for --full / --include prices, datasets, and existing pins.
        "policy": "negatives"
        if isinstance(est.get("classification"), dict)
        or (est.get("subset") or {}).get("policy")
        else None,
        "precision": precision.get("label"),
        "bytes_per_param": precision.get("bytes_per_param"),
        "architecture": lineage.get("architecture"),
        "parents": _clean_parents(lineage.get("parents")),
    }
    assert set(digest) == DIGEST_KEYS
    return digest


def _clean_parents(raw) -> list[dict] | None:
    """Parent edges as ``[{source, relation}]``; anything else is dropped."""
    if not isinstance(raw, list):
        return None
    out = []
    for edge in raw:
        if not isinstance(edge, dict) or not isinstance(edge.get("source"), str):
            continue
        relation = edge.get("relation")
        out.append(
            {
                "source": edge["source"],
                "relation": relation if isinstance(relation, str) else None,
            }
        )
    return out or None


def project_stored_estimate(est) -> dict | None:
    """Keep only DIGEST_KEYS. Live ``estimate()`` dicts are digested; disk paths never persist."""
    if not isinstance(est, dict):
        return None
    if isinstance(est.get("payload"), dict) and isinstance(est.get("source"), dict):
        try:
            return estimate_digest(est)
        except (AssertionError, KeyError, TypeError):
            pass
    out = {k: est[k] for k in DIGEST_KEYS if k in est}
    if "hints" in out:
        out["hints"] = _clean_hints(out["hints"])
    if "parents" in out:
        out["parents"] = _clean_parents(out["parents"])
    return out


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
    if raw.lower().startswith(("https://", "http://")):
        raise SystemExit(
            "error: a board URL works with estimate, list, archive --next, "
            "and catalog add/drop/adopt\n"
            "  hint: darsay catalog adopt <name> <board-url> makes a local "
            "copy for everything else"
        )
    found = try_resolve_catalog(vault, raw)
    if found is not None:
        catalog = load_catalog(found)
        parent_name = found.parent.name
        if (
            found.parent != Path(raw).expanduser()
            and SLUG_RE.fullmatch(fold_slug(raw))
            and fold_slug(parent_name) != fold_slug(catalog["id"])
        ):
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


def require_writable(
    vault: Path, path: Path, write_flag: bool, *, hint: str | None = None
) -> None:
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
        raise SystemExit(
            f"error: unreadable catalog at {path}: catalog_schema_version missing"
        )
    try:
        major = int(str(version).split(".", 1)[0])
    except ValueError:
        raise SystemExit(
            f"error: unreadable catalog at {path}: catalog_schema_version {version!r}"
        ) from None
    if major != CATALOG_SCHEMA_MAJOR:
        raise SystemExit(
            f"error: catalog schema {version} is not {CATALOG_SCHEMA_MAJOR}.x — this "
            "darsay reads 2.x catalogs; re-add the entries to a new catalog"
        )
    if data.get("kind") != CATALOG_KIND:
        raise SystemExit(
            f"error: unreadable catalog at {path}: kind is not {CATALOG_KIND!r}"
        )
    ident = data.get("id")
    if not isinstance(ident, str) or not SLUG_RE.fullmatch(fold_slug(ident)):
        raise SystemExit(f"error: unreadable catalog at {path}: invalid id")
    entries = data.get("entries")
    if entries is None:
        entries = []
    if not isinstance(entries, list):
        raise SystemExit(
            f"error: unreadable catalog at {path}: entries must be an array"
        )
    cleaned = []
    for i, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise SystemExit(
                f"error: unreadable catalog at {path}: entry {i} is not an object"
            )
        source = entry.get("source")
        if not isinstance(source, str) or not source.strip():
            raise SystemExit(
                f"error: unreadable catalog at {path}: entry {i} missing source"
            )
        desire = entry.get("desire")
        if desire is not None and (
            not isinstance(desire, int) or desire < DESIRE_MIN or desire > DESIRE_MAX
        ):
            raise SystemExit(
                f"error: unreadable catalog at {path}: entry {i} desire must be {DESIRE_MIN}–{DESIRE_MAX} or null"
            )
        include = entry.get("include")
        if include is not None and (
            not isinstance(include, list)
            or not all(isinstance(g, str) for g in include)
        ):
            raise SystemExit(
                f"error: unreadable catalog at {path}: entry {i} include must be a list of strings or null"
            )
        cleaned.append(
            {
                "source": source,
                "revision": entry.get("revision"),
                "include": include,
                "desire": desire,
                "note": entry.get("note"),
                "added": entry.get("added"),
                "estimate": project_stored_estimate(entry.get("estimate")),
            }
        )
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
        # The tool writes the schema it conforms to; 1.x is additive, so a
        # 1.0.0 file re-saved here is a valid 1.1.0 file.
        "catalog_schema_version": CATALOG_SCHEMA_VERSION,
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
    dry_run: bool = False,
) -> dict:
    """Create ``<vault>/catalogs/<slug>/``; ``dry_run`` validates and writes nothing."""
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
    if not dry_run:
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
        f"""# Curation notes — {catalog["title"]}

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
    """Insert or update by entry key. Returns (entry, 'added'|'updated'|'unchanged').

    A home URL (``is_home``) is accepted as a closed work; it carries no
    revision or include, since there is nothing to fetch.
    """
    canonical = canonical_source(source)
    if is_home(source) and (revision or include):
        raise SystemExit(
            "error: a closed work (a home URL) has nothing to pin or include — "
            "drop --revision / --include, or give a source ref"
        )
    key = entry_key(canonical, revision, include)
    now = utc_now()
    for existing in catalog["entries"]:
        if (
            entry_key(
                existing["source"], existing.get("revision"), existing.get("include")
            )
            == key
        ):
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
        "source": canonical,
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


def adopt_resolved_source(catalog: dict, entry: dict, address: str) -> bool:
    """Rewrite ``entry.source`` to the pin-resolved canonical if it does not collide.

    Estimate/archive may expand a model-shaped Hub shorthand to
    ``huggingface:datasets/owner/name`` when only a dataset exists.
    Catalog identity is that canonical; rewrite here so overlay matches
    the bundle that pin will write. Returns True when the source changed.
    """
    if not address or address == entry.get("source"):
        return False
    new_key = entry_key(address, entry.get("revision"), entry.get("include"))
    for other in catalog.get("entries") or []:
        if other is entry:
            continue
        if (
            entry_key(other.get("source"), other.get("revision"), other.get("include"))
            == new_key
        ):
            return False
    entry["source"] = address
    return True


def drop_entry(
    catalog: dict,
    source: str,
    *,
    revision: str | None = None,
    include: list[str] | None = None,
    include_given: bool = False,
    revision_given: bool = False,
) -> dict:
    canonical = canonical_source(source)
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
            i
            for i in candidates
            if include_key(catalog["entries"][i].get("include")) == include_key(include)
        ]
    if revision_given:
        candidates = [
            i
            for i in candidates
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
        raise SystemExit(
            f"error: {canonical} does not match a unique entry in {catalog['id']}"
        )
    removed = catalog["entries"].pop(candidates[0])
    catalog["updated"] = utc_now()
    return removed


def adoptable_entries(dest: dict, other: dict) -> tuple[list[dict], int]:
    """Entries of ``other`` that ``dest`` lacks, and how many it already has.

    The entries come back as fresh copies ready to append; nothing is changed.
    """
    existing = {
        entry_key(e["source"], e.get("revision"), e.get("include"))
        for e in dest["entries"]
    }
    new_entries: list[dict] = []
    skipped = 0
    for entry in other["entries"]:
        parsed = try_parse_source(entry["source"])
        key = entry_key(entry["source"], entry.get("revision"), entry.get("include"))
        if key in existing:
            skipped += 1
            continue
        new_entries.append(
            {
                "source": parsed.canonical if parsed is not None else entry["source"],
                "revision": entry.get("revision"),
                "include": list(entry["include"]) if entry.get("include") else None,
                "desire": entry.get("desire"),
                "note": entry.get("note"),
                "added": utc_now(),
                "estimate": entry.get("estimate"),
            }
        )
        existing.add(key)
    return new_entries, skipped


def adopt_entries(dest: dict, other: dict) -> tuple[int, int]:
    """Copy missing entries from other into dest. Returns (adopted, skipped)."""
    new_entries, skipped = adoptable_entries(dest, other)
    dest["entries"].extend(new_entries)
    if new_entries:
        dest["updated"] = utc_now()
    return len(new_entries), skipped


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
    if include_key(entry.get("include")) == include_key(row.get("include")):
        return True
    # ``include: null`` means "the default acquisition": a negatives-policy
    # bundle is what ``archive <source>`` produces for it, so it satisfies
    # the want (as a full-repo bundle, a superset, also does above).
    return not entry.get("include") and bool(row.get("policy"))


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
            **_lineage_fields(entry),
            "hints": entry_hints(entry),
            "status": "want",
            "bundle_id": None,
            "path": None,
            "partial": False,
            "integrity": None,
            "payload_bytes": payload,
            "on_disk_bytes": 0,
            "remaining_bytes": payload,
            "moved_bytes": 0,
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
        **_lineage_fields(entry),
        "hints": entry_hints(entry),
        "status": status,
        "bundle_id": best.get("bundle_id"),
        "path": best.get("path"),
        "partial": bool(best.get("partial")),
        "integrity": best.get("integrity"),
        "payload_bytes": payload,
        "on_disk_bytes": best.get("on_disk_bytes"),
        "remaining_bytes": remaining,
        "moved_bytes": best.get("moved_bytes") if status != "have" else 0,
        "license": best.get("license")
        or (est.get("license") if isinstance(est, dict) else None),
        "gated": gated,
        "percent": best.get("percent"),
        "matched_revision": best.get("revision"),
        "matched_include": best.get("include"),
        "estimate_stale": stale,
    }


def _lineage_fields(entry: dict) -> dict:
    """Name-derived lineage, the digest's precision, and a closed work's home."""
    source = entry.get("source") or ""
    est = entry.get("estimate") if isinstance(entry.get("estimate"), dict) else {}
    return {
        "lineage": lineage_of_source(source).as_dict(),
        "precision": est.get("precision"),
        "bytes_per_param": est.get("bytes_per_param"),
        "home": source if is_home(source) else None,
    }


def overlay(catalog: dict, records: list[dict], *, progress=None) -> list[dict]:
    """View-rows: entry fields + status + matched bundle identity.

    ``progress`` is accepted for callers that pass it; overlay itself has
    nothing to report — a closed work is a row, not a warning.
    """
    rows = []
    for entry in catalog["entries"]:
        ref = try_parse_source(entry["source"])
        if ref is None:
            # A closed work: nothing to fetch, nothing to match, a place held.
            rows.append(
                {
                    **entry,
                    **_lineage_fields(entry),
                    "hints": entry_hints(entry),
                    "status": "closed",
                    "bundle_id": None,
                    "path": None,
                    "partial": False,
                    "integrity": None,
                    "payload_bytes": None,
                    "on_disk_bytes": 0,
                    "remaining_bytes": None,
                    "moved_bytes": None,
                    "license": None,
                    "gated": False,
                    "percent": None,
                    "matched_revision": None,
                    "matched_include": None,
                    "estimate_stale": False,
                }
            )
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
        rows.append(
            {
                "status": status,
                "desire": None,
                "source": rec.get("source_address") or "—",
                "revision": rec.get("revision"),
                "include": rec.get("include"),
                "note": None,
                "bundle_id": rec.get("bundle_id"),
                "path": rec.get("path"),
                "policy": rec.get("policy"),
                "partial": rec.get("partial"),
                "integrity": rec.get("integrity"),
                "payload_bytes": rec.get("payload_bytes"),
                "on_disk_bytes": rec.get("on_disk_bytes"),
                "remaining_bytes": remaining,
                "moved_bytes": rec.get("moved_bytes"),
                "license": rec.get("license"),
                "gated": False,
                "percent": rec.get("percent"),
                "matched_revision": rec.get("revision"),
                "matched_include": rec.get("include"),
                "estimate_stale": False,
                "estimate": None,
                "hints": [],
                "artifact_type": rec.get("artifact_type"),
                **_lineage_fields({"source": rec.get("source_address") or ""}),
            }
        )
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
    closed = [r for r in rows if r.get("status") == "closed"]
    unfinished = [r for r in rows if r.get("status") in ("want", "partial")]
    haves = sum(1 for r in rows if r.get("status") == "have")
    if not unfinished and closed:
        return (
            f"nothing to fetch from {ident} — the remaining {len(closed)} "
            f"row{'s are' if len(closed) != 1 else ' is'} closed (no source to "
            "fetch from yet)",
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
            key=lambda r: (
                r.get("desire") is None,
                -(r.get("desire") or 0),
                (r.get("source") or "").lower(),
            ),
        )
    if sort == "size":
        return sorted(
            rows,
            key=lambda r: (
                -(
                    r.get("remaining_bytes")
                    if r.get("remaining_bytes") is not None
                    else -1
                ),
                (r.get("source") or "").lower(),
            ),
        )
    if sort == "name":
        return sorted(rows, key=lambda r: (r.get("source") or "").lower())
    if sort == "family":
        # The tree, flattened: family, then generation (oldest first), then
        # size within a generation — the order the lineage view reads in.
        def key(row):
            lin = lineage_of_source(row.get("source") or "")
            return (
                lin.family_key is None,
                lin.family_key or "",
                lin.generation_key,
                lin.size_total or 0,
                (lin.member or "").lower(),
                (row.get("source") or "").lower(),
            )

        return sorted(rows, key=key)
    if sort == "status":
        order = {"have": 0, "partial": 1, "want": 2, "closed": 3}
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
    closed = sum(1 for r in rows if r.get("status") == "closed")
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
        "closed": closed,
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
    unknown = f" · {stats['closed']} closed" if stats.get("closed") else ""
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


def format_type_cell(row: dict) -> str:
    est = row.get("estimate") if isinstance(row.get("estimate"), dict) else None
    if est and est.get("artifact_type"):
        return str(est["artifact_type"])
    rec_type = row.get("artifact_type")
    if rec_type:
        return str(rec_type)
    parsed = try_parse_source(row.get("source") or "")
    if parsed is not None:
        return parsed.artifact_type
    return "—"


def format_source_cell(row: dict) -> str:
    src = row.get("source") or "—"
    extras = []
    rev = row.get("revision")
    if rev:
        extras.append("@" + str(rev)[:12])
    if row.get("policy"):
        # The default acquisition; the exact selection is in the manifest.
        extras.append(f"[{row['policy']}]")
    else:
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
    if row.get("status") == "closed":
        return "closed"
    if payload is None and row.get("status") != "have":
        size = "?"
    else:
        size = human_size(payload)
        if row.get("estimate_stale"):
            size += "*"
    return size


def format_hints_cell(row: dict) -> str:
    hints = row.get("hints")
    if hints is None:
        hints = entry_hints(row)
    return ", ".join(hints) if hints else "—"


def family_spellings(rows: list[dict]) -> dict[str, str]:
    """One spelling per family across a table: the majority's, as the tree uses."""
    return {
        fam["key"]: fam["family"]
        for fam in group_by_family(rows)
        if fam["key"] is not None and fam["family"]
    }


def format_family_cell(row: dict, spelling: dict[str, str] | None = None) -> str:
    lin = row.get("lineage")
    if not isinstance(lin, dict):
        lin = lineage_of_source(row.get("source") or "").as_dict()
    family = lin.get("family")
    if family and spelling:
        family = spelling.get(family.casefold(), family)
    return display_generation(family, lin.get("generation"))


def format_precision_cell(row: dict) -> str:
    label = row.get("precision")
    if not label:
        est = row.get("estimate") if isinstance(row.get("estimate"), dict) else {}
        label = est.get("precision")
    return str(label) if label else "—"


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
        print("  ".join(str(v).ljust(w) for v, w in zip(row, widths, strict=True)))


def print_catalog_table(rows: list[dict], *, header_line: str | None = None) -> None:
    if header_line:
        print()
        print(header_line)
        print()
    spelling = family_spellings(rows)
    specs = (
        ("STATUS", lambda r: r.get("status") or "—"),
        ("DESIRE", format_desire_cell),
        ("SOURCE", format_source_cell),
        ("TYPE", format_type_cell),
        ("FAMILY", lambda r: format_family_cell(r, spelling)),
        ("HAVE", format_have_cell),
        ("SIZE", format_size_cell),
        ("PRECISION", format_precision_cell),
        ("HINTS", format_hints_cell),
        ("NOTE", format_note_cell),
    )
    raw = [[fmt(r) for _, fmt in specs] for r in rows]
    keep = []
    for i, (name, _) in enumerate(specs):
        if name in _HIDE_IF_EMPTY and (
            not raw or all(row[i] in ("—", "") for row in raw)
        ):
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
        cells.append(
            (
                cat["id"],
                cat.get("title") or cat["id"],
                cat.get("curator") or "—",
                str(len(cat.get("entries") or [])),
                updated,
            )
        )
    _print_table(header, cells)


def realize_from_overlay(
    row: dict, entry: dict | None = None
) -> tuple[str, str | None, list[str] | None]:
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


def render_catalog_readme(catalog_dir: Path, catalog: dict) -> str:
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
        "| Desire | Source | Family | Type | Size (cached) | Precision | Hints | License | Note |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    spelling = family_spellings(catalog.get("entries") or [])
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
        lin = lineage_of_source(src)
        family = display_generation(
            spelling.get(lin.family_key or "", lin.family) if lin.family else None,
            lin.generation,
        )
        precision = est.get("precision") or "—"
        if is_home(src):
            href = f"[{src}]({src})"
            artifact = "closed"
            size = "closed"
        elif est.get("payload_bytes") is None:
            size = "?"
        else:
            as_of = (est.get("as_of") or "")[:10]
            size = f"{human_size(est['payload_bytes'])}" + (
                f" (as of {as_of})" if as_of else ""
            )
        license_s = est.get("license") or "?"
        hints = ", ".join(entry_hints(entry)) or "—"
        note = entry.get("note") or ""
        lines.append(
            f"| {desire_s} | {href} | {family} | {artifact} | {size} | {precision} | {hints} | {license_s} | {note} |"
        )
    lines += _families_section(catalog.get("entries") or [])
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
    return "\n".join(lines)


def _families_section(entries: list[dict]) -> list[str]:
    """The lineage view of a catalog: families → generations → members."""
    tree = [f for f in group_by_family(entries) if f["key"] is not None]
    if not tree:
        return []
    lines = [
        "",
        "## Families",
        "",
        "Read from each work's name; generations oldest first.",
        "",
    ]
    for fam in tree:
        home = (
            f" · published by {fam['home_publisher']}"
            if fam.get("home_publisher")
            else ""
        )
        lines.append(
            f"### {fam['family']} — {fam['count']} work{'s' if fam['count'] != 1 else ''}{home}"
        )
        lines.append("")
        for gen in fam["generations"]:
            members = []
            for entry in gen["rows"]:
                src = entry["source"]
                lin = lineage_of_source(src)
                est = (
                    entry.get("estimate")
                    if isinstance(entry.get("estimate"), dict)
                    else {}
                )
                label = lin.member or "flagship"
                extras = list(lin.variants) + [f"{f} print" for f in lin.formats]
                if is_home(src):
                    extras.append("closed")
                else:
                    if est.get("payload_bytes") is not None:
                        extras.append(human_size(est["payload_bytes"]))
                    if est.get("precision"):
                        extras.append(str(est["precision"]))
                publisher = _publisher_of_source(src)
                if (
                    publisher
                    and fam.get("home_publisher")
                    and publisher != fam["home_publisher"]
                ):
                    extras.append(f"by {publisher}")
                members.append(
                    f"`{label}`" + (f" ({', '.join(extras)})" if extras else "")
                )
            gen_label = gen["generation"] or "no generation in the name"
            lines.append(f"- **{gen_label}** — " + " · ".join(members))
        lines.append("")
    return lines


def _publisher_of_source(source: str) -> str | None:
    parsed = try_parse_source(source)
    return parsed.publisher if parsed is not None else None


def write_catalog_readme(
    catalog_dir: Path, catalog: dict, *, dry_run: bool = False
) -> tuple[int, int]:
    """Rewrite the catalog README.md; returns (added, removed) lines versus disk.

    ``dry_run`` reports the delta and leaves the file alone.
    """
    from .readme_gen import changed_lines

    path = catalog_dir / "README.md"
    text = render_catalog_readme(catalog_dir, catalog)
    old = path.read_text(encoding="utf-8") if path.is_file() else None
    delta = changed_lines(old, text)
    if not dry_run:
        path.write_text(text, encoding="utf-8")
    return delta

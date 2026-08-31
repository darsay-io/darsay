"""Master/print classification of one repo's weight files.

Mechanizes the QUANTIZATION.md line the way docs/proposals/classify.md
ratified it: verdicts come from a closed enum — ``master``, ``print``,
``support``, ``unknown`` — where ``unknown`` always means *fetch*. Every
undecidable, unreadable, capped, or gated case degrades toward keeping
bytes, never toward skipping them; only a confident print is skipped.

``collect_facts`` is the only networked door: it gathers what the rules
need through a provider's bounded ``read_bytes`` (``config.json``,
``*.index.json``, GGUF KV tables, safetensors headers of files no index
accounts for) and records every cap it applied. Everything downstream —
``build_sets``, ``evaluate``, ``attach_selection`` — is a pure function
over dicts, testable exactly like ``subset.py``. The selection feeds the
existing subset machinery and is verified against the full inventory
before it can bind; exact paths when no clean glob survives.
"""

from __future__ import annotations

import json
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import PurePosixPath

from .gguf_meta import DEFAULT_FETCH_CAP, GGUFError, read_kv
from .providers.base import SourceError
from .safetensors_meta import read_header_via
from .subset import matches_include

WEIGHT_SUFFIXES = (".safetensors", ".bin", ".gguf", ".pt", ".pth")
FULL_FIDELITY_DTYPES = frozenset({"F64", "F32", "F16", "BF16"})
IMATRIX_PREFIX = "quantize.imatrix."
# Provenance the converter records about its own input, when it does.
SOURCE_CLAIM_KEYS = (
    "general.source.url",
    "general.source.huggingface.repository",
)
# Read caps, recorded in the receipt like query_limit.
HEADER_FILE_CAP = 64
JSON_CAP_BYTES = 10 * 1024 * 1024
# Header files read concurrently; each is an independent bounded read.
HEADER_READ_WORKERS = 8

_TORCH_TO_ST = {
    "float64": "F64",
    "float32": "F32",
    "float16": "F16",
    "bfloat16": "BF16",
}
_SHARD_RE = re.compile(r"^(?P<prefix>.+-)\d+(?P<suffix>-of-\d+\.\w+)$")


def _dir_of(path: str) -> str:
    return str(PurePosixPath(path).parent)


def _name_of(path: str) -> str:
    return PurePosixPath(path).name


def _join(directory: str, name: str) -> str:
    return name if directory == "." else f"{directory}/{name}"


def _is_weight(path: str) -> bool:
    return path.lower().endswith(WEIGHT_SUFFIXES)


def _is_index(path: str) -> bool:
    return _name_of(path).lower().endswith(".index.json")


def _suffix_kind(path: str) -> str:
    lowered = path.lower()
    if lowered.endswith(".safetensors"):
        return "safetensors"
    if lowered.endswith(".gguf"):
        return "gguf"
    return "legacy"


def _size_of(item: dict) -> int:
    return item.get("size") or 0


# ---------------------------------------------------------------- set model


def _new_set(kind: str, directory: str, items: list[dict], **extra) -> dict:
    paths = sorted(item["path"] for item in items)
    return {
        "kind": kind,
        "dir": directory,
        "paths": paths,
        "file_count": len(paths),
        "bytes": sum(_size_of(item) for item in items),
        "unknown_size_count": sum(1 for item in items if item.get("size") is None),
        **extra,
    }


def build_sets(files: list[dict], indexes: dict) -> list[dict]:
    """Group an inventory into the sets the rules judge.

    ``indexes`` maps ``*.index.json`` paths to their parsed dict or an
    ``{"__error__": reason}`` marker. Weight-map membership is scoped to
    the index's own directory; an unreadable index taints only that
    directory's unclaimed safetensors (they cannot prove membership).
    """
    weights = [f for f in files if _is_weight(f["path"])]
    support = [f for f in files if not _is_weight(f["path"])]
    by_dir: dict[str, list[dict]] = {}
    for item in weights:
        by_dir.setdefault(_dir_of(item["path"]), []).append(item)

    sets: list[dict] = []
    claimed: set[str] = set()
    st_index_dirs: set[str] = set()
    error_dirs: dict[str, str] = {}
    for index_path in sorted(indexes):
        parsed = indexes[index_path]
        directory = _dir_of(index_path)
        if not isinstance(parsed, dict) or "__error__" in parsed:
            reason = (parsed or {}).get("__error__", "unreadable")
            error_dirs[directory] = f"{_name_of(index_path)}: {reason}"
            continue
        weight_map = parsed.get("weight_map")
        if not isinstance(weight_map, dict):
            error_dirs[directory] = f"{_name_of(index_path)}: no weight_map"
            continue
        names = {str(v) for v in weight_map.values()}
        member_paths = {_join(directory, name) for name in names}
        members = [
            item
            for item in by_dir.get(directory, [])
            if item["path"] in member_paths and item["path"] not in claimed
        ]
        if any(name.lower().endswith(".safetensors") for name in names):
            st_index_dirs.add(directory)
        if members:
            claimed.update(item["path"] for item in members)
            fmt = _suffix_kind(members[0]["path"])
            sets.append(
                _new_set("indexed", directory, members, index=index_path, format=fmt)
            )

    for directory in sorted(by_dir):
        items = by_dir[directory]
        st_rest = [
            item
            for item in items
            if _suffix_kind(item["path"]) == "safetensors"
            and item["path"] not in claimed
        ]
        if st_rest:
            if directory in error_dirs:
                sets.append(
                    _new_set(
                        "index_error",
                        directory,
                        st_rest,
                        format="safetensors",
                        error=error_dirs[directory],
                    )
                )
            elif directory in st_index_dirs:
                sets.append(
                    _new_set("orphan", directory, st_rest, format="safetensors")
                )
            else:
                sets.append(
                    _new_set("standalone", directory, st_rest, format="safetensors")
                )
        for item in items:
            if _suffix_kind(item["path"]) == "gguf":
                sets.append(_new_set("gguf", directory, [item], format="gguf"))
        legacy = [
            item
            for item in items
            if _suffix_kind(item["path"]) == "legacy" and item["path"] not in claimed
        ]
        if legacy:
            sets.append(_new_set("legacy", directory, legacy, format="legacy"))

    if support:
        sets.append(_new_set("support", ".", support, format="support"))
    return sets


def set_glob(weight_set: dict) -> str | None:
    """A compact include pattern selecting exactly this set, when one exists."""
    paths = weight_set["paths"]
    if len(paths) == 1:
        return paths[0]
    matches = [_SHARD_RE.match(_name_of(p)) for p in paths]
    if (
        all(matches)
        and len({(m.group("prefix"), m.group("suffix")) for m in matches}) == 1
    ):
        pattern = matches[0].group("prefix") + "*" + matches[0].group("suffix")
        return _join(weight_set["dir"], pattern)
    return None


# ------------------------------------------------------------------- rules


def _config_for(weight_set: dict, configs: dict):
    return configs.get(weight_set["dir"])


def _quant_method(cfg) -> str | None:
    if not isinstance(cfg, dict) or "__error__" in cfg:
        return None
    quant = cfg.get("quantization_config")
    if isinstance(quant, dict):
        return str(quant.get("quant_method") or "unspecified")
    if isinstance(cfg.get("quantization"), dict):
        return "mlx"
    return None


def _set_dtype(weight_set: dict, configs: dict, st_headers: dict):
    """(dominant safetensors dtype, where it was established) or (None, None)."""
    counts: dict[str, int] = {}
    for path in weight_set["paths"]:
        summary = st_headers.get(path)
        if isinstance(summary, dict) and "__error__" not in summary:
            dtype = summary.get("dtype")
            if dtype:
                counts[dtype] = counts.get(dtype, 0) + (summary.get("parameters") or 1)
    if counts:
        return max(counts, key=counts.get), "headers"
    cfg = _config_for(weight_set, configs)
    if isinstance(cfg, dict) and "__error__" not in cfg:
        torch_dtype = cfg.get("torch_dtype")
        if isinstance(torch_dtype, str):
            return _TORCH_TO_ST.get(torch_dtype.lower(), torch_dtype.upper()), "config"
    return None, None


def _base_identical(weight_set: dict, base_map: dict) -> bool:
    paths = weight_set.get("_sha256") or {}
    return bool(weight_set["paths"]) and all(
        paths.get(p) and paths[p] in base_map for p in weight_set["paths"]
    )


def _judge_safetensors(weight_set: dict, facts: dict) -> tuple[str, str, str]:
    base = facts.get("base") or {}
    if _base_identical(weight_set, base.get("sha256") or {}):
        return (
            "print",
            "R2",
            f"byte-identical (LFS SHA-256) to files in {base.get('locator')}",
        )
    if weight_set["kind"] == "index_error":
        return (
            "unknown",
            "R14",
            f"cannot establish index membership — {weight_set['error']}",
        )
    cfg = _config_for(weight_set, facts.get("configs") or {})
    if isinstance(cfg, dict) and "__error__" in cfg:
        return "unknown", "R14", f"config.json unreadable — {cfg['__error__']}"
    method = _quant_method(cfg)
    dtype, established = _set_dtype(
        weight_set, facts.get("configs") or {}, facts.get("st_headers") or {}
    )
    if method or (dtype and dtype not in FULL_FIDELITY_DTYPES):
        return (
            "master",
            "R5",
            f"quantized safetensors ({method or dtype}) — calibration or a "
            "curated recipe may be baked in",
        )
    if dtype is None:
        return "unknown", "R14", "weight dtype not establishable"
    if weight_set["kind"] == "orphan":
        return (
            "unknown",
            "R6",
            f"full-fidelity {dtype} in no index — not loadable as shipped, "
            "possibly a distinct build",
        )
    if weight_set["kind"] == "indexed":
        return (
            "master",
            "R3",
            f"selected by {_name_of(weight_set['index'])}; {dtype} "
            f"(dtype from {established})",
        )
    return "master", "R4", f"standalone full-fidelity {dtype}"


def _judge_legacy(weight_set: dict, facts: dict, has_safetensors: bool):
    base = facts.get("base") or {}
    if _base_identical(weight_set, base.get("sha256") or {}):
        return (
            "print",
            "R2",
            f"byte-identical (LFS SHA-256) to files in {base.get('locator')}",
        )
    if has_safetensors:
        return (
            "unknown",
            "R12",
            "legacy-format weights beside safetensors — equivalence is not "
            "cheaply verifiable",
        )
    return "master", "R13", "only weight format in this repo"


def _judge_gguf(
    weight_set: dict, facts: dict, established: int, uncertain: int, locator: str
):
    path = weight_set["paths"][0]
    info = (facts.get("gguf") or {}).get(path)
    if not isinstance(info, dict) or "__error__" in info:
        reason = (info or {}).get("__error__", "header not read")
        return "unknown", "R14", f"GGUF header unreadable — {reason}"
    kv = info.get("kv") or {}
    if any(key.startswith(IMATRIX_PREFIX) for key in kv):
        return "master", "R7", "importance matrix baked in (quantize.imatrix.*)"
    claims = [kv[k] for k in SOURCE_CLAIM_KEYS if isinstance(kv.get(k), str)]
    if claims and not any(locator.lower() in claim.lower() for claim in claims):
        return (
            "unknown",
            "R8",
            f"header records an external source ({claims[0]!r})",
        )
    if uncertain:
        # A weight set of unestablished nature could itself be the source
        # (or a second build); a print claim cannot anchor on a maybe.
        return (
            "unknown",
            "R11",
            "no imatrix, but a weight set in this repo could not be "
            "established — the source build is not establishable",
        )
    if established == 0:
        return (
            "master",
            "R10",
            "no full-fidelity source in this repo — the only surviving form here",
        )
    if established == 1:
        return (
            "print",
            "R9",
            "no imatrix — mechanical quant of the sole full-fidelity set; "
            "regenerable under a recorded toolchain, not bit-identical",
        )
    return (
        "unknown",
        "R11",
        f"no imatrix, but {established} candidate source sets — the source "
        "build is not establishable",
    )


def _gguf_evidence(info) -> dict:
    kv = (info or {}).get("kv") or {}
    return {
        "file_type": kv.get("general.file_type"),
        "quantization_version": kv.get("general.quantization_version"),
        "general_name": kv.get("general.name"),
        "source_claims": [
            kv[k] for k in SOURCE_CLAIM_KEYS if isinstance(kv.get(k), str)
        ],
        "imatrix": any(key.startswith(IMATRIX_PREFIX) for key in kv),
    }


# Rules whose sets count as established (or presumed, for legacy) GGUF
# conversion sources. Not R5: a calibrated quant is not a conversion
# source. R14 sets are counted separately as *uncertain* — an
# unestablished set widens ambiguity toward fetching, and can never
# anchor an R9 print.
_CANDIDATE_RULES = frozenset({"R2", "R3", "R4", "R6", "R12", "R13"})


def evaluate(files: list[dict], facts: dict, *, locator: str) -> dict:
    """Pure rule evaluation: inventory + facts -> sets with verdicts."""
    sha_by_path = {f["path"]: f.get("sha256") for f in files}
    sets = build_sets(files, facts.get("indexes") or {})
    for weight_set in sets:
        weight_set["_sha256"] = {p: sha_by_path.get(p) for p in weight_set["paths"]}

    st_kinds = ("indexed", "orphan", "standalone", "index_error")
    st_sets = [
        s for s in sets if s["kind"] in st_kinds and s.get("format") == "safetensors"
    ]
    legacy_sets = [s for s in sets if s.get("format") == "legacy"]
    for weight_set in st_sets:
        verdict, rule, reason = _judge_safetensors(weight_set, facts)
        weight_set.update(verdict=verdict, rule=rule, reason=reason)
    for weight_set in legacy_sets:
        verdict, rule, reason = _judge_legacy(weight_set, facts, bool(st_sets))
        weight_set.update(verdict=verdict, rule=rule, reason=reason)

    # R15: a weight set byte-identical (file-for-file LFS SHA-256) to
    # another kept set in this repo is the strongest print there is —
    # the twin ships in the same bundle, so the skip is bit-recoverable
    # from inside the archive, no vault gate, no upstream dependency.
    # Runs before candidate counting: identical copies are one source.
    groups: dict[tuple, list[dict]] = {}
    for weight_set in st_sets + legacy_sets:
        shas = [weight_set["_sha256"].get(p) for p in weight_set["paths"]]
        if not shas or any(sha is None for sha in shas):
            continue  # incomplete hashes: never a claim
        groups.setdefault(tuple(sorted(shas)), []).append(weight_set)
    for twins in groups.values():
        keepable = [t for t in twins if t.get("verdict") in ("master", "unknown")]
        if len(twins) < 2 or not keepable:
            continue
        keeper = min(
            keepable,
            key=lambda t: (
                -1 if t["dir"] == "." else t["dir"].count("/"),
                t["dir"],
                t["paths"][0],
            ),
        )
        keeper_name = keeper["dir"] if keeper["dir"] != "." else keeper["paths"][0]
        for twin in twins:
            if twin is keeper:
                continue
            twin.update(
                verdict="print",
                rule="R15",
                reason=(
                    f"byte-identical (LFS SHA-256) to {keeper_name} — "
                    "bit-recoverable from the kept twin in this bundle"
                ),
            )
            twin["evidence"] = {
                "duplicate_of": keeper["paths"][0],
                "exact": True,
            }

    established = sum(
        1 for s in st_sets + legacy_sets if s.get("rule") in _CANDIDATE_RULES
    )
    uncertain = sum(1 for s in st_sets + legacy_sets if s.get("rule") == "R14")
    for weight_set in sets:
        if weight_set["kind"] == "gguf":
            verdict, rule, reason = _judge_gguf(
                weight_set, facts, established, uncertain, locator
            )
            weight_set.update(verdict=verdict, rule=rule, reason=reason)
            weight_set["evidence"] = _gguf_evidence(
                (facts.get("gguf") or {}).get(weight_set["paths"][0])
            )
        elif weight_set["kind"] == "support":
            weight_set.update(verdict="support", rule="R1", reason="kept always")
        elif "verdict" not in weight_set:
            weight_set.update(
                verdict="unknown",
                rule="R14",
                reason="unrecognized set shape — kept",
            )

    base_in_vault = bool((facts.get("base") or {}).get("in_vault"))
    verdict_bytes = {"master": 0, "print": 0, "support": 0, "unknown": 0}
    keep = {"files": 0, "bytes": 0}
    skip = {"files": 0, "bytes": 0}
    for weight_set in sets:
        weight_set.pop("_sha256", None)
        weight_set["glob"] = (
            None if weight_set["kind"] == "support" else set_glob(weight_set)
        )
        weight_set["name"] = (
            "support files"
            if weight_set["kind"] == "support"
            else weight_set["glob"]
            or f"{weight_set['file_count']} files in {weight_set['dir']}"
        )
        skippable = weight_set["rule"] in ("R9", "R15") or (
            weight_set["rule"] == "R2" and base_in_vault
        )
        weight_set["action"] = "skip" if skippable else "fetch"
        verdict_bytes[weight_set["verdict"]] += weight_set["bytes"]
        bucket = skip if weight_set["action"] == "skip" else keep
        bucket["files"] += weight_set["file_count"]
        bucket["bytes"] += weight_set["bytes"]

    return {
        "policy": "masters",
        "sets": sets,
        "candidates": established,
        "uncertain_sources": uncertain,
        "verdict_bytes": verdict_bytes,
        "keep": keep,
        "skip": skip,
        "unclassified_count": sum(1 for s in sets if s["verdict"] == "unknown"),
        "notes": [],
    }


def attach_selection(classification: dict, files: list[dict]) -> None:
    """Turn verdicts into a verified ``--include`` selection, or refuse.

    The selection keeps every master, unknown, and support file and drops
    skipped prints only. Globs must reproduce the intended weight set
    exactly against the full inventory (via the same matcher
    ``select_subset`` uses); exact paths are the fallback, and when even
    those cannot be verified the selection is ``None`` — the caller
    fetches the full repo and says why.
    """
    sets = classification["sets"]
    skipped = [s for s in sets if s["action"] == "skip"]
    if not skipped:
        classification["selection"] = None
        return
    kept_weight_paths: set[str] = set()
    kept_sets = [s for s in sets if s["action"] == "fetch" and s["kind"] != "support"]
    for weight_set in kept_sets:
        kept_weight_paths.update(weight_set["paths"])
    if not kept_weight_paths:
        classification["selection"] = None
        classification["notes"].append(
            "every weight file classified as a skippable print — the policy "
            "keeps the full repo rather than archive an empty payload; "
            "review with darsay classify"
        )
        return

    def verified(patterns: list[str]) -> bool:
        matched = {
            f["path"]
            for f in files
            if _is_weight(f["path"]) and matches_include(f["path"], patterns)
        }
        return matched == kept_weight_paths

    globs = [s["glob"] for s in kept_sets]
    if all(globs):
        patterns = sorted(set(globs))
        if verified(patterns):
            classification["selection"] = {
                "include": patterns,
                "explicit_paths": False,
            }
            return
        # Root-anchored globs disable filename matching: how a kept root
        # file is told apart from a skipped twin with the same basename.
        anchored = sorted({f"/{g}" for g in globs})
        if verified(anchored):
            classification["selection"] = {
                "include": anchored,
                "explicit_paths": False,
            }
            return
    paths = sorted(f"/{p}" for p in kept_weight_paths)
    if verified(paths):
        classification["selection"] = {"include": paths, "explicit_paths": True}
        return
    classification["selection"] = None
    classification["notes"].append(
        "selection could not be verified against the inventory — the full "
        "repo is fetched instead"
    )


# ------------------------------------------------------------ fact gathering


def _summarize_st_header(header: dict) -> dict:
    counts: dict[str, int] = {}
    tensors = 0
    for name, entry in header.items():
        if name == "__metadata__":
            continue
        tensors += 1
        n = 1
        for dim in entry.get("shape", []):
            n *= dim
        dtype = entry.get("dtype") or "unknown"
        counts[dtype] = counts.get(dtype, 0) + n
    dominant = max(counts, key=counts.get) if counts else None
    return {
        "dtype": dominant,
        "tensor_count": tensors,
        "parameters": sum(counts.values()),
    }


def collect_facts(
    provider,
    ref,
    revision: str,
    files: list[dict],
    *,
    base_locator: str | None = None,
    base_in_vault: bool = False,
    gguf_fetch_cap: int = DEFAULT_FETCH_CAP,
    header_file_cap: int = HEADER_FILE_CAP,
    json_cap: int = JSON_CAP_BYTES,
    on_read=None,
) -> tuple[dict, dict]:
    """Fetch the facts the rules need, through bounded range reads.

    Returns ``(facts, receipt)``. Every failure lands in the facts as an
    ``{"__error__": reason}`` marker — the rules turn those into
    ``unknown`` (= fetch); nothing raises past this function except a
    programming error. Header files are read concurrently
    (``HEADER_READ_WORKERS``); ``on_read(path, length)`` fires as each
    range request begins, so a caller can show liveness.
    """
    state = {"requests": 0, "bytes_fetched": 0}
    state_lock = threading.Lock()

    def read(path: str, start: int, length: int) -> bytes:
        if on_read is not None:
            on_read(path, length)
        with state_lock:
            state["requests"] += 1
        data = provider.read_bytes(ref, revision, path, start, length)
        with state_lock:
            state["bytes_fetched"] += len(data)
        return data

    by_path = {f["path"]: f for f in files}
    weight_paths = [p for p in by_path if _is_weight(p)]
    weight_dirs = {_dir_of(p) for p in weight_paths}
    facts: dict = {
        "configs": {},
        "indexes": {},
        "gguf": {},
        "st_headers": {},
        "base": {
            "locator": base_locator,
            "sha256": {},
            "in_vault": bool(base_in_vault),
            "error": None,
        },
    }

    def read_json(path: str):
        size = by_path[path].get("size")
        if size is not None and size > json_cap:
            return {"__error__": f"{path} is larger than the {json_cap}-byte JSON cap"}
        try:
            data = read(path, 0, size if size is not None else json_cap)
            if size is None and len(data) >= json_cap:
                return {"__error__": f"{path} exceeds the {json_cap}-byte JSON cap"}
            parsed = json.loads(data)
        except (SourceError, ValueError) as exc:
            return {"__error__": str(exc)}
        if not isinstance(parsed, dict):
            return {"__error__": f"{path} is not a JSON object"}
        return parsed

    for directory in sorted(weight_dirs):
        config_path = _join(directory, "config.json")
        facts["configs"][directory] = (
            read_json(config_path) if config_path in by_path else None
        )
    for path in sorted(by_path):
        if _is_index(path) and _dir_of(path) in weight_dirs:
            facts["indexes"][path] = read_json(path)

    def _pool_map(worker, paths, into: dict) -> None:
        if not paths:
            return
        with ThreadPoolExecutor(
            max_workers=min(HEADER_READ_WORKERS, len(paths))
        ) as pool:
            for path, info in pool.map(worker, paths):
                into[path] = info

    gguf_paths = [p for p in sorted(weight_paths) if p.lower().endswith(".gguf")]
    for path in gguf_paths[header_file_cap:]:
        facts["gguf"][path] = {
            "__error__": f"header file cap ({header_file_cap}) reached"
        }
    gguf_allowed = gguf_paths[:header_file_cap]
    header_files_read = len(gguf_allowed)

    def _read_gguf(path: str):
        try:
            out = read_kv(
                lambda start, end, p=path: read(p, start, end - start),
                fetch_cap=gguf_fetch_cap,
            )
            return path, {
                "kv": out["kv"],
                "tensor_count": out["tensor_count"],
                "bytes_fetched": out["bytes_fetched"],
            }
        except (GGUFError, SourceError) as exc:
            return path, {"__error__": str(exc)}

    _pool_map(_read_gguf, gguf_allowed, facts["gguf"])

    # Membership decides which safetensors headers are worth reading:
    # every file no readable safetensors index accounts for, plus one
    # member per set whose directory config cannot establish a dtype.
    sets = build_sets(list(by_path.values()), facts["indexes"])
    header_targets: list[str] = []
    for weight_set in sets:
        if weight_set.get("format") != "safetensors":
            continue
        if weight_set["kind"] in ("orphan", "index_error"):
            header_targets.extend(weight_set["paths"])
            continue
        cfg = facts["configs"].get(weight_set["dir"])
        has_dtype = (
            isinstance(cfg, dict)
            and "__error__" not in cfg
            and isinstance(cfg.get("torch_dtype"), str)
        )
        if not has_dtype:
            header_targets.append(weight_set["paths"][0])
    st_targets = list(dict.fromkeys(header_targets))
    remaining_cap = max(0, header_file_cap - header_files_read)
    for path in st_targets[remaining_cap:]:
        facts["st_headers"][path] = {
            "__error__": f"header file cap ({header_file_cap}) reached"
        }
    st_allowed = st_targets[:remaining_cap]
    header_files_read += len(st_allowed)

    def _read_st(path: str):
        try:
            header = read_header_via(
                lambda start, end, p=path: read(p, start, end - start),
                name=_name_of(path),
                size=by_path[path].get("size"),
            )
            return path, _summarize_st_header(header)
        except (SourceError, ValueError) as exc:
            return path, {"__error__": str(exc)}

    _pool_map(_read_st, st_allowed, facts["st_headers"])

    base_pinned = False
    if base_locator:
        try:
            base_ref = provider.parse(base_locator)
            snapshot = provider.pin(base_ref, None)
            facts["base"]["sha256"] = {
                f.sha256: f.path for f in snapshot.files if f.sha256
            }
            base_pinned = True
        except (SourceError, SystemExit) as exc:
            facts["base"]["error"] = str(exc)

    receipt = {
        "requests": state["requests"],
        "bytes_fetched": state["bytes_fetched"],
        "header_files_read": header_files_read,
        "base_pinned": base_pinned,
        "caps": {
            "gguf_fetch_cap_bytes": gguf_fetch_cap,
            "header_file_cap": header_file_cap,
            "json_cap_bytes": json_cap,
        },
    }
    return facts, receipt


def _display_rows(result: dict) -> list[tuple[str, str, str, str, str]]:
    """Table rows; per-file GGUF sets sharing a verdict merge for display."""
    from .readme_gen import human_size

    weight_sets = [s for s in result["sets"] if s["kind"] != "support"]
    support_sets = [s for s in result["sets"] if s["kind"] == "support"]
    ggufs = [s for s in weight_sets if s["kind"] == "gguf"]
    others = [s for s in weight_sets if s["kind"] != "gguf"]
    merged: list[dict] = []
    groups: dict[tuple, dict] = {}
    for weight_set in ggufs:
        key = (weight_set["verdict"], weight_set["rule"], weight_set["action"])
        group = groups.get(key)
        if group is None:
            group = groups[key] = {**weight_set, "paths": list(weight_set["paths"])}
            merged.append(group)
        else:
            group["paths"].extend(weight_set["paths"])
            group["file_count"] += weight_set["file_count"]
            group["bytes"] += weight_set["bytes"]
            group["unknown_size_count"] += weight_set["unknown_size_count"]
    for group in merged:
        if group["file_count"] > 1:
            group["name"] = f"*.gguf ({group['file_count']} files)"
    display = sorted(others + merged, key=lambda s: -s["bytes"]) + support_sets

    rows = []
    for weight_set in display:
        name = weight_set["name"]
        if len(name) > 44:
            name = name[:43] + "…"
        size = human_size(weight_set["bytes"])
        if weight_set["unknown_size_count"]:
            size += "+?"
        why = f"{weight_set['reason']} [{weight_set['rule']}]"
        if weight_set["action"] == "skip":
            why = "SKIPPED — " + why
        rows.append(
            (weight_set["verdict"], name, str(weight_set["file_count"]), size, why)
        )
    return rows


def print_classification(result: dict, progress=print) -> None:
    """Human view: verdict table, legend, read receipt, archive footer."""
    from .catalog import format_archive_command
    from .readme_gen import human_size

    p = progress
    src = result.get("source") or {}
    if src:
        p(f"\n{src['address']} @ {src['revision_ref']} -> {src['revision'][:12]}")
    weight_bytes = sum(s["bytes"] for s in result["sets"] if s["kind"] != "support")
    weight_files = sum(
        s["file_count"] for s in result["sets"] if s["kind"] != "support"
    )
    p(f"  weights {human_size(weight_bytes)} in {weight_files} files\n")

    rows = _display_rows(result)
    header = ("VERDICT", "SET", "FILES", "SIZE")
    widths = [max(len(str(row[i])) for row in [header, *rows]) for i in range(4)]
    p("  " + "  ".join(header[i].ljust(widths[i]) for i in range(4)) + "  WHY")
    for row in rows:
        p(
            "  "
            + "  ".join(str(row[i]).ljust(widths[i]) for i in range(4))
            + "  "
            + row[4]
        )

    p(
        "\n  print = regenerable from kept files: a mechanical "
        "re-quantization (not bit-identical) or a byte-identical twin "
        "kept in this bundle [R15]."
    )
    unknown_sets = [s for s in result["sets"] if s["verdict"] == "unknown"]
    if unknown_sets:
        unknown_bytes = sum(s["bytes"] for s in unknown_sets)
        p(
            f"  unknown = darsay will not guess; those files are fetched. "
            f"{len(unknown_sets)} set{'s' if len(unknown_sets) != 1 else ''} "
            f"({human_size(unknown_bytes)}) need your decision."
        )
    receipt = result.get("read") or {}
    caps = receipt.get("caps") or {}
    if receipt:
        p(
            f"  read: {receipt['requests']} range requests, "
            f"{human_size(receipt['bytes_fetched'])} fetched "
            f"(caps: {human_size(caps.get('gguf_fetch_cap_bytes', 0))}/GGUF "
            f"header, {caps.get('header_file_cap')} header reads) — "
            "nothing written."
        )
    for note in result.get("notes") or []:
        p(f"  note: {note}")

    keep, skip = result["keep"], result["skip"]
    selection = result.get("selection")
    if selection and skip["bytes"]:
        total = keep["bytes"] + skip["bytes"]
        p(
            f"\nmasters-first: fetch {human_size(keep['bytes'])} of "
            f"{human_size(total)} — skip {human_size(skip['bytes'])} of "
            "prints. darsay archive applies this by default; --full "
            "fetches everything."
        )
        if src.get("address"):
            revision = (
                src.get("revision_ref")
                if src.get("revision_ref") not in (None, "main")
                else None
            )
            p(
                "To pin exactly this selection: "
                + format_archive_command(src["address"], revision, selection["include"])
            )
    else:
        p(
            "\nNothing here is mechanically skippable — the masters policy "
            "fetches the full repo."
        )
    p("")


def base_bundle_in_vault(vault, provider, base_locator: str) -> bool:
    """A registered full-repo bundle of the base exists in this vault.

    The R2 skip gate: byte-identical prints are skipped only when the
    identical bytes are verified locally. A subset bundle of the base is
    not enough — it may not hold the matched files.
    """
    from .vault import bundle_records

    try:
        canonical = provider.parse(base_locator).canonical
    except SystemExit:
        return False
    for row in bundle_records(vault):
        if row.get("partial") or row.get("include"):
            continue
        if row.get("source_address") == canonical:
            return True
    return False


def _print_policy_preflight(result: dict, ref, progress) -> None:
    from .readme_gen import human_size

    skip, keep = result["skip"], result["keep"]
    unknown_sets = [s for s in result["sets"] if s["verdict"] == "unknown"]
    if not skip["bytes"] and not unknown_sets and not result["notes"]:
        return
    total = skip["bytes"] + keep["bytes"]
    line = f"masters-first: fetching {human_size(keep['bytes'])} of {human_size(total)}"
    if skip["bytes"]:
        line += f" — skipping {human_size(skip['bytes'])} of derivable prints"
    progress(line)
    for weight_set in result["sets"]:
        if weight_set["action"] == "skip":
            progress(
                f"  skipping  {weight_set['name']}  "
                f"{human_size(weight_set['bytes'])}  "
                f"{weight_set['reason']} [{weight_set['rule']}]"
            )
    if unknown_sets:
        unknown_bytes = sum(s["bytes"] for s in unknown_sets)
        progress(
            f"  fetching {len(unknown_sets)} unclassified "
            f"set{'s' if len(unknown_sets) != 1 else ''} "
            f"({human_size(unknown_bytes)}) — darsay will not guess"
        )
    for note in result["notes"]:
        progress(f"  note: {note}")
    progress(
        f"  --full fetches everything; darsay classify {ref.canonical} "
        "shows the evidence"
    )


def masters_policy(
    provider, ref, snapshot, vault, progress=print, on_read=None
) -> tuple[list[str] | None, dict | None]:
    """The archive default: classify a fresh model pin, derive its selection.

    Returns ``(include patterns, policy record)`` — or ``(None, None)``
    when nothing is skippable, the selection could not be verified, or
    classification failed for any reason. The caller then fetches the
    full repo; failures never raise past here.
    """
    from .providers.huggingface import parse_base_model_tags

    try:
        files = [
            {
                "path": f.path,
                "size": f.size,
                "sha256": f.sha256,
                "git_sha1": f.git_sha1,
            }
            for f in snapshot.files
        ]
        tags = list((snapshot.metadata or {}).get("tags") or [])
        base_ids, _ = parse_base_model_tags(tags)
        base_locator = base_ids[0] if base_ids else None
        base_in_vault = (
            base_bundle_in_vault(vault, provider, base_locator)
            if base_locator
            else False
        )
        result = classify_source(
            provider,
            ref,
            snapshot.revision,
            files,
            base_locator=base_locator,
            base_in_vault=base_in_vault,
            on_read=on_read,
        )
    except Exception as exc:  # every failure degrades to the full repo
        progress(f"WARNING: classification failed ({exc}) — fetching the full repo")
        return None, None
    _print_policy_preflight(result, ref, progress)
    selection = result.get("selection")
    if not selection:
        return None, None
    from . import __version__

    policy = {
        "policy": "masters",
        "classification": {
            "classifier": {"darsay": __version__},
            "read": result.get("read"),
            "sets": [
                {
                    "name": s["name"],
                    "kind": s["kind"],
                    "verdict": s["verdict"],
                    "rule": s["rule"],
                    "action": s["action"],
                    "file_count": s["file_count"],
                    "bytes": s["bytes"],
                    "reason": s["reason"],
                }
                for s in result["sets"]
            ],
        },
    }
    return list(selection["include"]), policy


def classify_source(
    provider,
    ref,
    revision: str,
    files: list[dict],
    *,
    base_locator: str | None = None,
    base_in_vault: bool = False,
    on_read=None,
    **caps,
) -> dict:
    """Collect facts, evaluate, and attach the verified selection."""
    facts, receipt = collect_facts(
        provider,
        ref,
        revision,
        files,
        base_locator=base_locator,
        base_in_vault=base_in_vault,
        on_read=on_read,
        **caps,
    )
    classification = evaluate(files, facts, locator=ref.locator)
    attach_selection(classification, files)
    classification["read"] = receipt
    if facts["base"].get("error"):
        classification["notes"].append(
            f"base repo not consulted — {facts['base']['error']}"
        )
    return classification

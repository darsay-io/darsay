"""Estimate a bundle's full footprint before downloading anything.

`modelvault estimate` is a read-only preflight for `archive`: it asks the Hub
API for the pinned revision's file inventory and reports what the bundle would
cost — payload size, parameter count, engines, completeness, disk headroom —
without downloading a single payload byte or writing anything to disk.

File sizes and parameter counts come straight from upstream metadata and are
exact; anything derived (RAM needs, download scratch) is labeled an estimate.
Query caps on the variant listing are recorded in the result, mirroring the
manifest's `query_limit` convention.
"""

from __future__ import annotations

import shutil
from fnmatch import fnmatch
from pathlib import Path

from huggingface_hub import HfApi
from huggingface_hub.utils import HfHubHTTPError

from .archiver import bundle_dir_for, utc_now
from .hydrate import detect_engines
from .readme_gen import human_params, human_size
from .schema import check_completeness

WEIGHT_SUFFIXES = (".safetensors", ".bin", ".gguf", ".pt", ".pth")
RAM_FACTOR = 1.2  # same heuristic as metadata.estimate_runtime
VARIANT_QUERY_LIMIT = 100  # upstream listing cap, recorded in the result
VARIANT_DETAIL_LIMIT = 10  # variants whose exact size is fetched (one API call each)

_FORMAT_TAGS = ("gguf", "mlx", "awq", "gptq", "fp8", "4-bit", "8-bit", "exl2", "exl3", "onnx")
_FORMAT_NAME_HINTS = ("nvfp4", "fp8", "awq", "gptq", "int8", "int4", "mlx", "gguf", "exl3", "exl2")


def _license_from_tags(tags: list[str] | None) -> str | None:
    for tag in tags or []:
        if tag.startswith("license:"):
            return tag.split(":", 1)[1]
    return None


def _safetensors_params(info) -> dict | None:
    st = getattr(info, "safetensors", None)
    if st is None:
        return None
    by_dtype = dict(getattr(st, "parameters", None) or {})
    total = getattr(st, "total", None) or sum(by_dtype.values())
    if not total:
        return None
    dominant = max(by_dtype, key=by_dtype.get) if by_dtype else None
    return {"total": total, "by_dtype": by_dtype or None, "dominant_dtype": dominant}


def _variant_formats(model_id: str, tags: list[str] | None) -> list[str] | None:
    found = [t for t in _FORMAT_TAGS if t in (tags or [])]
    lowered = model_id.lower()
    for hint in _FORMAT_NAME_HINTS:
        if hint in lowered and hint not in found:
            found.append(hint)
    return found or None


def _disk_probe(vault: Path) -> tuple[Path, int]:
    """Free bytes at the vault target, walking up to the nearest existing dir."""
    probe = vault.resolve()
    while not probe.exists():
        probe = probe.parent
    return probe, shutil.disk_usage(probe).free


def _list_variants(api: HfApi, repo_id: str, progress) -> dict:
    listed = list(api.list_models(filter=f"base_model:quantized:{repo_id}",
                                  limit=VARIANT_QUERY_LIMIT))
    listed.sort(key=lambda m: -(m.downloads or 0))
    rows = [{
        "repo_id": m.id,
        "downloads": m.downloads,
        "formats": _variant_formats(m.id, m.tags),
        "total_size_bytes": None,
    } for m in listed]
    detailed = rows[:VARIANT_DETAIL_LIMIT]
    if detailed:
        progress(f"Sizing top {len(detailed)} of {len(rows)} quantized variants ...")
    for row in detailed:
        try:
            vi = api.model_info(row["repo_id"], files_metadata=True)
            row["total_size_bytes"] = sum(s.size or 0 for s in vi.siblings or [])
        except (HfHubHTTPError, OSError):
            pass  # size stays null — listed but not sized
    return {
        "as_of": utc_now(),
        "query_limit": VARIANT_QUERY_LIMIT,
        "detail_limit": VARIANT_DETAIL_LIMIT,
        "count_listed": len(rows),
        "repos": rows,
    }


def estimate_repo(
    repo_id: str,
    revision: str | None = None,
    vault: Path = Path("vault"),
    include: list[str] | None = None,
    variants: bool = False,
    progress=print,
) -> dict:
    api = HfApi()
    progress(f"Resolving {repo_id} @ {revision or 'main'} (metadata only, no download) ...")
    try:
        info = api.model_info(repo_id, revision=revision, files_metadata=True)
    except (HfHubHTTPError, OSError) as exc:
        raise SystemExit(f"error: cannot resolve {repo_id} @ {revision or 'main'}: {exc}")

    files = [{"path": s.rfilename, "size": s.size} for s in info.siblings or []]
    full_count, full_total = len(files), sum(f["size"] or 0 for f in files)
    if include:
        files = [f for f in files if any(fnmatch(f["path"], pat) for pat in include)]

    weights = [f for f in files if f["path"].lower().endswith(WEIGHT_SUFFIXES)]
    support = [f for f in files if not f["path"].lower().endswith(WEIGHT_SUFFIXES)]
    total = sum(f["size"] or 0 for f in files)
    weight_bytes = sum(f["size"] or 0 for f in weights)
    largest = max(files, key=lambda f: f["size"] or 0, default=None)

    prospective_paths = [f"model/{f['path']}" for f in files]
    engines = detect_engines(prospective_paths)
    completeness = check_completeness("model", prospective_paths)

    bundle_dir = bundle_dir_for(vault, repo_id, info.sha)
    scratch = (largest["size"] or 0) if largest else 0
    needed = total + scratch
    checked_path, free = _disk_probe(vault)
    if free >= needed * 1.1:
        verdict = "ok"
    elif free >= needed:
        verdict = "tight"
    else:
        verdict = "insufficient"

    est = {
        "as_of": utc_now(),
        "source": {
            "repo_id": repo_id,
            "upstream_url": f"https://huggingface.co/{repo_id}",
            "revision_ref": revision or "main",
            "revision": info.sha,
            "pipeline_tag": info.pipeline_tag,
            "license": _license_from_tags(info.tags),
            "gated": bool(info.gated),
            "last_modified_upstream": info.last_modified.isoformat(timespec="seconds")
                                      if info.last_modified else None,
        },
        "subset": {"include": include,
                   "full_file_count": full_count,
                   "full_total_size_bytes": full_total} if include else None,
        "parameters": _safetensors_params(info),
        "payload": {
            "file_count": len(files),
            "total_size_bytes": total,
            "weights": {"count": len(weights), "bytes": weight_bytes},
            "support": {"count": len(support), "bytes": total - weight_bytes},
            "largest_file": largest,
            "unknown_size_count": sum(1 for f in files if f["size"] is None),
        },
        "engines": engines,
        "completeness": completeness,
        "bundle": {
            "dir": str(bundle_dir),
            "exists": (bundle_dir / "manifest.json").is_file(),
        },
        "estimates": {
            "download_scratch_bytes": scratch,
            "min_ram_gb": round(weight_bytes * RAM_FACTOR / 1024**3, 1) if weight_bytes else None,
            "min_vram_gb": round(weight_bytes * RAM_FACTOR / 1024**3, 1) if weight_bytes else None,
            "notes": "RAM/VRAM = weight bytes x1.2 (as in manifest runtime); "
                     "scratch = largest file in flight during download.",
        },
        "disk": {
            "checked_path": str(checked_path),
            "free_bytes": free,
            "needed_bytes": needed,
            "verdict": verdict,
        },
        "variants": None,
    }
    if variants:
        est["variants"] = _list_variants(api, repo_id, progress)
    return est


# ------------------------------------------------------------------ formatting

def print_estimate(est: dict, progress=print) -> None:
    p = progress
    src, pay = est["source"], est["payload"]
    params = est["parameters"]

    p(f"\n{src['repo_id']} @ {src['revision_ref']} -> {src['revision'][:12]}")
    facts = [src["pipeline_tag"], f"license {src['license'] or '?'}"]
    if src["gated"]:
        facts.append("GATED (archive needs an accepted license + auth token)")
    p(f"  {' | '.join(str(f) for f in facts if f)}")
    if est["subset"]:
        sub = est["subset"]
        p(f"  subset:       only files matching {', '.join(sub['include'])} "
          f"(full repo: {sub['full_file_count']} files, {human_size(sub['full_total_size_bytes'])})")

    if params:
        by_dtype = params["by_dtype"] or {}
        if len(by_dtype) > 1:
            split = ", ".join(f"{human_params(n)} {d}" for d, n in
                              sorted(by_dtype.items(), key=lambda kv: -kv[1]))
            dtypes = f" ({split})"
        else:
            dtypes = f" {params['dominant_dtype']}" if params["dominant_dtype"] else ""
        p(f"  parameters:   {human_params(params['total'])}{dtypes}  [upstream safetensors metadata]")
    else:
        p("  parameters:   not published upstream")
    n_files = lambda n: f"{n} file{'s' if n != 1 else ''}"
    p(f"  payload:      {n_files(pay['file_count'])}, {human_size(pay['total_size_bytes'])}")
    p(f"                weights {human_size(pay['weights']['bytes'])} in {n_files(pay['weights']['count'])}"
      + (f" (largest {human_size(pay['largest_file']['size'])}: {pay['largest_file']['path']})"
         if pay["largest_file"] else ""))
    p(f"                support {human_size(pay['support']['bytes'])} in {n_files(pay['support']['count'])}")
    if pay["unknown_size_count"]:
        p(f"                WARNING: {pay['unknown_size_count']} files have no upstream size")
    p(f"  engines:      {', '.join(est['engines']) or 'none recognized'}")
    comp = est["completeness"]
    missing = ", ".join(comp.get("missing_required") or [])
    p(f"  completeness: {comp['status']}" + (f" (missing: {missing})" if missing else ""))

    e = est["estimates"]
    p(f"  estimated:    download scratch +{human_size(e['download_scratch_bytes'])} (largest file in flight), "
      f"min RAM/VRAM {e['min_ram_gb']} GB (weight bytes x1.2)")

    b, d = est["bundle"], est["disk"]
    p(f"  bundle:       {b['dir']}" + ("  (EXISTS — archive would need --force)" if b["exists"] else "  (new)"))
    verdict_note = {
        "ok": "OK",
        "tight": "TIGHT — under 10% headroom",
        "insufficient": "INSUFFICIENT",
    }[d["verdict"]]
    p(f"  disk:         needs ~{human_size(d['needed_bytes'])}, "
      f"free {human_size(d['free_bytes'])} at {d['checked_path']} — {verdict_note}")

    if est["variants"]:
        v = est["variants"]
        p(f"\n  Quantized variants upstream ({v['count_listed']} listed, cap {v['query_limit']}; "
          f"sizes fetched for top {v['detail_limit']} by downloads):")
        for row in v["repos"][:v["detail_limit"]]:
            size = human_size(row["total_size_bytes"]) if row["total_size_bytes"] else "?"
            fmts = ",".join(row["formats"] or ["?"])
            p(f"    {size:>10}  {fmts:<12} {row['repo_id']}  ({row['downloads']:,} downloads)")
        rest = v["count_listed"] - v["detail_limit"]
        if rest > 0:
            p(f"    ... and {rest} more (modelvault estimate <repo> to size any of them)")

    cmd = f"modelvault archive {src['repo_id']}"
    if src["revision_ref"] != "main":
        cmd += f" --revision {src['revision_ref']}"
    if est["subset"]:
        p(f"\nNOTE: archive has no subset mode yet — `{cmd}` would fetch the FULL repo "
          f"({human_size(est['subset']['full_total_size_bytes'])}), not this subset. "
          "See docs/QUANTIZATION.md (proposed --include).\n")
    else:
        p(f"\nTo archive: {cmd}\n")

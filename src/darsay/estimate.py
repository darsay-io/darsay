"""Estimate a bundle's full footprint before downloading anything.

`darsay estimate` is a read-only preflight for `archive`: it asks the
source provider for the pinned revision's file inventory and reports what
the bundle would cost — payload size, parameter count (models) or a formats
breakdown (datasets), engines, completeness, disk headroom — without
downloading a single payload byte or writing anything to disk.

File sizes and parameter counts come straight from upstream metadata and are
exact; anything derived (RAM needs, download scratch) is labeled an estimate.
Query caps on the variant listing are recorded in the result, mirroring the
manifest's `query_limit` convention.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from .archiver import bundle_dir_for, utc_now
from .hydrate import detect_engines
from .readme_gen import human_params, human_size
from .schema import check_completeness, payload_root_for
from .sources import SourceError, SourceRef, get_provider, parse_source

WEIGHT_SUFFIXES = (".safetensors", ".bin", ".gguf", ".pt", ".pth")
DATA_SUFFIXES = (".parquet", ".jsonl", ".json", ".csv", ".arrow", ".txt", ".tsv")
RAM_FACTOR = 1.2  # same heuristic as metadata.estimate_runtime


def _disk_probe(vault: Path) -> tuple[Path, int]:
    """Free bytes at the vault target, walking up to the nearest existing dir."""
    probe = vault.resolve()
    while not probe.exists():
        probe = probe.parent
    return probe, shutil.disk_usage(probe).free


def _format_breakdown(files: list[dict]) -> dict:
    by_ext: dict[str, dict] = {}
    for f in files:
        ext = Path(f["path"]).suffix.lower().lstrip(".") or "(none)"
        entry = by_ext.setdefault(ext, {"file_count": 0, "total_size_bytes": 0})
        entry["file_count"] += 1
        entry["total_size_bytes"] += f["size"] or 0
    return dict(
        sorted(by_ext.items(), key=lambda kv: (-kv[1]["total_size_bytes"], kv[0]))
    )


def estimate(
    source: str | SourceRef,
    revision: str | None = None,
    vault: Path = Path("vault"),
    include: list[str] | None = None,
    variants: bool = False,
    progress=print,
) -> dict:
    ref = source if isinstance(source, SourceRef) else parse_source(source)
    provider = get_provider(ref.provider)
    repo_type = ref.artifact_type
    progress(
        f"Resolving {ref.canonical} @ {revision or provider.default_revision} "
        "(metadata only, no download) ..."
    )
    try:
        snapshot = provider.pin(ref, revision, require_access=False)
    except SourceError as exc:
        raise SystemExit(str(exc)) from None

    files = [
        {"path": f.path, "size": f.size, "sha256": f.sha256, "git_sha1": f.git_sha1}
        for f in snapshot.files
    ]
    subset = None
    if include:
        from .subset import select_subset

        files, subset = select_subset(files, include)

    primary_suffixes = DATA_SUFFIXES if repo_type == "dataset" else WEIGHT_SUFFIXES
    primary = [f for f in files if f["path"].lower().endswith(primary_suffixes)]
    support = [f for f in files if not f["path"].lower().endswith(primary_suffixes)]
    total = sum(f["size"] or 0 for f in files)
    primary_bytes = sum(f["size"] or 0 for f in primary)
    largest = max(files, key=lambda f: f["size"] or 0, default=None)

    root = payload_root_for(repo_type)
    prospective_paths = [f"{root}/{f['path']}" for f in files]
    engines = detect_engines(prospective_paths)
    completeness = check_completeness(repo_type, prospective_paths)

    bundle_dir = bundle_dir_for(vault, ref, snapshot.revision)
    scratch = (largest["size"] or 0) if largest else 0
    needed = total + scratch
    checked_path, free = _disk_probe(vault)
    if free >= needed * 1.1:
        verdict = "ok"
    elif free >= needed:
        verdict = "tight"
    else:
        verdict = "insufficient"

    ram_gb = (
        round(primary_bytes * RAM_FACTOR / 1024**3, 1)
        if repo_type == "model" and primary_bytes
        else None
    )
    est = {
        "as_of": utc_now(),
        "artifact_type": repo_type,
        "source": {
            "provider": ref.provider,
            "address": ref.canonical,
            "repo_id": ref.locator,
            "upstream_url": ref.url,
            "revision_ref": snapshot.revision_ref,
            "revision": snapshot.revision,
            "pipeline_tag": snapshot.pipeline_tag,
            "license": snapshot.license_id,
            "gated": bool((snapshot.metadata or {}).get("gated")),
            "last_modified_upstream": snapshot.last_modified,
        },
        "subset": subset,
        "parameters": snapshot.parameters,
        "formats": _format_breakdown(files) if repo_type == "dataset" else None,
        "payload": {
            "file_count": len(files),
            "total_size_bytes": total,
            ("data" if repo_type == "dataset" else "weights"): {
                "count": len(primary),
                "bytes": primary_bytes,
            },
            "support": {"count": len(support), "bytes": total - primary_bytes},
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
            "min_ram_gb": ram_gb,
            "min_vram_gb": ram_gb,
            "notes": (
                "scratch = largest file in flight during download; "
                "RAM/VRAM not applicable to dataset bundles."
                if repo_type == "dataset"
                else "RAM/VRAM = weight bytes x1.2 (as in manifest runtime); "
                "scratch = largest file in flight during download."
            ),
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
        listed = provider.variants(ref, progress)
        if listed is None and repo_type == "dataset":
            progress(
                "(--variants lists quantized variants of a model; not applicable to datasets)"
            )
        est["variants"] = listed
    return est


def estimate_repo(
    repo_id: str,
    revision: str | None = None,
    vault: Path = Path("vault"),
    include: list[str] | None = None,
    variants: bool = False,
    repo_type: str = "model",
    progress=print,
) -> dict:
    """Estimate by locator (``owner/name`` or ``datasets/owner/name``)."""
    loc = f"datasets/{repo_id}" if repo_type == "dataset" else repo_id
    return estimate(
        loc,
        revision=revision,
        vault=vault,
        include=include,
        variants=variants,
        progress=progress,
    )


def print_estimate(est: dict, progress=print) -> None:
    p = progress
    src, pay = est["source"], est["payload"]
    params = est["parameters"]
    is_dataset = est.get("artifact_type") == "dataset"
    ref = src.get("address") or (
        f"datasets/{src['repo_id']}" if is_dataset else src["repo_id"]
    )

    p(f"\n{ref} @ {src['revision_ref']} -> {src['revision'][:12]}")
    facts = [src["pipeline_tag"], f"license {src['license'] or '?'}"]
    if src["gated"]:
        facts.append("GATED (archive needs an accepted license + auth token)")
    p(f"  {' | '.join(str(f) for f in facts if f)}")
    if est["subset"]:
        sub = est["subset"]
        extra = " + sidecars" if sub.get("sidecars") else ""
        p(
            f"  subset:       only files matching {', '.join(sub['include'])}{extra} "
            f"(full repo: {sub['full_file_count']} files, {human_size(sub['full_total_size_bytes'])})"
        )

    if is_dataset:
        fmts = est["formats"] or {}
        if fmts:
            breakdown = ", ".join(
                f"{ext} {human_size(d['total_size_bytes'])} in {d['file_count']}"
                for ext, d in fmts.items()
            )
            p(f"  formats:      {breakdown}")
        else:
            p("  formats:      no files listed upstream")
    elif params:
        by_dtype = params["by_dtype"] or {}
        if len(by_dtype) > 1:
            split = ", ".join(
                f"{human_params(n)} {d}"
                for d, n in sorted(by_dtype.items(), key=lambda kv: -kv[1])
            )
            dtypes = f" ({split})"
        else:
            dtypes = f" {params['dominant_dtype']}" if params["dominant_dtype"] else ""
        p(
            f"  parameters:   {human_params(params['total'])}{dtypes}  [upstream safetensors metadata]"
        )
    else:
        p("  parameters:   not published upstream")

    def n_files(n):
        return f"{n} file{'s' if n != 1 else ''}"

    primary_key, primary_label = (
        ("data", "data") if is_dataset else ("weights", "weights")
    )
    p(
        f"  payload:      {n_files(pay['file_count'])}, {human_size(pay['total_size_bytes'])}"
    )
    p(
        f"                {primary_label} {human_size(pay[primary_key]['bytes'])} in {n_files(pay[primary_key]['count'])}"
        + (
            f" (largest {human_size(pay['largest_file']['size'])}: {pay['largest_file']['path']})"
            if pay["largest_file"]
            else ""
        )
    )
    p(
        f"                support {human_size(pay['support']['bytes'])} in {n_files(pay['support']['count'])}"
    )
    if pay["unknown_size_count"]:
        p(
            f"                WARNING: {pay['unknown_size_count']} files have no upstream size"
        )
    if is_dataset:
        p("  engines:      none (dataset bundle — hydrate/run not applicable)")
    else:
        p(f"  engines:      {', '.join(est['engines']) or 'none recognized'}")
    comp = est["completeness"]
    missing = ", ".join(comp.get("missing_required") or [])
    p(
        f"  completeness: {comp['status']}"
        + (f" (missing: {missing})" if missing else "")
    )

    e = est["estimates"]
    if is_dataset:
        p(
            f"  estimated:    download scratch +{human_size(e['download_scratch_bytes'])} (largest file in flight)"
        )
    else:
        p(
            f"  estimated:    download scratch +{human_size(e['download_scratch_bytes'])} (largest file in flight), "
            f"min RAM/VRAM {e['min_ram_gb']} GB (weight bytes x1.2)"
        )

    b, d = est["bundle"], est["disk"]
    p(
        f"  bundle:       {b['dir']}"
        + ("  (EXISTS — archive would need --force)" if b["exists"] else "  (new)")
    )
    verdict_note = {
        "ok": "OK",
        "tight": "TIGHT — under 10% headroom",
        "insufficient": "INSUFFICIENT",
    }[d["verdict"]]
    p(
        f"  disk:         needs ~{human_size(d['needed_bytes'])}, "
        f"free {human_size(d['free_bytes'])} at {d['checked_path']} — {verdict_note}"
    )

    if est["variants"]:
        v = est["variants"]
        p(
            f"\n  Quantized variants upstream ({v['count_listed']} listed, cap {v['query_limit']}; "
            f"sizes fetched for top {v['detail_limit']} by downloads):"
        )
        for row in v["repos"][: v["detail_limit"]]:
            size = (
                human_size(row["total_size_bytes"]) if row["total_size_bytes"] else "?"
            )
            fmts = ",".join(row["formats"] or ["?"])
            p(
                f"    {size:>10}  {fmts:<12} {row['repo_id']}  ({row['downloads']:,} downloads)"
            )
        rest = v["count_listed"] - v["detail_limit"]
        if rest > 0:
            p(f"    ... and {rest} more (darsay estimate <source> to size any of them)")

    import shlex

    cmd = f"darsay archive {shlex.quote(ref)}"
    if src["revision_ref"] != "main":
        cmd += f" --revision {shlex.quote(str(src['revision_ref']))}"
    if est["subset"]:
        for pat in est["subset"]["include"]:
            cmd += f" --include {shlex.quote(pat)}"
        p(f"\nTo archive this subset: {cmd}\n")
    else:
        p(f"\nTo archive: {cmd}\n")

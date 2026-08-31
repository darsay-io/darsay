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
import sys
from pathlib import Path

from .archiver import bundle_dir_for, utc_now
from .config import free_space_floor
from .hydrate import detect_engines
from .progress import color_enabled, dimmed, emphasized, format_percent, styled_bar
from .readme_gen import human_params, human_size
from .schema import check_completeness, payload_root_for
from .sources import SourceError, SourceRef, get_provider, parse_source
from .transfer import disk_verdict

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


def _dominant_format(primary: list[dict]) -> str | None:
    """Extension carrying most of the primary (weight / data) bytes, e.g. ``gguf``.

    Ties and all-unknown sizes fall back to file count. ``None`` when there
    are no primary files. Feeds the catalog ``quant`` hint.
    """
    by_ext: dict[str, tuple[int, int]] = {}
    for f in primary:
        ext = Path(f["path"]).suffix.lower().lstrip(".") or "(none)"
        size, count = by_ext.get(ext, (0, 0))
        by_ext[ext] = (size + (f["size"] or 0), count + 1)
    if not by_ext:
        return None
    return max(by_ext, key=lambda k: by_ext[k])


def _existing_transfer(
    ref: SourceRef,
    revision: str | None,
    vault: Path,
    bundle_dir: Path,
    files: list[dict],
) -> dict | None:
    """Read-only look at bytes this vault has already banked for the source.

    Mirrors what ``archive`` would find on its next run: a registered bundle
    at the pinned revision, an in-progress transfer ledger, or a ledger-less
    payload that reconciliation would hash and adopt. Nothing is written or
    hashed here, so ``unverified`` means a size match pending a digest check.
    """
    from .hashing import iter_payload_files
    from .transfer import find_resume

    root = payload_root_for(ref.artifact_type)
    if (bundle_dir / "manifest.json").is_file():
        total = sum(f["size"] or 0 for f in files)
        count = len(files)
        return {
            "status": "registered",
            "resume_dir": str(bundle_dir),
            "has_ledger": False,
            "pinned_revision": None,
            "pinned_revision_ref": None,
            "files": {
                "total": count,
                "verified": count,
                "unverified": 0,
                "moved": 0,
                "partial": 0,
                "missing": 0,
            },
            "bytes": {
                "total": total,
                "verified": total,
                "unverified": 0,
                "moved": 0,
                "partial": 0,
                "missing": 0,
                "banked": total,
                "remaining_network": 0,
            },
            "scratch_bytes": 0,
        }
    try:
        resume = find_resume(vault, ref, revision, root)
    except SystemExit:
        # Ambiguous partials; archive will explain — price a fresh download.
        return None
    if resume is None:
        return None
    resume_dir, ledger = resume
    payload_dir = resume_dir / root
    if ledger is not None:
        expected = ledger["expected"]
        states = ledger["files"]
        provider = get_provider(ledger.get("provider") or ref.provider)
    else:
        expected = [
            {
                "path": f["path"],
                "size": f["size"],
                "lfs_sha256": f["sha256"],
                "git_sha1": f["git_sha1"],
            }
            for f in files
        ]
        states = {}
        provider = get_provider(ref.provider)

    present = dict(iter_payload_files(payload_dir)) if payload_dir.is_dir() else {}
    counts = {"verified": 0, "unverified": 0, "moved": 0, "partial": 0, "missing": 0}
    sizes = {"verified": 0, "unverified": 0, "moved": 0, "partial": 0, "missing": 0}
    total = 0
    scratch = 0
    for item in expected:
        size = item.get("size") or 0
        total += size
        path = present.get(item["path"])
        size_ok = path is not None and (
            item.get("size") is None or path.stat().st_size == size
        )
        state = states.get(item["path"]) or {}
        if size_ok:
            bucket = "verified" if state.get("status") == "verified" else "unverified"
            counts[bucket] += 1
            sizes[bucket] += size
            continue
        # A moved file's bytes are verified in another vault: not present
        # here, not this vault's to fetch. archive on the source will not
        # re-download it — assembly with the other vault registers.
        if state.get("status") == "moved":
            counts["moved"] += 1
            sizes["moved"] += size
            continue
        banked = min(provider.partial_bytes(payload_dir, item), size) if size else 0
        if banked:
            counts["partial"] += 1
            sizes["partial"] += banked
        else:
            counts["missing"] += 1
            sizes["missing"] += size
        scratch = max(scratch, size)
    banked_total = sizes["verified"] + sizes["unverified"] + sizes["partial"]
    return {
        "status": "in_progress",
        "resume_dir": str(resume_dir),
        "has_ledger": ledger is not None,
        "pinned_revision": ledger["revision"] if ledger else None,
        "pinned_revision_ref": ledger["revision_ref"] if ledger else None,
        "files": {"total": len(expected), **counts},
        "bytes": {
            "total": total,
            **sizes,
            "banked": banked_total,
            "remaining_network": max(0, total - banked_total - sizes["moved"]),
        },
        "scratch_bytes": scratch,
    }


def estimate(
    source: str | SourceRef,
    revision: str | None = None,
    vault: Path = Path("vault"),
    include: list[str] | None = None,
    variants: bool = False,
    full: bool = False,
    progress=print,
) -> dict:
    """Price what ``archive`` would do for this source.

    A model source with no explicit ``include`` and no existing pin is
    classified the way ``archive`` classifies it, and the priced payload
    is the masters-first selection (``subset.policy``); ``full=True``
    prices the whole repo. Bounded header reads, nothing written.
    """
    ref = source if isinstance(source, SourceRef) else parse_source(source)
    provider = get_provider(ref.provider)
    progress(
        f"Resolving {ref.canonical} @ {revision or provider.default_revision} "
        "(metadata + bounded header reads, no download) ..."
    )
    try:
        snapshot = provider.pin(ref, revision, require_access=False)
    except SourceError as exc:
        raise SystemExit(str(exc)) from None
    if snapshot.source.canonical != ref.canonical:
        progress(
            f"Resolved {ref.canonical} as {snapshot.source.artifact_type} "
            f"{snapshot.source.canonical}"
        )
        ref = snapshot.source
    repo_type = ref.artifact_type

    files = [
        {"path": f.path, "size": f.size, "sha256": f.sha256, "git_sha1": f.git_sha1}
        for f in snapshot.files
    ]
    subset = None
    if include:
        from .subset import select_subset

        files, subset = select_subset(files, include)
    elif repo_type == "model" and not full:
        # Price the default acquisition. An existing pin already decided
        # its selection; the download block below reflects it as-is.
        probe_dir = bundle_dir_for(vault, ref, snapshot.revision)
        has_pin = (probe_dir / "manifest.json").is_file()
        if not has_pin:
            from .transfer import find_resume

            try:
                has_pin = (
                    find_resume(vault, ref, revision, payload_root_for(repo_type))
                    is not None
                )
            except SystemExit:
                has_pin = True  # ambiguous partials; archive will explain
        if not has_pin:
            from .classify import masters_policy
            from .subset import select_subset

            policy_include, policy_record = masters_policy(
                provider, ref, snapshot, vault, progress
            )
            if policy_include:
                files, subset = select_subset(files, policy_include)
                subset["policy"] = policy_record["policy"]
                subset["classification"] = policy_record["classification"]

    primary_suffixes = DATA_SUFFIXES if repo_type == "dataset" else WEIGHT_SUFFIXES
    primary = [f for f in files if f["path"].lower().endswith(primary_suffixes)]
    support = [f for f in files if not f["path"].lower().endswith(primary_suffixes)]
    total = sum(f["size"] or 0 for f in files)
    primary_bytes = sum(f["size"] or 0 for f in primary)
    largest = max(files, key=lambda f: f["size"] or 0, default=None)
    dominant_format = _dominant_format(primary)

    root = payload_root_for(repo_type)
    prospective_paths = [f"{root}/{f['path']}" for f in files]
    engines = detect_engines(prospective_paths)
    completeness = check_completeness(repo_type, prospective_paths)

    bundle_dir = bundle_dir_for(vault, ref, snapshot.revision)
    transfer = _existing_transfer(ref, revision, vault, bundle_dir, files)
    if transfer is not None:
        remaining = transfer["bytes"]["remaining_network"]
        scratch = transfer["scratch_bytes"]
    else:
        remaining = total
        scratch = (largest["size"] or 0) if largest else 0
    needed = remaining + scratch
    checked_path, free = _disk_probe(vault)
    floor = free_space_floor(vault)
    verdict = disk_verdict(free, needed, floor)

    ram_gb = (
        round(primary_bytes * RAM_FACTOR / 1024**3, 1)
        if repo_type == "model" and primary_bytes
        else None
    )
    if transfer is None:
        bundle_state = "new"
    elif transfer["status"] == "registered":
        bundle_state = "registered"
    elif transfer["has_ledger"]:
        bundle_state = "resuming"
    else:
        bundle_state = "adoptable"
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
            "dominant_format": dominant_format,
        },
        "transfer": transfer,
        "engines": engines,
        "completeness": completeness,
        "bundle": {
            "dir": transfer["resume_dir"] if transfer else str(bundle_dir),
            "exists": (bundle_dir / "manifest.json").is_file(),
            "state": bundle_state,
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
            "min_free_bytes": floor,
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


def _n_files(n: int) -> str:
    return f"{n} file{'s' if n != 1 else ''}"


def _download_lines(est: dict, *, width: int = 80, color: bool = False) -> list[str]:
    """The download block: a static preview of the archive transfer panel.

    Same bar, percent, and ``done / total`` layout as ``progress.snapshot_lines``,
    fed from banked-vs-remaining state instead of a live meter, with dim
    breakdown lines showing how banked bytes work into the total.
    """
    transfer = est.get("transfer")
    pay = est["payload"]
    total = transfer["bytes"]["total"] if transfer else pay["total_size_bytes"]
    files_total = transfer["files"]["total"] if transfer else pay["file_count"]
    label = f"  {'download:':<14}"
    indent = " " * len(label)
    if not total:
        return [label + "nothing to fetch (no sized files upstream)"]

    banked = transfer["bytes"]["banked"] if transfer else 0
    moved = (transfer["bytes"].get("moved") or 0) if transfer else 0
    remaining = transfer["bytes"]["remaining_network"] if transfer else total
    # A skeleton's moved bytes are done for this pin (verified in another
    # vault), so the bar counts them alongside what is banked here — it
    # answers "how much of this pin exists anywhere", matching `list`.
    accounted = banked + moved
    fraction = min(1.0, accounted / total)
    bar_width = min(28, max(12, max(60, width) - 56))
    bar = styled_bar(fraction, bar_width, color=color)
    percent = emphasized(format_percent(fraction), color=color)
    bytes_part = (
        f"{human_size(accounted)} / {emphasized(human_size(total), color=color)}"
    )
    lines = [f"{label}{bar}  {percent}   {bytes_part}"]

    def note(text: str) -> None:
        lines.append(indent + dimmed(text, color=color))

    if transfer is None:
        note(
            f"nothing banked yet — full {human_size(total)} in {_n_files(files_total)} to fetch"
        )
        return lines
    if transfer["status"] == "registered":
        note("bundle already archived — nothing left to fetch")
        return lines

    sizes, counts = transfer["bytes"], transfer["files"]
    segments = [
        f"{human_size(sizes[bucket])} {bucket} in {_n_files(counts[bucket])}"
        for bucket in ("verified", "unverified", "partial")
        if counts[bucket]
    ]
    if segments:
        note(f"banked {human_size(banked)} = " + " + ".join(segments))
    if counts.get("moved"):
        note(
            f"{human_size(sizes['moved'])} in {_n_files(counts['moved'])} "
            "moved to another vault (verified there)"
        )
    if remaining:
        fetch_files = counts["partial"] + counts["missing"]
        note(f"still to fetch {human_size(remaining)} in {_n_files(fetch_files)}")
    elif moved:
        note(
            "nothing to fetch here — assemble with the vault holding the moved "
            "files to register"
        )
    else:
        note("nothing left to fetch — next archive run verifies and registers")
    if not transfer["has_ledger"]:
        note("no transfer ledger — archive re-hashes the payload and adopts matches")
    pinned = transfer["pinned_revision"]
    estimated = est["source"]["revision"]
    if pinned and pinned != estimated:
        note(
            f"resumes pinned revision {pinned[:12]} — "
            f"upstream {transfer['pinned_revision_ref']} has since moved to {estimated[:12]}"
        )
    return lines


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
        if sub.get("policy"):
            p(
                f"  masters-first: {_n_files(sub['kept_file_count'])}, "
                f"{human_size(sub['kept_total_size_bytes'])} of the full repo "
                f"({sub['full_file_count']} files, "
                f"{human_size(sub['full_total_size_bytes'])}) — --full prices everything"
            )
        else:
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

    primary_key, primary_label = (
        ("data", "data") if is_dataset else ("weights", "weights")
    )
    p(
        f"  payload:      {_n_files(pay['file_count'])}, {human_size(pay['total_size_bytes'])}"
    )
    p(
        f"                {primary_label} {human_size(pay[primary_key]['bytes'])} in {_n_files(pay[primary_key]['count'])}"
        + (
            f" (largest {human_size(pay['largest_file']['size'])}: {pay['largest_file']['path']})"
            if pay["largest_file"]
            else ""
        )
    )
    p(
        f"                support {human_size(pay['support']['bytes'])} in {_n_files(pay['support']['count'])}"
    )
    if not is_dataset and params:
        from .catalog import expected_weight_bytes, hints_for

        if "redundant" in hints_for(est):
            expected = expected_weight_bytes(params.get("by_dtype"))
            ratio = pay["weights"]["bytes"] / expected
            p(
                f"                NOTE: weights ≈ {ratio:.1f}x one "
                f"{human_params(params['total'])}-param "
                f"{params['dominant_dtype']} copy — several weight sets "
                f"likely (darsay classify {ref})"
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

    color = color_enabled(sys.stdout)
    width = shutil.get_terminal_size(fallback=(80, 24)).columns
    for line in _download_lines(est, width=width, color=color):
        p(line)

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
    bundle_note = {
        "registered": "  (EXISTS — archive would need --force)",
        "resuming": "  (in progress — archive resumes here)",
        "adoptable": "  (payload present without a ledger — archive re-hashes and adopts)",
        "new": "  (new)",
    }[b.get("state") or ("registered" if b["exists"] else "new")]
    p(f"  bundle:       {b['dir']}{bundle_note}")
    verdict_note = {
        "ok": "OK",
        "tight": "TIGHT — under 10% headroom",
        "insufficient": "INSUFFICIENT",
    }[d["verdict"]]
    transfer = est.get("transfer")
    more = (
        " more"
        if transfer and transfer["bytes"]["banked"] and d["needed_bytes"]
        else ""
    )
    floor = d.get("min_free_bytes")
    floor_note = f" ({human_size(floor)} floor)" if floor else ""
    p(
        f"  disk:         needs ~{human_size(d['needed_bytes'])}{more}, "
        f"free {human_size(d['free_bytes'])}{floor_note} at {d['checked_path']} — {verdict_note}"
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
    if est["subset"] and est["subset"].get("policy"):
        p(f"\nTo archive (masters-first is the default): {cmd}\n")
    elif est["subset"]:
        for pat in est["subset"]["include"]:
            cmd += f" --include {shlex.quote(pat)}"
        p(f"\nTo archive this subset: {cmd}\n")
    else:
        p(f"\nTo archive: {cmd}\n")

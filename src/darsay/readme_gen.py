"""Generate the human-readable bundle README.md from manifest.json + curation.md.

The README is a derived view: edit curation.md (notes) or manifest.json
(structured fields), then `darsay regen` to rebuild it.
"""

from __future__ import annotations

from pathlib import Path


def human_size(n: int | None) -> str:
    if n is None:
        return "?"
    size = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024 or unit == "TiB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{n} B"


def human_params(n: int | None) -> str:
    if n is None:
        return "unknown"
    if n >= 1e9:
        return f"{n / 1e9:.2f}B"
    if n >= 1e6:
        return f"{n / 1e6:.1f}M"
    return str(n)


def _tested_hardware_line(tested: list | None) -> str:
    if not tested:
        return "- Tested hardware: not yet tested — `darsay run` records the first real run"
    parts = []
    for e in tested:
        label = e.get("chip") or e.get("host") or "unknown machine"
        perf = f", {e['tokens_per_second']} tok/s" if e.get("tokens_per_second") else ""
        parts.append(f"{label} ({e.get('device', '?')}, {e.get('engine', '?')}{perf}, {str(e.get('at', ''))[:10]})")
    return "- Tested hardware: " + "; ".join(parts)


def _curation_body(bundle_dir: Path) -> str | None:
    path = bundle_dir / "curation.md"
    if not path.is_file():
        return None
    text = path.read_text(encoding="utf-8")
    # Include from the first section heading on (skips the H1 + instruction note),
    # demoting headings one level so they nest under the README's "## Curation".
    idx = text.find("\n## ")
    if idx == -1:
        return None
    body = text[idx + 1:].strip()
    return "\n".join("#" + line if line.startswith("## ") else line for line in body.splitlines())


def _header_table_close(lines: list[str], lic: dict, inv: dict, arc: dict, sec: dict) -> None:
    lines += [
        f"| **License** | {lic['name'] or lic['spdx_id'] or 'UNKNOWN'} |",
        f"| **Payload** | {inv['file_count']} files, {human_size(inv['total_size_bytes'])} |",
        f"| **Integrity** | {sec['integrity_status']} (last check {arc['last_integrity_check']}) |",
    ]


# Upstream tags that flag alignment modifications — surfaced so a bundle README
# never reads like a vanilla derivation of its base model.
ALIGNMENT_MODIFICATION_TAGS = ("abliterated", "uncensored")

_DOWNLOADER_SKIP = {"tool", "version", "python", "platform", "provider"}


def _downloader_clients(downloader: dict) -> str:
    parts = []
    hub = downloader.get("huggingface_hub")
    if hub:
        parts.append(f"huggingface_hub {hub}")
    for key, value in downloader.items():
        if key in _DOWNLOADER_SKIP or key == "huggingface_hub" or value is None:
            continue
        parts.append(f"{key} {value}")
    return (", ".join(parts) + ", ") if parts else ""


def _source_lines(src: dict) -> list[str]:
    lines = [
        "",
        "## Source & provenance",
        "",
        f"- Origin: [{src.get('address') or src['repo_id']}]({src['upstream_url']}) "
        f"via {src.get('provider') or src['origin']}",
        f"- Pinned revision: `{src['revision']}` (ref `{src['revision_ref']}`, last modified upstream {src['last_modified_upstream'] or '?'})",
        f"- Downloaded: {src['download_timestamp']} by {src['downloader']['tool']} v{src['downloader']['version']} "
        f"({_downloader_clients(src['downloader'])}Python {src['downloader']['python']})",
        f"- Signatures: {src['signatures'] or 'none provided upstream'}",
    ]
    access = src.get("access") or {}
    if access.get("gated"):
        lines.append(f"- Access: **gated upstream** (mode: {access['gated']}) — the gate agreement "
                     "is not part of the snapshot; re-fetching requires an account that accepted it")
    flagged = [t for t in (src.get("upstream_tags") or []) if t in ALIGNMENT_MODIFICATION_TAGS]
    if flagged:
        lines.append(f"- Upstream tags flag **alignment modifications**: {', '.join(flagged)} "
                     "— see the archived model card and `curation.md`")
    stats = src.get("upstream_stats_at_archive") or {}
    if stats.get("downloads_last_month") is not None:
        lines.append(f"- Upstream popularity at archive time: {stats['downloads_last_month']:,} downloads/month, "
                     f"{stats.get('likes', 0):,} likes")
    return lines


def _licensing_lines(lic: dict) -> list[str]:
    def flag(v):
        return {True: "yes", False: "no", None: "review required"}[v]

    lines = [
        "",
        "## Licensing",
        "",
        f"- SPDX: `{lic['spdx_id'] or 'unknown'}`" + (" — **needs manual review**" if lic["needs_manual_review"] else ""),
        f"- Commercial use: {flag(lic['commercial_use'])} · Redistribution: {flag(lic['redistribution'])} · "
        f"Modification: {flag(lic['modification'])} · Attribution required: {flag(lic['attribution_required'])}",
        f"- License text in bundle: {', '.join('`' + f + '`' for f in lic['license_files']) or 'NONE SHIPPED UPSTREAM'}",
    ]
    if lic["trademark_terms"]:
        lines.append(f"- Trademark terms: {lic['trademark_terms']}")
    if lic["notes"]:
        lines.append(f"- Note: {lic['notes']}")
    return lines


def _archive_record_lines(arc: dict, sec: dict) -> list[str]:
    return [
        "",
        "## Archive record",
        "",
        f"- Archived: {arc['date_archived']} on `{arc['host']}` ({arc['storage_tier']})",
        f"- Location: `{arc['location']}`",
        f"- Backups: {arc['backup_status']} · Replicas: {len(arc['replicas'])}",
        f"- Trust level: {sec['trust_level']}"
        + (f" — reviewed by {sec['reviewed_by']}" if sec.get("reviewed_by") else ""),
    ]


def _inventory_lines(inv: dict) -> list[str]:
    lines = [
        "",
        "## Inventory",
        "",
        f"Bundle hash (`{inv['bundle_hash']['algorithm']}`): `{inv['bundle_hash']['value']}`",
        "",
        "| File | Size | SHA-256 (first 16) | Upstream match |",
        "|---|---|---|---|",
    ]
    for f in inv["files"]:
        match = {True: "✓", False: "✗ MISMATCH", None: "—"}[f["verified_against_upstream"]]
        lines.append(f"| `{f['path']}` | {human_size(f['size'])} | `{f['sha256'][:16]}…` | {match} |")
    lines.append("\nFull hashes (SHA-256" + (", BLAKE3" if any(f.get("blake3") for f in inv["files"]) else "")
                 + ", upstream LFS/git) are in `manifest.json`.")
    return lines


def _curation_and_footer_lines(bundle_dir: Path, m: dict) -> list[str]:
    curation = _curation_body(bundle_dir)
    lines = ["", "## Curation", ""]
    if curation:
        lines.append(curation)
    else:
        lines.append("_No curation notes yet — edit `curation.md` and run `darsay regen`._")
    lines += [
        "",
        "---",
        f"_Generated by darsay from `manifest.json` (schema v{m['schema_version']}). "
        "Do not edit this file directly; edit `curation.md` or the manifest and run `darsay regen`._",
        "",
    ]
    return lines


def render_bundle_readme(bundle_dir: Path, m: dict) -> str:
    if m["artifact_type"] == "dataset":
        return _render_dataset_readme(bundle_dir, m)
    return _render_model_readme(bundle_dir, m)


def _render_model_readme(bundle_dir: Path, m: dict) -> str:
    ident = m["identity"]
    src = m["source"]
    lic = m["licensing"]
    inv = m["inventory"]
    meta = m["model_metadata"]
    rt = m["runtime"]
    val = m["validation"]
    rel = m["relationships"]
    arc = m["archive"]
    sec = m["security"]

    check = val["checksum_verification"]
    tok_smoke = val["smoke_tests"]["tokenizer"]["status"]
    inf_smoke = val["smoke_tests"]["inference"]["status"]

    lines = [
        f"# {ident['publisher']} / {ident['model_name']}",
        "",
        f"> Archived model bundle `{m['bundle_id']}` — schema v{m['schema_version']}, artifact type `{m['artifact_type']}`.",
        "",
        "| | |",
        "|---|---|",
        f"| **Family** | {ident['family']} |",
        f"| **Publisher** | {ident['publisher']} |",
        f"| **Version** | {ident['version'] or '—'} |",
        f"| **Released** | {ident['release_date'] or 'unknown'} |",
        f"| **Parameters** | {human_params(meta['parameter_count'])} ({meta['precision'] or '?'}) |",
        f"| **Architecture** | {meta['architecture'] or '?'} ({meta['model_type'] or '?'}) |",
        f"| **Context length** | {meta['context_length'] or '?'} |",
    ]
    _header_table_close(lines, lic, inv, arc, sec)
    lines += _source_lines(src)
    lines += _licensing_lines(lic)

    tok = meta["tokenizer"]
    lines += [
        "",
        "## Model metadata",
        "",
        f"- Parameters: {meta['parameter_count']:,}" if meta["parameter_count"] else "- Parameters: unknown",
        f"- Layers {meta['num_hidden_layers']} · hidden {meta['hidden_size']} · heads {meta['num_attention_heads']}"
        + (f" (KV {meta['num_key_value_heads']})" if meta.get("num_key_value_heads") else ""),
        f"- Precision: {meta['precision'] or '?'} · Quantization: {meta['quantization'] or 'none'}"
        f" · Shards: {meta['weight_shards'] or '?'}",
        f"- Tokenizer: {tok['class'] or '?'}, vocab {tok['vocab_size'] or '?'}, "
        f"chat template {'present' if tok['chat_template_present'] else 'absent'}",
        f"- Languages (per model card): {', '.join(meta['languages']) if meta['languages'] else 'not declared'}",
        f"- Training cutoff: {meta['training_cutoff'] or 'not published'}",
        "",
        "## Runtime",
        "",
        f"- Engines (from shipped formats): {', '.join(rt['supported_engines']) if rt['supported_engines'] else 'unknown'}",
        f"- Estimated minimum RAM/VRAM: {rt['estimated_min_ram_gb']} GB (estimate, weights x1.2)",
        _tested_hardware_line(rt["tested_hardware"]),
        f"- CPU inference: {'yes' if rt['cpu_inference'] else 'unknown'}",
        "",
        "## Validation",
        "",
        f"- Checksums: **{check['status'].upper()}** at {check['at']} ({check['files_checked']} files)",
        f"- Completeness: **{val['completeness']['status']}**"
        + (f" — missing recommended: {', '.join(val['completeness']['missing_recommended'])}"
           if val["completeness"].get("missing_recommended") else ""),
        f"- Smoke tests: tokenizer `{tok_smoke}`, inference `{inf_smoke}`",
        f"- Full report: [VERIFICATION.md](VERIFICATION.md) · history in `verification.json`",
        "",
        "## Relationships",
        "",
    ]
    bases = rel.get("base_models") or ([rel["base_model"]] if rel.get("base_model") else [])
    if bases:
        relation = rel.get("base_model_relation")
        lines.append("- Derived from: " + ", ".join(f"`{b}`" for b in bases)
                     + (f" — relation: **{relation}**" if relation else " — relation not declared upstream"))
    else:
        lines.append("- Derived from: none declared (base model)")
    if rel.get("gguf_repos"):
        shown = rel["gguf_repos"][:8]
        more = len(rel["gguf_repos"]) - len(shown)
        lines.append(f"- Known GGUF conversions ({len(rel['gguf_repos'])}): "
                     + ", ".join(f"`{g}`" for g in shown) + (f" … +{more} more" if more > 0 else ""))
    cap = rel.get("query_limit") or 100

    def capped(n):
        return f"≥{n} (query capped)" if n == cap else str(n)

    if rel.get("quantized_versions"):
        lines.append(f"- Known quantizations at archive time: {capped(len(rel['quantized_versions']))} repos (full list in manifest)")
    if rel.get("finetunes_count") is not None:
        lines.append(f"- Public finetunes at archive time: {capped(rel['finetunes_count'])}"
                     + (f" · adapters: {capped(rel['adapters_count'])}" if rel.get("adapters_count") is not None else ""))
    lines.append(f"- Ecosystem snapshot taken: {rel.get('ecosystem_snapshot_as_of', 'n/a')}")

    lines += _archive_record_lines(arc, sec)
    lines += _inventory_lines(inv)

    bundle_path = arc["location"]
    lines += [
        "",
        "## Using this bundle",
        "",
        "One command — builds (or reuses) a local env, then runs a prompt fully offline:",
        "",
        "```bash",
        f"darsay run {bundle_path} \"Say hello in one short sentence.\"",
        "```",
        "",
        "(`darsay hydrate` prepares the env without running; envs live outside the",
        "bundle under the vault's `.runtime/` and are recorded in `hydration.json`.)",
        "",
        "Or point any Hugging Face-compatible loader at the pristine payload directly:",
        "",
        "```python",
        "from transformers import AutoModelForCausalLM, AutoTokenizer",
        "",
        f'path = "{bundle_path}/model"',
        "tokenizer = AutoTokenizer.from_pretrained(path)",
        'model = AutoModelForCausalLM.from_pretrained(path, torch_dtype="auto")',
        "```",
        "",
        "## Reproduce & audit",
        "",
        "```bash",
        "# Re-verify this bundle against its manifest",
        f"darsay verify {bundle_path}",
        "",
        "# Re-create an identical bundle from upstream (bit-for-bit payload)",
        f"darsay archive {src.get('address') or src['repo_id']} --revision {src['revision']}",
        "```",
    ]

    lines += _curation_and_footer_lines(bundle_dir, m)
    return "\n".join(lines)


def _render_dataset_readme(bundle_dir: Path, m: dict) -> str:
    ident = m["identity"]
    src = m["source"]
    lic = m["licensing"]
    inv = m["inventory"]
    dm = m["dataset_metadata"]
    val = m["validation"]
    rel = m["relationships"]
    arc = m["archive"]
    sec = m["security"]

    check = val["checksum_verification"]
    structure_smoke = val["smoke_tests"].get("structure", {}).get("status", "not-run")

    formats = dm.get("formats") or {}
    fmt_summary = ", ".join(f"{ext} ({d['file_count']}, {human_size(d['total_size_bytes'])})"
                            for ext, d in formats.items()) or "—"
    declared = dm.get("declared") or {}
    configs = sorted((declared.get("configs") or {}).keys())
    example_total = declared.get("example_count_total")

    lines = [
        f"# {ident['publisher']} / {ident['model_name']}",
        "",
        f"> Archived dataset bundle `{m['bundle_id']}` — schema v{m['schema_version']}, artifact type `{m['artifact_type']}`.",
        "",
        "| | |",
        "|---|---|",
        f"| **Publisher** | {ident['publisher']} |",
        f"| **Released** | {ident['release_date'] or 'unknown'} |",
        f"| **Formats** | {fmt_summary} |",
        f"| **Configs** | {', '.join(configs) or '—'} |",
        f"| **Examples (declared)** | {f'{example_total:,}' if example_total is not None else 'unknown'} |",
    ]
    _header_table_close(lines, lic, inv, arc, sec)
    lines += _source_lines(src)
    lines += _licensing_lines(lic)

    lines += [
        "",
        "## Dataset metadata",
        "",
        f"- Formats (from the archived inventory): {fmt_summary}",
        f"- Task categories (per dataset card): {', '.join(dm['task_categories']) if dm.get('task_categories') else 'not declared'}",
        f"- Languages (per dataset card): {', '.join(dm['languages']) if dm.get('languages') else 'not declared'}",
        f"- Size category (per dataset card): {', '.join(dm['size_categories']) if dm.get('size_categories') else 'not declared'}",
    ]
    if declared.get("configs"):
        lines += [
            "",
            f"Declared splits — upstream claims from {' + '.join(declared.get('sources') or ['upstream metadata'])}, "
            "recorded, not measured:",
            "",
            "| Config | Split | Examples | Bytes |",
            "|---|---|---|---|",
        ]
        for cfg_name in configs:
            cfg = declared["configs"][cfg_name]
            for split_name, split in (cfg.get("splits") or {}).items():
                ex = f"{split['num_examples']:,}" if split.get("num_examples") is not None else "?"
                nb = human_size(split.get("num_bytes"))
                lines.append(f"| {cfg_name} | {split_name} | {ex} | {nb} |")
    measured = dm.get("measured") or {}
    if measured.get("status") in ("measured", "partial"):
        total_rows = measured.get("total_rows")
        lines.append("")
        lines.append(f"Measured rows ({measured.get('method')}): "
                     + (f"{total_rows:,}" if total_rows is not None else "?")
                     + f" across {len(measured.get('row_counts') or {})} parquet files"
                     + (" — some files unreadable, see manifest" if measured["status"] == "partial" else "")
                     + ".")
    else:
        lines.append("")
        lines.append(f"Measured rows: skipped — {measured.get('reason', 'not recorded')}.")

    lines += [
        "",
        "## Validation",
        "",
        f"- Checksums: **{check['status'].upper()}** at {check['at']} ({check['files_checked']} files)",
        f"- Completeness: **{val['completeness']['status']}**"
        + (f" — missing recommended: {', '.join(val['completeness']['missing_recommended'])}"
           if val["completeness"].get("missing_recommended") else ""),
        f"- Structure checks: `{structure_smoke}` (parquet magic, JSONL parse, CSV sniff — `darsay smoke`)",
        f"- Full report: [VERIFICATION.md](VERIFICATION.md) · history in `verification.json`",
        "",
        "## Relationships",
        "",
        f"- Source datasets (declared upstream): "
        + (", ".join(f"`{d}`" for d in rel["source_datasets"]) if rel.get("source_datasets") else "none declared"),
    ]
    cap = rel.get("query_limit") or 100
    trained = rel.get("models_trained_on")
    if trained is not None:
        count = f"≥{len(trained)} (query capped)" if len(trained) == cap else str(len(trained))
        shown = trained[:8]
        more = len(trained) - len(shown)
        lines.append(f"- Models trained on this dataset at archive time: {count}"
                     + (": " + ", ".join(f"`{t}`" for t in shown) + (f" … +{more} more" if more > 0 else "")
                        if shown else ""))
    else:
        lines.append("- Models trained on this dataset: query failed at archive time")
    lines.append(f"- Ecosystem snapshot taken: {rel.get('ecosystem_snapshot_as_of', 'n/a')}")

    lines += _archive_record_lines(arc, sec)
    lines += _inventory_lines(inv)

    bundle_path = arc["location"]
    root = (inv.get("layout") or {}).get("payload_root", "data/").rstrip("/")
    lines += [
        "",
        "## Using this bundle",
        "",
        f"The payload under `{root}/` is a pristine snapshot of the upstream repo —",
        "point any data reader at the files directly:",
        "",
        "```python",
        "import pyarrow.parquet as pq   # or pandas, polars, datasets",
        "",
        f'table = pq.read_table("{bundle_path}/{root}/<file>.parquet")',
        "```",
        "",
        "(`darsay hydrate`/`run` apply to model bundles; a dataset bundle has no engine.)",
        "",
        "## Reproduce & audit",
        "",
        "```bash",
        "# Re-verify this bundle against its manifest",
        f"darsay verify {bundle_path}",
        "",
        "# Re-create an identical bundle from upstream (bit-for-bit payload)",
        f"darsay archive {src.get('address') or ('datasets/' + src['repo_id'])} --revision {src['revision']}",
        "```",
    ]

    lines += _curation_and_footer_lines(bundle_dir, m)
    return "\n".join(lines)


def write_bundle_readme(bundle_dir: Path, manifest: dict) -> None:
    (bundle_dir / "README.md").write_text(render_bundle_readme(bundle_dir, manifest), encoding="utf-8")

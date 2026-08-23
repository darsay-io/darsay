# manifest.json — schema reference (v1.1.0)

`manifest.json` is the machine-readable source of truth for a bundle. This
document describes every field so a bundle remains interpretable without the
tool. Consumers should check `schema_version` before parsing; the major
component changes only on breaking layout changes.

Conventions:

- Timestamps are ISO 8601 UTC with second precision (`2026-08-23T00:30:58+00:00`).
- File paths are bundle-root-relative POSIX paths (`model/config.json`).
- `null` means *unknown or not yet recorded* — the tool records what it can
  establish and never fabricates. Fields marked **curator** below are meant to
  be filled in by hand (directly in the manifest, or via `curation.md`).

## Top level

| Field | Meaning |
|---|---|
| `schema_version` | Version of this schema (`"1.1.0"`; 1.1 defined the shape of `runtime.tested_hardware`, previously always null). |
| `artifact_type` | Registry key driving completeness rules (`"model"`; future: datasets, GGUF packs, papers — see `src/modelvault/schema.py`). |
| `bundle_id` | `<repo_id lowercased, "/"→"--">@<first 12 of pinned commit>`, e.g. `qwen--qwen3-0.6b@c1899de289a0`. Stable, deterministic, unique per (repo, revision). |

## `identity`

| Field | Meaning |
|---|---|
| `model_name` | Repo name as published (`Qwen3-0.6B`). |
| `family` | Model family, taken from `config.json` `model_type` when available (`qwen3`). |
| `publisher` | Repo owner (`Qwen`). |
| `version` | Family version parsed from the name/model_type; null when not derivable. |
| `release_date` | Upstream repo creation time. |
| `aliases` | Known ids for this model, starting with the source repo id. |

## `source`

Provenance of the download.

| Field | Meaning |
|---|---|
| `origin` | Hosting service (`huggingface`). |
| `repo_id`, `upstream_url` | Where it came from. |
| `revision` | Full commit hash the download is pinned to. Re-archiving this revision reproduces the payload bit-for-bit. |
| `revision_ref` | The ref that was requested (`main`, a tag, or a hash). |
| `last_modified_upstream` | Upstream's last-commit time at archive time. |
| `download_timestamp` | When the archive was made. |
| `downloader` | Tool name/version, `huggingface_hub` version, Python version, platform — enough to reconstruct the download environment. |
| `mirrors_used` | Non-primary sources, if any were used (empty list otherwise). |
| `signatures` | Upstream cryptographic signatures, when provided (null otherwise — Hugging Face repos generally ship none). |
| `upstream_stats_at_archive` | Downloads/month and likes at archive time — a popularity snapshot for the historical record. |
| `upstream_tags` | Raw repo tags at archive time. |

## `licensing`

| Field | Meaning |
|---|---|
| `spdx_id` | License id from upstream repo metadata (`apache-2.0`). |
| `name` | Human name when the id is in the rights-flags table (`src/modelvault/licensing.py`). |
| `license_files` | License/notice text files shipped upstream, as archived paths. The primary one is also copied to the bundle root as `LICENSE`. |
| `commercial_use`, `redistribution`, `modification`, `attribution_required`, `patent_grant` | Rights flags from the table. `null` = unknown license → review manually. Curator convenience, not legal advice. |
| `trademark_terms` | Trademark clauses worth knowing about (e.g. Apache-2.0 §6). |
| `needs_manual_review` | True when the license id is missing or not in the table. |
| `notes` | Free text; auto-set when no license file ships upstream. |

## `inventory`

| Field | Meaning |
|---|---|
| `file_count`, `total_size_bytes` | Payload totals. |
| `bundle_hash` | `{algorithm, value, covers}`. SHA-256 over the sorted `"<sha256>  <path>"` lines of the payload — one value that fingerprints the whole payload. Covers `model/` only; bundle-root metadata is mutable by design. |
| `layout` | `payload_root` (always `model/`) and the list of mutable metadata files. |
| `files[]` | Per file: `path`, `size`, `sha256`, `blake3` (null if blake3 wasn't installed), `upstream_lfs_sha256` (LFS files), `upstream_git_sha1` (small git-blob files), `verified_against_upstream` (true/false/null = no upstream expectation). |

## `model_metadata`

Extracted offline from the payload itself (`config.json`,
`generation_config.json`, `tokenizer_config.json`, safetensors headers — no
torch required).

| Field | Meaning |
|---|---|
| `parameter_count`, `parameters_by_dtype`, `weight_shards` | Counted from safetensors headers. Null for `.bin`/`.gguf`-only payloads. |
| `architecture`, `model_type` | From `config.json` (`Qwen3ForCausalLM`, `qwen3`). |
| `context_length` | `max_position_embeddings`. |
| `precision` | `torch_dtype`, falling back to the dominant tensor dtype. |
| `quantization` | `quantization_config.quant_method` when present, else null. |
| `hidden_size`, `num_hidden_layers`, `num_attention_heads`, `num_key_value_heads`, `tie_word_embeddings` | Architecture shape. |
| `tokenizer` | `class`, `vocab_size`, `model_max_length`, `special_tokens` (bos/eos/pad/…), `chat_template_present`. |
| `languages` | From the model card, when declared. |
| `training_cutoff` | **curator** — rarely published. |
| `generation_defaults` | Sampling defaults from `generation_config.json`. |

## `runtime`

| Field | Meaning |
|---|---|
| `supported_engines` | Derived from shipped formats only (safetensors → transformers, gguf → llama.cpp). |
| `estimated_min_ram_gb`, `estimated_min_vram_gb` | Estimates: weight bytes × 1.2. |
| `tested_hardware` | Measured runs, never estimates. `modelvault run` appends/refreshes one entry per (host, device, engine) on each successful run: `{at, host, os, chip, device, engine, engine_versions, tokens_per_second, status, via}`. Curators may add entries by hand in the same shape. Null until the model has actually run somewhere. |
| `os_support`, `cuda_notes`, `rocm_notes`, `cpu_inference` | Coarse defaults; refine by hand. |
| `notes` | States the estimation method. |

## `validation`

| Field | Meaning |
|---|---|
| `checksum_verification` | Latest run: `at`, `status` (pass/fail), `files_checked`; at archive time also `upstream_mismatches`; on re-verification `missing`/`extra`/`mismatched` path lists and `bundle_hash_match`. |
| `completeness` | Result of the artifact-type rules: `status` (complete/incomplete), per-rule matches, `missing_required`, `missing_recommended`. |
| `smoke_tests.tokenizer` | Encode/decode round-trip via `tokenizers` (or transformers fallback): status, engine, token count, `roundtrip_exact`. |
| `smoke_tests.inference` | Opt-in (`smoke --inference`): greedy generation via transformers — status, prompt, output, new token count. |

Statuses: `pass` / `fail` / `skipped` (dependency or file missing) / `not-run`.

## `relationships`

| Field | Meaning |
|---|---|
| `base_model`, `finetuned_from` | Declared parent from the model card. |
| `quantized_versions`, `gguf_repos` | Downstream repos found at archive time (GGUFs are the `*gguf*` subset). |
| `finetunes_count`, `adapters_count` | Counts of downstream finetune/adapter repos. |
| `query_limit` | Cap on the ecosystem queries (100). A count or list length equal to the cap means **at least** that many — renderers show `≥`. |
| `related_variants`, `successors` | **curator**. |
| `ecosystem_snapshot_as_of` | When the ecosystem queries ran. This section is a historical snapshot, not a live index. |

## `archive`

| Field | Meaning |
|---|---|
| `date_archived` | Creation time of the bundle. Also the fixed mtime used in exports. |
| `archived_by` | **curator** (or automation identity). |
| `location`, `host` | Absolute path and machine. Rewritten on import. |
| `storage_tier`, `backup_status`, `replicas` | Where copies live (`local-disk` / `none` / `[]` until a backup workflow records otherwise). |
| `last_integrity_check`, `last_accessed` | Maintained by `verify`, `smoke`, `info`, import. |
| `imported` | Present on imported bundles: `at`, `from_file`, `file_sha256`, `mvb_format_version`. |

Export events are logged in the sibling file `exports.json`
(`{"exports": [{at, file, sha256, size_bytes, mvb_format_version}]}`), not in
the manifest — see [MVB-FORMAT.md](MVB-FORMAT.md) for why. Hydration state and
run history live in the sibling file `hydration.json` for the same reason:
both are volatile machine-local state, excluded from exports — see
[HYDRATION.md](HYDRATION.md).

## `security`

| Field | Meaning |
|---|---|
| `integrity_status` | `verified-against-upstream` (archive-time cross-check passed) → `compromised` if a later `verify` finds changes; `upstream-mismatch` if the archive-time cross-check itself failed. |
| `unexpected_changes[]` | Append-only log of `{detected_at, type: modified|missing|extra, path}` from failed verifications. |
| `trust_level` | `unreviewed` until a human reviews; then curator-set (e.g. `reviewed`, `trusted`). |
| `reviewed_by`, `review_notes` | **curator**. |

## `curation`

Structured mirror of the curator's notes: `historical_significance`,
`major_capabilities`, `known_limitations`, `successor_models`,
`personal_notes` (all **curator**), plus `curation_file` pointing at
`curation.md` — the free-form file that `modelvault regen` folds into the
bundle README. Prose belongs in `curation.md`; use the structured fields when
downstream tooling needs to query them.

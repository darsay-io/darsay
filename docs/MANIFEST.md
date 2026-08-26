<p align="center">
  <a href="GETTING-STARTED.md">Start here</a> ·
  <a href="CONCEPTS.md">Concepts</a> ·
  <a href="../examples/README.md">Examples</a> ·
  <a href="README.md">All docs</a> ·
  <a href="../README.md">README</a>
</p>

# manifest.json — schema reference (v1.6.0)

> **In one sentence.** The manifest is the recorded facts about a bundle.
> Unknown is `null`. The tool never fabricates.

`manifest.json` is the machine-readable source of truth for a bundle. This
document describes every field so a bundle remains interpretable without the
tool. Check `schema_version` before parsing; the major component changes
only on breaking layout. See [Versioning](#versioning).

Conventions:

- Timestamps are ISO 8601 UTC with second precision (`2026-08-23T00:30:58+00:00`).
- File paths are bundle-root-relative POSIX paths (`model/config.json`,
  `data/train.parquet`) rooted at the bundle's `inventory.layout.payload_root`.
- `null` means *unknown or not yet recorded* — the tool records what it can
  establish and never fabricates. Fields marked **curator** below are meant to
  be filled in by hand (directly in the manifest, or via `curation.md`).
- Sections are universal across artifact types unless marked otherwise.
  Per-type presence: **model bundles** carry `model_metadata` + `runtime`;
  **dataset bundles** carry `dataset_metadata` instead.

## Top level

| Field | Meaning |
|---|---|
| `schema_version` | Version of this schema (`"1.6.0"`). Major changes only on breaking layout. |
| `kind` | Always `"darsay.bundle"`. Any other value (including missing) is a load error. |
| `artifact_type` | Registry key driving completeness rules and the payload root (`"model"` or `"dataset"`; future: GGUF packs, papers — see `src/darsay/schema.py`). |
| `bundle_id` | `<bundle directory name>@<first 12 of pinned revision>`, e.g. `qwen--qwen3-0.6b@c1899de289a0`. Hugging Face dataset bundles take a `datasets--` prefix (`datasets--saidutta69--fable-5-premium@684cb1f849fe`) since model and dataset namespaces can collide on that host. Other providers include their id in the directory name. Stable, deterministic, unique per (source, revision). |

## `identity`

| Field | Meaning |
|---|---|
| `model_name` | Repo name as published (`Qwen3-0.6B`), for datasets too — the field name is kept stable across types. |
| `family` | Model family, taken from `config.json` `model_type` when available (`qwen3`); name-derived fallback otherwise. |
| `publisher` | Repo owner (`Qwen`). |
| `version` | Family version parsed from the name/model_type; null when not derivable. |
| `release_date` | Upstream repo creation time. |
| `aliases` | Known ids for this artifact, starting with the source repo id. |

## `source`

Provenance of the download.

| Field | Meaning |
|---|---|
| `origin` | Hosting service id (`huggingface`). Same value as `provider` for the Hugging Face plugin. |
| `provider` | Acquisition plugin id (`huggingface`). |
| `address` | Canonical source ref (`huggingface:Qwen/Qwen3-0.6B`, `huggingface:datasets/owner/name`). |
| `repo_id`, `upstream_url` | Provider-native locator and its web URL. |
| `revision` | Full commit hash the download is pinned to. Re-archiving this revision reproduces the payload bit-for-bit. |
| `revision_ref` | The ref that was requested (`main`, a tag, or a hash). |
| `last_modified_upstream` | Upstream's last-commit time at archive time. |
| `download_timestamp` | When the transfer completed and the bundle was registered. |
| `transfer` | Durable summary of incremental acquisition: `sessions`, `started`, `completed`, actual `bytes_network`, `bytes_adopted`, `bytes_local_sources`, and `retries`. One uninterrupted archive has `sessions: 1`; totals may span budgeted, resumed, or offline-assembly runs. |
| `downloader` | Tool name/version, `provider`, Python version, platform, plus the provider's client library versions (`huggingface_hub` for the Hugging Face plugin) — enough to reconstruct the download environment. |
| `mirrors_used` | Stable `local:<bundle_id>` references when registered sibling bundles supplied matching LFS blobs (empty list otherwise). Every local copy is independently re-hashed before attribution. |
| `signatures` | Upstream cryptographic signatures, when provided (null otherwise — Hugging Face repos generally ship none). |
| `access` | `{gated, notes}`. `gated` is the Hub gate status at archive time: `"auto"` (agree → instant access, contact info shared with the authors), `"manual"` (authors approve each request), or `false`. The gate agreement text lives in Hub repo settings, **not** in the repo tree, so it is not part of the snapshot — `notes` records that. Gates are enforced server-side on file downloads; an archive of a gated repo means the archiving account had accepted the terms. |
| `upstream_stats_at_archive` | Downloads/month and likes at archive time — a popularity snapshot for the historical record. |
| `upstream_tags` | Raw repo tags at archive time. |
| `subset` | Present when `archive --include` pinned a globbed subset. `{include, sidecars, sidecar_file_count, full_file_count, full_total_size_bytes, kept_file_count, kept_total_size_bytes, omitted_file_count, full_files[]}`. `full_files` is the complete upstream inventory (path, size, sha256, git_sha1, sorted by path) so the bundle states exactly what it left out. `inventory.files` is only the kept payload. `null` when the pin is the whole repo. |

## `licensing`

| Field | Meaning |
|---|---|
| `spdx_id` | License id from upstream repo metadata (`apache-2.0`). |
| `name` | Human name when the id is in the rights-flags table (`src/darsay/licensing.py`). |
| `license_files` | License/notice text files shipped upstream, as archived paths. The primary one is also copied to the bundle root as `LICENSE`. |
| `commercial_use`, `redistribution`, `modification`, `attribution_required`, `patent_grant` | Rights flags from the table. `null` = unknown license → review manually. Curator convenience, not legal advice. |
| `trademark_terms` | Trademark clauses worth knowing about (e.g. Apache-2.0 §6). |
| `needs_manual_review` | True when the license id is missing or not in the table, **or when the upstream repo is gated** — the gate terms are outside the snapshot, so the flags alone can't settle redistribution. |
| `notes` | Free text; auto-set when no license file ships upstream, the id is unrecognized, or the repo is gated. |

## `inventory`

| Field | Meaning |
|---|---|
| `file_count`, `total_size_bytes` | Payload totals. |
| `bundle_hash` | `{algorithm, value, covers}`. SHA-256 over the sorted `"<sha256>  <path>"` lines of the payload — one value that fingerprints the whole payload. Covers the payload root only; bundle-root metadata is mutable by design. |
| `layout` | `payload_root` (`model/` for model bundles, `data/` for dataset bundles — readers must take the root from here, writers from the registry) and the list of mutable bundle-root metadata files, including machine-local transfer/export/hydration state. |
| `files[]` | Per file: `path`, `size`, `sha256`, `blake3` (null if blake3 wasn't installed), `upstream_lfs_sha256` (LFS files), `upstream_git_sha1` (small git-blob files), `verified_against_upstream` (true/false/null = no upstream expectation). |

## `model_metadata` — model bundles only

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

## `dataset_metadata` — dataset bundles only

The declared/measured split is record-don't-fabricate applied to data:
upstream claims are recorded as *declared*; only facts established from the
payload itself are *measured*.

| Field | Meaning |
|---|---|
| `formats` | Files/bytes per extension, computed from the archived inventory (`{"parquet": {file_count, total_size_bytes}, ...}`), largest first. |
| `declared` | Upstream claims: `sources` (which of `dataset_infos.json` / the card YAML supplied them), `configs` (per config: `features` verbatim, `splits` with `num_examples`/`num_bytes`, `download_size`, `dataset_size`), `example_count_total` (sum of declared split counts; null when none declared). Null when upstream declares nothing. |
| `measured` | Row counts read from the payload's parquet metadata via pyarrow (optional extra `darsay[datasets]`): `status` (`measured`/`partial`/`skipped`), `method`, `row_counts` per file, `total_rows`, `errors` on partial reads. Skipped with a reason — never guessed — when pyarrow is absent or no parquet ships. |
| `task_categories`, `size_categories`, `languages` | From the dataset card, when declared. |

Worked example (`cornell-movie-review-data/rotten_tomatoes`, features elided):

```json
"dataset_metadata": {
  "formats": {
    "parquet": {"file_count": 3, "total_size_bytes": 881052},
    "md":      {"file_count": 1, "total_size_bytes": 7457},
    "(none)":  {"file_count": 1, "total_size_bytes": 1174}
  },
  "declared": {
    "sources": ["card"],
    "configs": {
      "default": {
        "features": [{"name": "text", "dtype": "string"}, {"name": "label", "...": "..."}],
        "splits": {
          "train":      {"num_examples": 8530, "num_bytes": 1074810},
          "validation": {"num_examples": 1066, "num_bytes": 134679},
          "test":       {"num_examples": 1066, "num_bytes": 135972}
        },
        "download_size": 487770,
        "dataset_size": 1345461
      }
    },
    "example_count_total": 10662
  },
  "measured": {
    "status": "measured",
    "method": "pyarrow parquet metadata",
    "row_counts": {"test.parquet": 1066, "train.parquet": 8530, "validation.parquet": 1066},
    "total_rows": 10662
  },
  "task_categories": ["text-classification"],
  "size_categories": ["1K<n<10K"],
  "languages": ["en"]
}
```

(Without pyarrow, `measured` records the degradation instead of guessing:
`{"status": "skipped", "reason": "pyarrow not installed (pip install
darsay[datasets])"}`. Here declared and measured agree at 10,662 —
when they disagree, both are kept; the manifest never averages claims.)

## `runtime` — model bundles only

| Field | Meaning |
|---|---|
| `supported_engines` | Derived from shipped formats only (safetensors → transformers, gguf → llama.cpp). |
| `estimated_min_ram_gb`, `estimated_min_vram_gb` | Estimates: weight bytes × 1.2. |
| `tested_hardware` | Measured runs, never estimates. `darsay run` appends/refreshes one entry per (host, device, engine) on each successful run: `{at, host, os, chip, device, engine, engine_versions, tokens_per_second, status, via}`. Curators may add entries by hand in the same shape. Null until the model has actually run somewhere. |
| `os_support`, `cuda_notes`, `rocm_notes`, `cpu_inference` | Coarse defaults; refine by hand. |
| `notes` | States the estimation method. |

## `validation`

| Field | Meaning |
|---|---|
| `checksum_verification` | Latest run: `at`, `status` (pass/fail), `files_checked`; at archive time also `upstream_mismatches`; on re-verification `missing`/`extra`/`mismatched` path lists and `bundle_hash_match`. |
| `completeness` | Result of the artifact-type rules: `status` (complete/incomplete), per-rule matches, `missing_required`, `missing_recommended`. |
| `smoke_tests.tokenizer` | Model bundles: encode/decode round-trip via `tokenizers` (or transformers fallback): status, engine, token count, `roundtrip_exact`. |
| `smoke_tests.inference` | Model bundles, opt-in (`smoke --inference`): greedy generation via transformers — status, prompt, output, new token count. |
| `smoke_tests.structure` | Dataset bundles: stdlib-only structural checks (parquet `PAR1` magic at head and tail, JSONL first-line parse, CSV/TSV dialect sniff) — status, `files_checked`, per-format counts, `failures`. |

Statuses: `pass` / `fail` / `skipped` (dependency or file missing) / `not-run`.

## `relationships`

Model bundles:

| Field | Meaning |
|---|---|
| `base_models` | All declared parents, from card `base_model` plus the Hub's `base_model:*` repo tags (merges have several; null when nothing is declared). |
| `base_model` | The primary (first) parent — convenience alias for `base_models[0]`. |
| `base_model_relation` | The model-tree edge label: `finetune`, `adapter`, `quantized`, or `merge`, from the card's `base_model_relation` or an unambiguous typed tag. Null when upstream doesn't label the edge (or labels conflict) — record, don't fabricate. Note the Hub has no relation type for alignment edits (e.g. abliteration); those surface only in tags and card text, and belong in `curation.md`. |
| `finetuned_from` | The parent **only when the declared relation is `finetune`**; null otherwise — a quantization or alignment edit is not a finetune. |
| `training_datasets` | Dataset ids the model card declares training on (`card.datasets`, normalized to a list; null when not declared) — the mirror of a dataset bundle's `models_trained_on`. |
| `quantized_versions`, `gguf_repos` | Downstream repos found at archive time (GGUFs are the `*gguf*` subset). |
| `finetunes_count`, `adapters_count` | Counts of downstream finetune/adapter repos. |
| `related_variants`, `successors` | **curator**. |

Dataset bundles:

| Field | Meaning |
|---|---|
| `source_datasets` | Upstream datasets this one was derived from, as the card declares them. |
| `models_trained_on` | Models on the Hub declaring training on this dataset (`dataset:<id>` filter) at archive time. |

Both:

| Field | Meaning |
|---|---|
| `query_limit` | Cap on the ecosystem queries (100). A count or list length equal to the cap means **at least** that many — renderers show `≥`. |
| `ecosystem_snapshot_as_of` | When the ecosystem queries ran. This section is a historical snapshot, not a live index. |

## `archive`

| Field | Meaning |
|---|---|
| `date_archived` | Creation time of the bundle. Also the fixed mtime used in exports. |
| `archived_by` | **curator** (or automation identity). |
| `location`, `host` | Absolute path and machine. Rewritten on import. |
| `storage_tier`, `backup_status`, `replicas` | Where copies live (`local-disk` / `none` / `[]` until a backup workflow records otherwise). |
| `last_integrity_check`, `last_accessed` | Maintained by `verify`, `smoke`, `hydrate` / `run`, import. `info` is read-only and does not rewrite the manifest. |
| `imported` | Present on imported bundles: `at`, `from_file`, `file_sha256`, `mvb_format_version`. |

Export events are logged in the sibling file `exports.json`
(`{"exports": [{at, file, sha256, size_bytes, mvb_format_version, written_by}]}`),
not in the manifest — see [MVB-FORMAT.md](MVB-FORMAT.md) for why. Hydration state and
run history live in the sibling file `hydration.json` for the same reason:
both are volatile machine-local state, excluded from exports — see
[HYDRATION.md](HYDRATION.md). The resumable acquisition ledger
`transfer.json` and transient `transfer.lock` are also bundle-root,
machine-local, payload-excluded, and export-excluded. `transfer.json` remains
as detailed history after registration but is disposable: payload bytes and
the manifest are archival authority. See [INCREMENTAL.md](INCREMENTAL.md).

## `security`

| Field | Meaning |
|---|---|
| `integrity_status` | `verified-against-upstream` (archive-time cross-check passed) → `compromised` if a later `verify` finds changes → back to `verified-against-upstream` when a later `verify` passes. `upstream-mismatch` if the archive-time cross-check itself failed (not healed by a later payload-vs-manifest pass). |
| `unexpected_changes[]` | Append-only log of `{detected_at, type: modified|missing|extra, path}` from failed verifications. |
| `trust_level` | `unreviewed` until a human reviews; then curator-set (e.g. `reviewed`, `trusted`). |
| `reviewed_by`, `review_notes` | **curator**. |

## `curation`

Structured mirror of the curator's notes: `historical_significance`,
`major_capabilities`, `known_limitations`, `successor_models`,
`personal_notes` (all **curator**), plus `curation_file` pointing at
`curation.md` — the free-form file that `darsay regen` folds into the
bundle README. Prose belongs in `curation.md`; use the structured fields when
downstream tooling needs to query them.

## Versioning

`schema_version` is a property of **this file's shape**, not of the tool that
last touched it. Tool version (`darsay.__version__`) moves independently.

1.x is a closed major with an open minor:

- **Major** changes only on breaking layout. A 1.x tool refuses
  `schema_version` major `> 1` (`load_manifest`, and `import` on the
  `.mvb.json` marker before unpacking). A 2.x bundle is unsupported, not
  silently misread. Mixed 1.x minors may coexist in one vault.
- **Minor / patch** are additive. Readers **ignore unknown fields**.
  Missing additive fields mean `null`. Writers **preserve** unknown
  top-level keys on round-trip (`verify`, `run`, `regen`).
- **The version describes this file**, not the tool that last touched it.
  Writes do not stamp the tool's current schema onto the record.
- **1.x will not** rename or remove a field, change `payload_root` for
  `model` or `dataset`, change `bundle_hash` or what it covers, or rewrite
  payload.
- **New artifact types** (GGUF packs, papers) are a new `artifact_type`
  plus payload root — not a 2.0. Readers take `inventory.layout.payload_root`
  from the record.

2.0, when it exists, is a second reader dispatched on `schema_version`
major (and `kind`). It lives next to 1.x in the same vault. Payload stays
byte-immutable; a migrate, if ever needed, rewrites only the record.

---

[Documentation index](README.md)

<p align="center">
  <a href="GETTING-STARTED.md">Start here</a> ·
  <a href="CONCEPTS.md">Concepts</a> ·
  <a href="../examples/README.md">Examples</a> ·
  <a href="README.md">All docs</a> ·
  <a href="../README.md">README</a>
</p>

# manifest.json — schema reference (v2.1.0)

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
| `schema_version` | Version of this schema (`"2.1.0"`). Major changes only on breaking layout. |
| `kind` | Always `"darsay.bundle"`. A file with the field missing is read as `"darsay.bundle"`. Any other value is a load error. |
| `artifact_type` | Registry key driving completeness rules and the payload root (`"model"` or `"dataset"`; future: GGUF packs, papers — see `src/darsay/schema.py`). |
| `bundle_id` | `<bundle directory name>@<first 12 of pinned revision>`, e.g. `qwen--qwen3-0.6b@c1899de289a0`. Hugging Face dataset bundles take a `datasets--` prefix (`datasets--saidutta69--fable-5-premium@684cb1f849fe`) since model and dataset namespaces can collide on that host. Other providers include their id in the directory name. Stable, deterministic, unique per (source, revision). |

## `identity`

| Field | Meaning |
|---|---|
| `model_name` | Repo name as published (`Qwen3-0.6B`), for datasets too — the field name is kept stable across types. |
| `publisher` | Repo owner (`Qwen`). |
| `family` | The product family, read from the name by the documented grammar (`Qwen`, `Kimi`, `DeepSeek-Coder`); null when the name has no alphabetic lead. The *architecture* family (`config.json` `model_type`) lives in `model_metadata.model_type`. |
| `generation` | The generation token after the family (`3.8`, `K3`, `V3.2`); null when the name carries none. Orders numerically within a family. |
| `member` | What remains of the name after family, generation, variants, and formats (`2.4T-A95B`, `27B`, `Flash-Next`); null for a family's flagship (`Kimi-K3`). |
| `variants` | Closed vocabulary read from the name: `base`, `instruct`, `chat`, `thinking`, `abliterated`, `uncensored`, `distill`. |
| `formats` | Packaging tokens read from the name: `gguf`, `fp8`, `awq`, `gptq`, `int4`, `mlx`, … |
| `size` | `{total, active}` parameter counts read from the name (`480B-A35B` → 480e9 / 35e9; `8x7B` → 56e9 / 7e9); null when the name has no size token. A claim of the *name*, distinct from `model_metadata.parameter_count`, which is counted from headers. |
| `read_from` | Always `"name"` — the provenance of `family` … `size`. |
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
| `subset` | Present when the pin is not the whole repo — an explicit `archive --include`, or the default negatives selection. `{include, sidecars, sidecar_file_count, full_file_count, full_total_size_bytes, kept_file_count, kept_total_size_bytes, omitted_file_count, full_files[]}`. `full_files` is the complete upstream inventory (path, size, sha256, git_sha1, sorted by path) so the bundle states exactly what it left out. `inventory.files` is only the kept payload. `null` when the pin is the whole repo (`--full`, datasets, or nothing skippable). |
| `subset.policy` | `"negatives"` when the selection was made by the default classification — the negative set ([Quantization §4](QUANTIZATION.md#4-mechanics)); absent for explicit `--include` subsets. |
| `subset.classification` | The recorded rationale of a policy selection: `classifier.darsay` (the version that classified), the read receipt (`requests`, `bytes_fetched`, `caps` — the classification's `query_limit`s), and one row per weight set (`{name, kind, verdict, rule, action, file_count, bytes, reason}`; `verdict` ∈ `negative`, `print`, `support`, `unknown`). Recorded facts about a decision the tool actually made, not curator claims. |

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
| `parameter_count`, `parameters_by_dtype`, `weight_shards` | Counted from safetensors headers. Null for `.bin`/`.gguf`-only payloads, and when any shard's header cannot be read (never a partial count). |
| `architecture`, `model_type` | From `config.json` (`Qwen3ForCausalLM`, `qwen3`). |
| `context_length` | `max_position_embeddings`. |
| `precision` | The release precision label — `BF16`, `FP8`, `MXFP4`, `AWQ INT4`, `Q4_K_M` — from `config.json`'s `quantization_config` (wherever a multimodal config nests it), else the dominant tensor dtype, else a GGUF-only payload's file name ([Quantization §2](QUANTIZATION.md#2-precision-and-the-one-number-that-explains-a-size)). |
| `precision_detail` | The recipe behind the label when there is one (`compressed-tensors · mxfp4-pack-quantized · 4-bit · group 32`), else null. |
| `bytes_per_param` | Measured: weight bytes in the payload over `parameter_count`. Null when either is unknown. |
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

## `lineage`

Where this work sits: its parents as upstream declares them, and the
descendants that existed when it was archived. The name-derived half of
lineage — family, generation, member — lives in `identity`
([Concepts → lineage](CONCEPTS.md#lineage)).

| Field | Meaning |
|---|---|
| `parents` | Edges to the works this one was made from, each `{source, relation, declared_by}`. `source` is a canonical ref (`huggingface:Qwen/Qwen3.8-27B`, `huggingface:datasets/owner/name`). `relation` is the label upstream gave the edge — `finetune`, `adapter`, `merge`, `quantized` (the Hub's model-tree relations), `trained_on` (from the card's `datasets`), `derived` (a dataset's `source_datasets`) — or null when a parent is declared without a label. `declared_by` is `card` or `tag`. Null when nothing is declared. The Hub has no relation for alignment edits (abliteration); those surface in `identity.variants` (read from the name) and in `curation.md`. |
| `descendants` | An archive-time snapshot of what pointed back at this work. Models: `quantized` (repo ids declaring `base_model:quantized`), `gguf` (the `*gguf*` subset of those), `finetunes_count`, `adapters_count`. Datasets: `models_trained_on` (repo ids declaring `dataset:<id>`). A null list means the query failed; a length equal to `query_limit` means **at least** that many — renderers show `≥`. |
| `successors`, `related` | **curator** — the next generation, and kin the name grammar cannot see. |
| `as_of` | When the descendant queries ran. This section is a historical snapshot, not a live index. |
| `query_limit` | Cap on the descendant queries (100). |

## `archive`

| Field | Meaning |
|---|---|
| `date_archived` | Creation time of the bundle. Also the fixed mtime used in exports. |
| `archived_by` | **curator** (or automation identity). |
| `location`, `host` | Absolute path and machine. Rewritten on import, by `darsay mv`, and on both sides of `darsay cp`. |
| `storage_tier` | `local-disk` until a curator or workflow records otherwise. |
| `backup_status`, `replicas` | `none` / `[]` until `darsay cp` records a verified copy; then `backup_status` is `replicated` and `replicas` lists the *other* known locations of this bundle as `{at, location, host}`, written on both sides of the copy and keyed by location (a `--force` refresh updates the timestamp rather than adding a row). An rsync leaves no entry — it is a first-class copy darsay never sees happen. Entry shape and `replicated` since 1.8.0. |
| `last_integrity_check`, `last_accessed` | Maintained by `verify`, `smoke`, `hydrate` / `run`, import. `info` is read-only and does not rewrite the manifest. |
| `imported` | Present on imported bundles: `at`, `from_file`, `file_sha256`, `mvb_format_version`. |
| `moves` | Present once `darsay mv` has relocated the bundle: a list, oldest first, of `{at, from_location, method}` where `method` is `rename` (same filesystem; bytes untouched) or `copy` (re-hashed at the destination before the source was removed). An rsync leaves no entry — it is a first-class copy that darsay never sees happen. Since 1.8.0. |
| `migrations` | Present once `darsay migrate` (or `import` of an older export) has brought the record forward: a list, oldest first, of `{at, from_schema, to_schema, darsay}`. The payload never changes across a migration; what changed is the shape its facts are written in — see [Migration](#migration). Since 2.1.0. |

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

2.x is a closed major with an open minor:

- **Major** changes only on breaking layout. This tool reads
  `schema_version` major `2` only (`load_manifest`, and `import` on the
  `.mvb.json` marker before unpacking): a newer major is unsupported, not
  silently misread. An older major is refused by every verb but `list`
  and `migrate`, and the refusal names the command: `darsay migrate`
  re-reads the record under the current model and writes it in this
  shape ([Migration](#migration)); `import` does the same as an older
  export lands. Readers never read around an older record. Mixed 2.x
  minors may coexist in one vault.
- **Minor / patch** are additive. Readers **ignore unknown fields**.
  Missing additive fields mean `null` (`kind` missing is read as
  `"darsay.bundle"`). Writers **preserve** unknown top-level keys on
  round-trip (`verify`, `run`, `regen`, `migrate`).
- **The version describes this file**, not the tool that last touched it.
  Writes do not stamp the tool's current schema onto the record — except
  `migrate`, whose purpose is exactly that, and which says so in
  `archive.migrations`.
- **2.x will not** rename or remove a field, change `payload_root` for
  `model` or `dataset`, change `bundle_hash` or what it covers, or rewrite
  payload.
- **New artifact types** (GGUF packs, papers) are a new `artifact_type`
  plus payload root — not a 3.0. Readers take `inventory.layout.payload_root`
  from the record.

What 2.0.0 changed against 1.x: `identity` gained the name-derived
`family` … `size` and dropped `version` (the generation) and the
architecture-as-family reading; `relationships` became `lineage` with
typed, provenance-carrying `parents`; `model_metadata.precision` became
the release-precision label with `precision_detail` and
`bytes_per_param` beside it; `source.subset.policy` reads `"negatives"`
and verdicts read `negative`. Payload stays byte-immutable across majors.
2.1.0 added `archive.migrations`.

## Migration

A record written under an earlier major is a complete set of facts about
a payload that has not changed. `darsay migrate BUNDLE` reads those facts
back under the current model and writes them in the current shape —
offline, from the record and the payload, touching no payload byte and
re-hashing nothing (`verify` is the next line it prints). `-n` prints,
per section, what the record will say and where that came from; `--all`
walks the vault; `--json` is for scripts. `darsay list` marks such
bundles `migrate`, `darsay doctor` reports them (`bundle.manifest`,
warning), and `darsay import` migrates an older export as it lands.

For a 1.x record, section by section:

| Section | What `migrate` does | From |
|---|---|---|
| `identity` | re-reads `family`, `generation`, `member`, `variants`, `formats`, `size` (`read_from: "name"`); keeps `model_name`, `publisher`, `release_date`, `aliases`; drops `version` (now `generation`) and the config-read `family` (the architecture — still `model_metadata.model_type`) | the name |
| `model_metadata` | re-derived whole, so `precision` is the release-precision label with `precision_detail` and `bytes_per_param` beside it; `languages` and the curator's `training_cutoff` are kept | `config.json`, the weight headers, the file names |
| `lineage` | translated from `relationships`: `parents` with each edge's relation and provenance, `descendants` / `as_of` / `query_limit` carried as the archive-time snapshot they are, curator `successors` / `related` kept | the archive-time card and tags when `transfer.json` travelled with the bundle, else the record and its `source.upstream_tags` — a parent both named is then recorded as declared by the tag, the declaration the record itself holds |
| `source.subset` | `policy` reads `negatives`, verdicts read `negative` — the classification itself is the recorded one | the record |
| `inventory` | carried as recorded (it is the hash record); `layout` filled from the registry for records that predate it | the record |
| `source` | carried as recorded; `provider` and `address` spelled out from `origin` and `repo_id` for records that predate them (1.5.0) | the record |
| everything else | `licensing`, `runtime`, `validation`, `archive`, `security`, `curation`, `dataset_metadata`: carried as recorded; unknown top-level keys round-trip | the record |

Then `archive.migrations` gains `{at, from_schema, to_schema, darsay}`,
and `README.md` / `VERIFICATION.md` are regenerated (`curation.md` is
never touched). Nothing is fetched: descendants are a snapshot of when
the bundle was archived and stay that snapshot. A record of a *newer*
major is refused — a record moves forward, not back.

The reading of an older major lives in one module (`src/darsay/migrate.py`);
every other reader reads the current major only. When 3.0 comes, that
module gains a 2.x reading and 1.x records take both steps.

---

[Documentation index](README.md)

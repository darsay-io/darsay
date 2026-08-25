> [Documentation](README.md) · [Project README](../README.md)

# Dataset bundles — the vault's second artifact type

Design for archiving Hugging Face datasets with the same bundling mechanism
as models. Status: **implemented** (schema 1.2.0, modelvault 0.4.0, 2026-08).
Case study: `saidutta69/fable-5-premium` (2026-08).

## 1. Why

Models are functions of data: a vault holding a model but not what it was
trained on preserves the sculpture and discards the quarry. The graph is
concrete — `Danielbrdz/Barcenas-Qwen3.8-27B-Fable`, a finetune of the
archived-candidate Qwen3.8-27B, declares training on `fable-5-premium`;
without dataset bundles that edge dangles out of the museum. Datasets are
also *more* endangered than weights: they get DMCA'd, gated retroactively,
and quietly rewritten, and unlike popular weights they are rarely mirrored.
The `ARTIFACT_TYPES` registry (`schema.py`) was built for this day.

## 2. The conceptual contract — what a bundle is, type-free

A bundle is an **immutable payload + a manifest of recorded facts + derived
views + one curator file**. Every invariant is already artifact-agnostic:
payload immutability, export determinism, record-don't-fabricate,
verify-before-register, generated-vs-hand-edited, registry extensibility,
disposable hydration. None mentions "model". A user who can archive a model
learns datasets from one sentence:

> *Datasets are addressed as `huggingface:datasets/owner/name` (or the
> Hugging Face shorthand `datasets/owner/name`) and their payload lives
> in `data/`; everything else is identical.*

That sentence is the design's budget. Anything exceeding it is rejected.

## 3. The whole delta — five registry-scoped properties

Everything that varies by artifact type becomes a property of the
`ARTIFACT_TYPES` entry; nothing varies anywhere else:

| Property | `model` | `dataset` |
|---|---|---|
| payload root | `model/` | `data/` |
| completeness rules | config/weights/tokenizer | data files (parquet/jsonl/csv/arrow/…); card, license, `dataset_infos.json` recommended |
| metadata extractor | `model_metadata` (params, arch, tokenizer) | `dataset_metadata` (formats, configs/splits, examples) |
| ecosystem snapshot | quantized/finetunes/adapters of it | models trained **on** it |
| engines | transformers, llama-cpp | none (hydration degrades gracefully) |

## 4. Addressing: one source-ref grammar, zero new verbs

No new verbs, no `--type` flag. Every source-taking command accepts a
provider-qualified ref; Hugging Face shorthand and paste-from-browser URLs
still work:

    modelvault estimate huggingface:datasets/saidutta69/fable-5-premium
    modelvault archive  datasets/saidutta69/fable-5-premium
    modelvault archive  https://huggingface.co/datasets/saidutta69/fable-5-premium

`parse_source` dispatches on the provider; the Hugging Face plugin maps
`owner/name` → model and `datasets/owner/name` → dataset. Bundle-path
commands (`verify`, `info`, `export`, …) need nothing: they dispatch on the
manifest's `artifact_type`, which is why that field has existed since
schema 1.0. See [SOURCES.md](SOURCES.md).

## 5. Naming and layout

    vault/datasets--saidutta69--fable-5-premium/684cb1f849fe/
    ├── data/            immutable payload (the manifest's layout.payload_root)
    ├── manifest.json    artifact_type: "dataset"
    └── ... identical bundle anatomy ...

- **Directory & bundle id** take a `datasets--` prefix
  (`datasets--saidutta69--fable-5-premium@684cb1f849fe`): model and dataset
  namespaces can collide on the Hub, the prefix mirrors the Hub's URL
  grammar, and the two-level `*/*/manifest.json` layout that `list`/`envs`
  glob is preserved. Model bundles are unchanged.
- **Payload root is `data/`**, not a reused `model/`. The manifest has
  recorded `inventory.layout.payload_root` since 1.0 — the abstraction
  already lives in the data model; implementation finally honors it. Rule:
  writers take the root from the registry, readers from the manifest —
  never a literal. Audit (2026-08): ~10 one-line hardcoded sites across
  archiver/verify/export/smoke/licensing/estimate; `hydrate`'s
  `model/*`-prefixed engine globs stay as-is (they simply never match a
  dataset inventory, which is the correct outcome).

## 6. The dataset manifest

Universal sections unchanged: `identity`, `source`, `licensing`,
`inventory`, `validation`, `relationships`, `archive`, `security`,
`curation`. Per-type: `model_metadata` and `runtime` are model-only;
dataset bundles instead carry:

- `dataset_metadata` — formats (files/bytes per extension, from the
  inventory), configs and splits, features schema and per-split example
  counts read from `dataset_infos.json` / card YAML and recorded as
  **declared** (upstream claims), task/size categories and languages from
  the card. **Measured** row counts only when pyarrow is present (optional
  extra, else `skipped` with reason) — the declared/measured split is
  record-don't-fabricate applied to data.
- `relationships` — `models_trained_on` (Hub `dataset:<id>` filter,
  `query_limit` recorded) and `source_datasets` from the card. The model
  side gains the mirror edge: `relationships.training_datasets` from card
  data, making the vault graph two-sided.

Schema bump: **1.2.0** (additive — new artifact type, new per-type section,
new model relationship field; existing consumers unaffected). MVB format
unchanged (the marker already carries bundle id + hash; `artifact_type`
rides in the embedded manifest) — doc note only.

## 7. Per-command behavior

`estimate` prices a dataset identically (exact sizes, completeness, disk
verdict), printing a formats breakdown where a model shows parameters.
`archive` cross-checks LFS/git hashes, snapshots `models_trained_on`,
writes the same reports. `verify`/`export`/`import` are payload-root
parameterization away from working verbatim. `smoke` gets stdlib-only
structural checks: parquet magic bytes (`PAR1` head and tail), first-line
JSONL parse, CSV header sniff — real integrity signal with zero heavy
dependencies. `regen`/`curation.md` unchanged. `hydrate`/`run` exit with
the existing "no known engine" message; a future read-only "peek" engine
(pyarrow env that prints sample records offline) is a natural `ENGINES`
entry, not designed here.

## 8. Deliberately out of scope

- **Datasets-server statistics** (row counts, size estimates from the Hub's
  viewer service): derived by an external service, not established from
  payload or repo — record nothing, or record explicitly attributed claims.
- **Croissant metadata snapshot** (`/api/datasets/<id>/croissant`): cheap
  and standards-based; a candidate `source` addition later, not core.
- **Non-Hugging-Face origins** (Zenodo, academic torrents, ModelScope):
  the provider registry in [SOURCES.md](SOURCES.md) is the slot; each host
  is a `SourceProvider`, not a new command.

## 9. Implementation plan

1. Payload-root parameterization (registry + manifest-honoring readers) —
   touches the ~10 audited sites; model bundles byte-identical before/after.
2. Source-ref plumbing through `estimate`/`archive` (Hugging Face provider
   uses `dataset_info` / `repo_type="dataset"`).
3. `dataset` registry entry, `dataset_metadata` extractor, relationships
   both directions; schema 1.2.0; update MANIFEST.md and the README.
4. Dataset smoke checks; validate end-to-end on a small public dataset,
   then archive `fable-5-premium` (2.3 GiB) as the reference.

All four steps are implemented. End-to-end validation ran on
`datasets/cornell-movie-review-data/rotten_tomatoes` (869 KiB, 3 parquet
splits): archive → verify → export ×2 (byte-identical tars) → import →
smoke → regen, with model bundles proven byte-stable against a pre-change
baseline; pyarrow-measured rows (10,662) matched the declared count exactly.
Archiving `fable-5-premium` as the reference dataset bundle awaits the
curator's download go-ahead.

## Case study: saidutta69/fable-5-premium

2.34 GB, 18 files: parquet + JSONL in two chat schemas (`agent_traces/`,
`openai_chat/`) × train/validation/test, `dataset_infos.json` with
features and split sizes, MIT, `10K<n<100K` examples declared. Everything
the manifest wants is already in the repo or one API call away — and four
models on the Hub cite it as training data, two of them descendants of
Qwen3.8-27B. Archive the model, its FP8 satellite, and this dataset, and
the vault holds a closed, cross-referenced exhibit: the artifact, its
efficient print, and its food.

---

[Documentation index](README.md)

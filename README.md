# model-vault

Tools for downloading and archiving **full model ecosystems** as reproducible,
auditable bundles — museum-grade curation that stays directly usable.

`modelvault` pulls a model or dataset from a source (Hugging Face today) at a
pinned revision, hashes every file (SHA-256, plus BLAKE3 when available),
cross-checks each file against upstream checksums, captures the license
verbatim with rights flags, extracts model metadata straight from the payload
(parameter counts are read from safetensors headers — no torch needed),
snapshots the downstream ecosystem (quantizations, GGUFs, finetunes), and
writes it all into a bundle that any Hugging Face-compatible loader can use
as-is. When you want to hear the archived model speak, `modelvault run <bundle>`
takes it from cold storage to a local inference — building the environment for
you — in one command. Large archives are incremental by
default: budgets and Ctrl-C leave durable Range partials, copied partial
bundles resume on another machine, and friends can coordinate byte-balanced
transfer orders and assemble their work offline. Before committing tens of
gigabytes, `modelvault estimate` prices a source from upstream metadata
alone — exact sizes, parameter counts, disk headroom, and its quantized
ecosystem — without downloading a byte.

## Install

Requires Python 3.10+. The package is pure Python — one wheel for every OS.
Isolated CLI tools (`pipx`, `uvx`) are the intended way to run a release;
see [docs/DISTRIBUTION.md](docs/DISTRIBUTION.md).

```bash
# from a GitHub tag (after the repo is on GitHub)
pipx install git+https://github.com/archive-dawn/modelvault@v0.5.0
uvx --from git+https://github.com/archive-dawn/modelvault@v0.5.0 modelvault --help

# from a downloaded GitHub Release wheel
pipx install ./modelvault-0.5.0-py3-none-any.whl

# development checkout
python3 -m venv .venv
.venv/bin/pip install -e .                 # core: huggingface_hub only
.venv/bin/pip install -e ".[fast-hash,smoke]"   # + blake3, tokenizers
.venv/bin/pip install -e ".[inference]"    # + transformers/torch for inference smoke test
.venv/bin/pip install -e ".[datasets]"     # + pyarrow for measured dataset row counts
```

The extras only serve the in-process smoke tests; `modelvault run` needs none
of them — hydration builds its own isolated env per engine (see below).

## Usage

```bash
modelvault estimate Qwen/Qwen3.8-27B --variants   # preflight: size, params, disk, quantized ecosystem — no download
modelvault estimate unsloth/Qwen3.8-27B-GGUF --include '*Q4_K_M*'   # price one quant of a pack repo
modelvault estimate datasets/saidutta69/fable-5-premium   # datasets use the Hub's own address grammar
modelvault archive Qwen/Qwen3-0.6B          # download + hash + manifest + reports
modelvault archive datasets/saidutta69/fable-5-premium    # dataset bundle: payload under data/
modelvault archive Qwen/Qwen3.8-27B --max-gb 10           # pause cleanly; rerun to resume
modelvault archive Qwen/Qwen3.8-27B --dry-run             # verified/partial/missing plan
modelvault archive Qwen/Qwen3.8-27B --shard 1/3 --max-gb 20  # participant 1 of 3
modelvault --vault ./combined assemble /usb/alice/<bundle> /usb/bob/<bundle>
modelvault verify vault/qwen--qwen3-0.6b/<rev>    # re-hash, detect tampering
modelvault smoke  vault/qwen--qwen3-0.6b/<rev> [--inference]
modelvault list                             # inventory of the whole vault
modelvault info   vault/qwen--qwen3-0.6b/<rev>
modelvault regen  vault/qwen--qwen3-0.6b/<rev>    # rebuild README after editing curation.md
modelvault export vault/qwen--qwen3-0.6b/<rev> -o /backups   # single-file .mvb.tar
modelvault import /backups/qwen--qwen3-0.6b@<rev>.mvb.tar    # unpack + verify + register

modelvault run     vault/qwen--qwen3-0.6b/<rev> "Say hello"  # hydrate if needed + offline inference
modelvault hydrate vault/qwen--qwen3-0.6b/<rev> [--dry-run]  # just build/reuse the runnable env
modelvault envs [--prune]                   # list shared runtime envs / delete unreferenced ones
modelvault dehydrate vault/qwen--qwen3-0.6b/<rev>            # drop a bundle's hydration record
```

The vault root defaults to `./vault` (override with `--vault` or
`$MODELVAULT_HOME`). Bundles are gitignored — they are large binary payloads
that live on disk or in your backup tier, not in this repo. Source refs are
provider-qualified — `huggingface:Qwen/Qwen3-0.6B`,
`huggingface:datasets/owner/name`. Unprefixed `owner/name` /
`datasets/owner/name` and huggingface.co URLs are Hugging Face shorthand so
existing commands keep working. Bundle-path commands need nothing, they
dispatch on the manifest's `artifact_type`. Adding another host is a source
provider, not a new CLI: [docs/SOURCES.md](docs/SOURCES.md).

## Bundle layout

```
vault/qwen--qwen3-0.6b/<revision12>/
├── model/              # immutable payload: pristine snapshot of the upstream repo
├── manifest.json       # machine-readable record (the source of truth)
├── README.md           # human-readable summary, generated from the manifest
├── VERIFICATION.md     # latest verification report
├── verification.json   # verification history (last 50 runs)
├── curation.md         # curator's notes — the only hand-edited file
├── exports.json        # log of single-file exports (appears after first export)
├── hydration.json      # runnable-env record + run history (appears after first hydrate)
├── transfer.json       # disposable resumable-transfer ledger + detailed history
├── transfer.lock       # transient per-bundle writer lock (only during archive/assemble)
└── LICENSE             # upstream license text, surfaced at the root
```

The payload under `model/` is **immutable after archiving**; the bundle hash
covers it alone. Metadata at the bundle root is mutable by design, so
verification runs, curation notes, and access timestamps never disturb the
archived artifact. To use the model, point `transformers` (or any HF-compatible
loader) at `<bundle>/model` — no unpacking or conversion needed.

## Incremental, relocatable, and cooperative transfers

The first `archive` run pins one immutable commit and writes its expected file
set to `transfer.json`. Every later run reconciles that set against local
bytes, trusts already verified files, hashes and adopts unrecorded complete
files, resumes bundle-local `.incomplete` files with HTTP Range, and only
registers `manifest.json` after every expected file is verified. `--max-gb`,
`--max-bytes`, and `--max-minutes` stop cleanly with exit code 10; `--rehash`
rechecks trusted ledger entries; `--jobs` controls the small-file pool.

Partial bundles are self-contained and relocatable. Copy the entire
`<repo-slug>/<revision12>/` directory—including the payload `.cache`—under a
different vault and rerun the same `archive` command. The pin is unchanged,
completed files are adopted, the longest Range partial continues, and an
inherited lock is safely recognized as a copied lock.

For cooperative acquisition, collaborators use `--shard N/T`: `1/3`, `2/3`,
and `3/3` deterministically prioritize different byte-balanced whole-file
lanes, but each still proceeds through all lanes and can finish the identical
bundle alone. `modelvault --vault DEST assemble PARTIAL...` validates that all
inputs have the same full pin and expected inventory, clone-copies complete
files, re-hashes them, and keeps the longest matching partial—all offline.
File-granular coordination works especially well for sharded weights; one
monolithic weight file cannot be divided into independent starting regions.
Full design, ledger shape, and failure semantics:
[docs/INCREMENTAL.md](docs/INCREMENTAL.md).

## What the manifest records

| Section | Contents |
|---|---|
| `identity` | model name, family, publisher, version, release date, bundle id |
| `source` | origin, repo id, pinned commit, transfer session/byte accounting, downloader tool versions, local mirrors, signatures, upstream popularity + tags at archive time |
| `licensing` | SPDX id, license files, commercial-use / redistribution / modification / attribution flags, patent grant, trademark terms, manual-review flag |
| `inventory` | per-file size + SHA-256 (+BLAKE3), upstream LFS/git checksums with match status, deterministic bundle hash, expected layout |
| `model_metadata` | parameter count (by dtype, read from safetensors headers), architecture, context length, precision/quantization, tokenizer class + vocab + special tokens + chat template, languages, training cutoff |
| `runtime` | supported engines (from shipped formats), estimated min RAM/VRAM, tested hardware (measured entries written by `modelvault run`), OS support, CUDA/ROCm notes, CPU inference |
| `validation` | checksum verification, completeness against artifact-type rules, tokenizer + inference smoke tests |
| `relationships` | base/parent model, finetuned-from, known quantizations + GGUF repos + finetune counts (snapshot at archive time) |
| `archive` | date archived, location, host, storage tier, backup status, replicas, last integrity check, last access |
| `security` | integrity status, unexpected-change flags (modified/missing/extra files), trust level, review notes |
| `curation` | historical significance, capabilities, limitations, successors, personal notes (via `curation.md`) |

`schema_version` is recorded in every manifest; `verification.json` keeps an
auditable history of every integrity check. Full field-by-field reference:
[docs/MANIFEST.md](docs/MANIFEST.md).

## Estimating before you archive

A 27B model is a 50+ GB commitment; `modelvault estimate` prices it first.
It is a read-only preflight against the Hub API — nothing downloaded, nothing
written: the pinned revision's exact file inventory, parameter counts by dtype
(published in the Hub's safetensors metadata), the engines the payload will
support, a completeness pre-check against the artifact-type rules, and a disk
verdict for the target vault. The command exits non-zero when free space is
insufficient, so it doubles as a guard in scripts. Upstream numbers are
reported as facts; derived figures (min RAM, download scratch) are labeled
estimates — the manifest's record-don't-fabricate rule applies even to a
command that writes nothing.

```
$ modelvault estimate Qwen/Qwen3.8-27B

Qwen/Qwen3.8-27B @ main -> 1d4bf0f2ff60
  image-text-to-text | license apache-2.0
  parameters:   27.78B BF16  [upstream safetensors metadata]
  payload:      32 files, 51.8 GiB
                weights 51.7 GiB in 18 files (largest 3.7 GiB: model-00004-of-00018.safetensors)
                support 22.0 MiB in 14 files
  engines:      transformers
  completeness: complete
  estimated:    download scratch +3.7 GiB (largest file in flight), min RAM/VRAM 62.1 GB (weight bytes x1.2)
  bundle:       vault/qwen--qwen3.8-27b/1d4bf0f2ff60  (new)
  disk:         needs ~55.5 GiB, free 1022.6 GiB — OK

To archive: modelvault archive Qwen/Qwen3.8-27B
```

`--variants` additionally lists the model's quantized ecosystem (via the
Hub's `base_model:quantized` relation), sized for the most-downloaded repos
with the query caps recorded in the output. `--include GLOB` prices a subset
of a repo — e.g. the single Q4_K_M file (15.3 GiB) inside a 439.7 GiB GGUF
pack — and warns that `archive` itself has no subset mode yet. `--json`
emits the full machine-readable estimate.

## Quantized models: fidelity first

When a model ships in many precisions, the canonical bundle is the
**highest-fidelity upstream release**, archived byte-exact — the master is
the negative, quants are prints. Published quants that matter historically
(the official FP8, the community-standard GGUF) are archived as ordinary
**satellite bundles**: most are calibration-based (AWQ/GPTQ, imatrix GGUFs,
curated FP8 layer maps) and can never be regenerated bit-exact from the
master, so if it matters what people actually ran, the bytes themselves must
be kept. Everything else — running the model smaller on your own hardware —
is disposable hydration-time derivation, never archival. The full policy,
with the Qwen3.8-27B case study and the proposed `archive --include` /
`hydrate --quantize` mechanics: [docs/QUANTIZATION.md](docs/QUANTIZATION.md).

## Dataset bundles

Datasets are the vault's second artifact type — models are functions of data,
and datasets are *more* endangered than weights (DMCA'd, gated retroactively,
quietly rewritten, rarely mirrored). One sentence covers the difference:
datasets are addressed as `datasets/owner/name` and their payload lives in
`data/`; everything else is identical. Bundle directories take a `datasets--`
prefix, the manifest carries `dataset_metadata` (formats from the inventory;
configs, splits, and example counts recorded as **declared** upstream claims,
with **measured** parquet row counts only when pyarrow is installed —
`modelvault[datasets]`) and a two-sided relationship graph: dataset bundles
record `models_trained_on`, model bundles record `training_datasets`.
`smoke` runs stdlib-only structural checks (parquet magic bytes, JSONL
first-line parse, CSV dialect sniff); `hydrate`/`run` don't apply — a dataset
bundle has no engine, and any reader can be pointed at `data/` directly.

`estimate` prices a dataset exactly like a model, printing a formats
breakdown where a model shows parameters:

```
$ modelvault estimate datasets/saidutta69/fable-5-premium

datasets/saidutta69/fable-5-premium @ main -> 684cb1f849fe
  license mit
  formats:      jsonl 1.5 GiB in 6, parquet 702.2 MiB in 6, png 49.0 KiB in 1, py 15.3 KiB in 1, json 3.9 KiB in 2, md 3.0 KiB in 1, (none) 2.8 KiB in 1
  payload:      18 files, 2.2 GiB
                data 2.2 GiB in 14 files (largest 689.4 MiB: openai_chat/train.jsonl)
                support 70.0 KiB in 4 files
  engines:      none (dataset bundle — hydrate/run not applicable)
  completeness: complete
  estimated:    download scratch +689.4 MiB (largest file in flight)
  bundle:       vault/datasets--saidutta69--fable-5-premium/684cb1f849fe  (new)
  disk:         needs ~2.8 GiB, free 997.5 GiB — OK

To archive: modelvault archive datasets/saidutta69/fable-5-premium
```

Using an archived dataset needs no unpacking or conversion — the payload is
the upstream repo's own files:

```python
import pyarrow.parquet as pq   # or pandas, polars, datasets — plain files either way

path = "vault/datasets--cornell-movie-review-data--rotten_tomatoes/aa13bc287fa6/data"
table = pq.read_table(f"{path}/train.parquet")     # 8,530 rows, offline
```

Design and rationale: [docs/DATASETS.md](docs/DATASETS.md).

## Verification model

- **At archive time** every file is checked against upstream expectations:
  LFS files against their upstream SHA-256, small files against their git blob
  SHA-1 (computed locally). Result: `verified-against-upstream`.
- **`modelvault verify`** re-hashes the payload and diffs it against the
  manifest: modified, missing, and extra files are flagged in
  `security.unexpected_changes`, integrity status flips to `compromised`, and
  the command exits non-zero — suitable for cron.
- The **bundle hash** (SHA-256 over the sorted per-file hash lines) gives a
  single value that fingerprints the entire payload.

## Single-file exports (.mvb.tar)

`modelvault export` packs a whole bundle into one **deterministic tar**:
entries sorted (a `.mvb.json` marker first, carrying the format version,
bundle id, and bundle hash), tar metadata normalized (mtime = the bundle's
archive date, no owners), no compression — model weights are essentially
incompressible and a plain tar stays inspectable with standard tools. The
same bundle state always exports byte-identically, so the export file has one
stable SHA-256 suitable for an offsite catalog. Export events (timestamp,
path, tar hash) are logged to the bundle's `exports.json`, which is excluded
from the tar precisely so it can't break determinism.

`modelvault import` streams the marker, checks format compatibility, unpacks
to a staging directory (safe extraction filter), re-hashes the entire payload
against the embedded manifest and marker bundle hash, and only then registers
the bundle in the vault — a corrupted archive is refused with a non-zero exit
and nothing written. The import provenance (source file, its SHA-256, when)
is recorded in the imported manifest under `archive.imported`. Full container
spec, including manual recovery without the tool:
[docs/MVB-FORMAT.md](docs/MVB-FORMAT.md).

## Running archived models (hydration)

`modelvault run <bundle> ["prompt"]` goes from bundle to generated tokens in
one command (macOS and Linux). Under the hood it *hydrates* first: picks an
engine from what the payload ships (safetensors → `transformers`,
GGUF → `llama-cpp`), builds a dedicated virtualenv **outside the bundle**
under `<vault>/.runtime/envs/` — content-keyed so bundles with the same needs
share one env — and probes it against the payload. Inference then runs fully
**offline** (`HF_HUB_OFFLINE=1`): a passing run is evidence the archived
payload alone is sufficient, with nothing quietly fetched at load time.

The chat template is applied when the tokenizer ships one (`--raw` for plain
completion); decoding is greedy for reproducibility (`--sample --seed N` for
the model's own sampling defaults). Everything is recorded, nothing
fabricated: the exact interpreter, installer, and resolved package versions
go to the bundle's `hydration.json` along with the last 20 runs, and each
successful run writes a measured `runtime.tested_hardware` entry (host, chip,
device, engine versions, tokens/sec) into the manifest. The payload stays
byte-immutable throughout — `verify` passes before and after. Envs are
disposable: `modelvault envs --prune` reclaims the disk, and the next `run`
rebuilds. Design details: [docs/HYDRATION.md](docs/HYDRATION.md).

## Extending to new artifact types and engines

The design is registry-based so new artifact types slot in later. A bundle's
`artifact_type` drives the payload root and completeness rules from
`ARTIFACT_TYPES` in `src/modelvault/schema.py` — add an entry (e.g.
`gguf-pack`, `paper`) with its payload root and required/recommended file
patterns, plus an extractor if it has structured metadata, and the
verify/export/report machinery works unchanged; the `dataset` type was added
exactly this way.
Inference runtimes work the same way: `ENGINES` in `src/modelvault/hydrate.py`
maps detection globs to pip requirements and a standalone runner script, so an
MLX, vLLM, or ONNX engine is a registry entry plus a runner, with no special
cases elsewhere.

## Design notes

modelvault is deliberately Python: the hydration runners live inside the
torch/transformers ecosystem, the Hugging Face *provider* uses
`huggingface_hub` as the reference client for that host's snapshot
semantics, and the workload is IO-bound glue where a compiled rewrite would
buy seconds on a tens-of-minutes job. Acquisition is a plugin — Hugging Face
is the first backend, not the product. Longevity is carried by the
**formats**, not the tool — plain-JSON manifests and plain-tar exports, each
documented for manual recovery without modelvault — so the bundles outlive
whatever software reads them next. Full rationale, accepted costs, and the
revisit criteria: [docs/DESIGN.md](docs/DESIGN.md). Source providers:
[docs/SOURCES.md](docs/SOURCES.md). How to consume GitHub releases, and why
this project does not ship frozen binaries as the primary artifact:
[docs/DISTRIBUTION.md](docs/DISTRIBUTION.md).

## License

Apache License 2.0. See [LICENSE](LICENSE). Bundles record *upstream* model
and dataset licenses separately in `manifest.json`; those do not change the
license of this tool.

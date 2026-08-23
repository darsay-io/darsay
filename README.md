# model-vault

Tools for downloading and archiving **full model ecosystems** as reproducible,
auditable bundles — museum-grade curation that stays directly usable.

`modelvault` pulls a model repo from Hugging Face at a pinned revision, hashes
every file (SHA-256, plus BLAKE3 when available), cross-checks each file
against upstream LFS/git checksums, captures the license verbatim with rights
flags, extracts model metadata straight from the payload (parameter counts are
read from safetensors headers — no torch needed), snapshots the downstream
ecosystem (quantizations, GGUFs, finetunes), and writes it all into a bundle
that any Hugging Face-compatible loader can use as-is. When you want to hear
the archived model speak, `modelvault run <bundle>` takes it from cold storage
to a local inference — building the environment for you — in one command.

## Install

```bash
python3 -m venv .venv
.venv/bin/pip install -e .                 # core: huggingface_hub only
.venv/bin/pip install -e ".[fast-hash,smoke]"   # + blake3, tokenizers
.venv/bin/pip install -e ".[inference]"    # + transformers/torch for inference smoke test
```

The extras only serve the in-process smoke tests; `modelvault run` needs none
of them — hydration builds its own isolated env per engine (see below).

## Usage

```bash
modelvault archive Qwen/Qwen3-0.6B          # download + hash + manifest + reports
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
that live on disk or in your backup tier, not in this repo.

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
└── LICENSE             # upstream license text, surfaced at the root
```

The payload under `model/` is **immutable after archiving**; the bundle hash
covers it alone. Metadata at the bundle root is mutable by design, so
verification runs, curation notes, and access timestamps never disturb the
archived artifact. To use the model, point `transformers` (or any HF-compatible
loader) at `<bundle>/model` — no unpacking or conversion needed.

## What the manifest records

| Section | Contents |
|---|---|
| `identity` | model name, family, publisher, version, release date, bundle id |
| `source` | origin, repo id, pinned commit, download timestamp, downloader tool versions, mirrors, signatures, upstream popularity + tags at archive time |
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
`artifact_type` drives completeness rules from `ARTIFACT_TYPES` in
`src/modelvault/schema.py` — add an entry (e.g. `dataset`, `gguf-pack`,
`paper`) with its required/recommended file patterns, plus an extractor if it
has structured metadata, and the verify/report machinery works unchanged.
Inference runtimes work the same way: `ENGINES` in `src/modelvault/hydrate.py`
maps detection globs to pip requirements and a standalone runner script, so an
MLX, vLLM, or ONNX engine is a registry entry plus a runner, with no special
cases elsewhere.

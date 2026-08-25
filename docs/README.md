<p align="center">
  <a href="../README.md"><img src="darsay-logo.png" alt="darsay" width="640"></a>
</p>

<p align="center">
  <strong>The formats outlive the tool.</strong><br>
  These documents are the self-describing record of those formats,
  and the design that produced them.
</p>

<p align="center">
  <a href="../README.md">Project README</a> ·
  <a href="../CONTRIBUTING.md">Contributing</a> ·
  <a href="../CHANGELOG.md">Changelog</a> ·
  <a href="../SECURITY.md">Security</a>
</p>

---

| | Current |
|---|---|
| Tool | **0.6.0** |
| Manifest schema | **1.5.0** |
| MVB format | **1.1** |
| License | Apache 2.0 |

Tool version, schema version, and export-format version bump independently.
Major schema / format bumps are breaking; additive fields are minor.

## Reading order

**Using the vault.** [Project README](../README.md) → this page → the
document for the command you are about to run.

**Keeping a bundle interpretable without the tool.**
[Manifest](MANIFEST.md) and [MVB format](MVB-FORMAT.md) are the two that
must survive a 2040 reader. Everything else is how we got there.

**Changing the tool.** [Design](DESIGN.md) for why, [Testing](TESTING.md)
and [Contributing](../CONTRIBUTING.md) for how, [Distribution](DISTRIBUTION.md)
for how a release is consumed.

## The formats

These two documents are the archival surface. A bundle remains useful if
the CLI is gone, as long as these are followed.

| Document | What it specifies |
|---|---|
| [**manifest.json**](MANIFEST.md) | Every field of the machine-readable source of truth. `null` means unknown — the tool never fabricates. |
| [**.mvb.tar**](MVB-FORMAT.md) | Single-file export: uncompressed tar, marker first, deterministic metadata, manual recovery with stock `tar`. |

## Using the vault

| Document | When to open it |
|---|---|
| [**Hydration**](HYDRATION.md) | `hydrate` / `run` / `envs` — isolated engines, offline inference, disposable venvs. |
| [**Incremental transfer**](INCREMENTAL.md) | Budgets, Ctrl-C, Range partials, `--shard N/T`, offline `assemble`, relocatable ledgers. |
| [**Datasets**](DATASETS.md) | Second artifact type. Addressed `datasets/owner/name`; payload under `data/`. |
| [**Sources**](SOURCES.md) | Provider-qualified refs (`huggingface:owner/name`). Hugging Face is a plugin. |
| [**Quantization**](QUANTIZATION.md) | Canonical bundle vs satellite quants vs disposable derived precision. |

## Project

| Document | When to open it |
|---|---|
| [**Design**](DESIGN.md) | Why Python. Why longevity is in the formats, not a frozen binary. |
| [**Distribution**](DISTRIBUTION.md) | PyPI, pipx / uvx / wheel, personal Homebrew tap. Why frozen executables are not the primary path. |
| [**Testing**](TESTING.md) | Unit / integration / opt-in Hub e2e. What the suite is there to keep. |

## Invariants

The short list that every document, and every change, is measured against:

1. **Payload immutability.** Nothing under a bundle's payload root is
   modified after archiving. Tool-written state lives at the bundle root.
   The bundle hash covers the payload only.
2. **Partials are portable.** `transfer.json` is disposable acceleration.
   Full files and bundle-local Range partials survive budgets, SIGINT,
   ledger loss, and copying to another vault. No source-machine absolute
   paths in the ledger.
3. **Export determinism.** The same bundle state produces a byte-identical
   `.mvb.tar`. Volatile machine-local files are excluded.
4. **Record, don't fabricate.** Manifests contain only what was established.
   Unknown is `null`. Query caps are recorded, never silently truncated.
5. **Verify before register.** `import` re-hashes a payload before a bundle
   enters the vault. Failures write nothing.
6. **Generated vs hand-edited.** Bundle `README.md` is a derived view
   (`regen`). `curation.md` is never overwritten once it exists.
7. **Registries, not special cases.** Artifact types, engines, and source
   providers are registry entries.
8. **Hydration is disposable.** Envs live outside bundles. Deleting
   `hydration.json` never loses archival data. Inference is offline.

## Extending the system

| To add… | Register it in… |
|---|---|
| A new artifact type (`gguf-pack`, `paper`, …) | `ARTIFACT_TYPES` in `src/darsay/schema.py` |
| A new inference runtime (MLX, vLLM, ONNX, …) | `ENGINES` in `src/darsay/hydrate.py` |
| A new acquisition host | `SourceProvider` in `src/darsay/providers/`, wired in `sources.py` |

Field changes to `manifest.json` or `.mvb.tar` need a docs update here and
a schema / format version bump. See [Contributing](../CONTRIBUTING.md).

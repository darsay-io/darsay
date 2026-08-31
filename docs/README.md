<p align="center">
  <a href="../README.md"><img src="darsay-logo.png" alt="darsay" width="640"></a>
</p>

<p align="center">
  <strong>Keep a model forever. Run it tomorrow.</strong><br>
  A vault is a folder of bundles. A bundle is a pinned snapshot<br>
  whose payload never changes.
</p>

<p align="center">
  <a href="GETTING-STARTED.md">Start here</a> ·
  <a href="CONCEPTS.md">Concepts</a> ·
  <a href="../examples/README.md">Examples</a> ·
  <a href="../README.md">README</a> ·
  <a href="../CONTRIBUTING.md">Contributing</a>
</p>

---

| | Current |
|---|---|
| Tool | **0.14.2** |
| Manifest schema | **1.6.0** |
| Catalog schema | **1.1.0** |
| MVB format | **1.2** |
| License | Apache 2.0 |

Tool version, schema version, and export-format version bump independently.
Major schema / format bumps are breaking; additive fields are minor.

## Where to go

**You have five minutes.**
[Start here](GETTING-STARTED.md) — install, archive a tiny model, look
at the bundle, run it.

**You want the picture in your head.**
[Concepts](CONCEPTS.md) — vault, bundle, pin, catalog,
payload vs metadata, why the formats outlive the tool.

**You want a command that already exists.**
[Examples](../examples/README.md) — estimate, resume, datasets, catalogs,
export, shards, verify. Then the spec for the command you are about to run.

**You are reading this in 2040 and the CLI is gone.**
[Manifest](MANIFEST.md) and [MVB format](MVB-FORMAT.md) are the two
documents that must survive to open a bundle. [Catalogs](CATALOGS.md)
are optional curator data — a want-list, not payload — and are not
required to open a bundle. Everything else is how we got there.

**You are changing the tool.**
[Design](DESIGN.md) for why, [Testing](TESTING.md) and
[Contributing](../CONTRIBUTING.md) for how,
[Distribution](DISTRIBUTION.md) for how a release is consumed.

## Using the vault

| Document | Open it when… |
|---|---|
| [**Getting started**](GETTING-STARTED.md) | You have never run `darsay` |
| [**Concepts**](CONCEPTS.md) | You want the objects named before the flags |
| [**Examples**](../examples/README.md) | You want a copy-paste recipe |
| [**Hydration**](HYDRATION.md) | `hydrate` / `run` / `envs` — isolated engines, offline inference |
| [**Incremental transfer**](INCREMENTAL.md) | Budgets, Ctrl-C, Range partials, the free-space floor and `config.toml`, `--shard N/T`, `assemble` |
| [**Datasets**](DATASETS.md) | The source is `datasets/owner/name`; payload under `data/` |
| [**Sources**](SOURCES.md) | Provider-qualified refs; Hugging Face is a plugin |
| [**Quantization**](QUANTIZATION.md) | Canonical bundle vs satellite quants vs derived precision |
| [**Catalogs**](CATALOGS.md) | Shareable want-lists; the vault is the same list, realized |
| [**Doctor**](DOCTOR.md) | Offline vault diagnostics, reversible repair, JSON contract, evidence history |

## The formats

Manifest and MVB are the archival surface of a bundle. A bundle remains
useful if the CLI is gone, as long as those two are followed. Catalogs
are an optional third surface: a shareable want-list. They are not inside
`.mvb.tar` and are not required to open a bundle.

| Document | What it specifies |
|---|---|
| [**manifest.json**](MANIFEST.md) | Every field of the machine-readable source of truth. `null` means unknown — the tool never fabricates. |
| [**.mvb.tar**](MVB-FORMAT.md) | Single-file export: uncompressed tar, marker first, frozen `darsay-verify.py`, deterministic metadata, manual recovery with stock `tar`. |
| [**catalog.json**](CATALOGS.md) | Optional curator want-list. Overlay is a view; the file does not record possession. |

## Project

| Document | Open it when… |
|---|---|
| [**Design**](DESIGN.md) | Why Python. Why transfer concurrency is threads, and where Xet fits. Why longevity is in the formats, not a frozen binary. |
| [**Distribution**](DISTRIBUTION.md) | PyPI, pipx / uvx / wheel, personal Homebrew tap. |
| [**Testing**](TESTING.md) | Unit / integration / opt-in Hub e2e. What the suite is there to keep. |

## Invariants

The short list that every document, and every change, is measured against:

1. **Payload immutability.** Nothing under a bundle's payload root is
   modified after archiving. Tool-written state lives at the bundle root.
   The bundle hash covers the payload only.
2. **Partials are portable.** `transfer.json` is disposable acceleration.
   Full files and bundle-local Range partials survive budgets, SIGINT,
   ledger loss, and copying to another vault. No source-machine absolute
   paths in the ledger. rsync is a first-class copy: the next darsay
   command trusts dest ledger + size and fetches only what is still missing.
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

# model-vault

Tools for archiving full model ecosystems as museum-grade but directly usable
bundles. One Python package: `modelvault` in `src/modelvault/`, CLI entry
point `modelvault` (argparse, subcommands in `cli.py`).

## Environment

- Python 3.14 venv at `.venv` (`.venv/bin/modelvault`, `.venv/bin/python`).
- Core dependency is `huggingface_hub` only; `blake3`, `tokenizers`,
  `transformers`/`torch` are optional extras — every feature that needs them
  must degrade gracefully (record `skipped` with a reason, never crash).
- No test suite yet. Changes are validated by running the CLI against a tiny
  repo (e.g. `sshleifer/tiny-gpt2`) into a scratch `--vault`, plus the checks
  in the invariants list below.

## Layout

- `src/modelvault/` — `archiver.py` (download + manifest assembly),
  `verify.py`, `smoke.py`, `export.py` (.mvb.tar), `readme_gen.py`,
  `metadata.py`, `licensing.py`, `hashing.py`, `safetensors_meta.py`,
  `schema.py` (artifact-type registry), `hydrate.py` (ENGINES registry, env
  management, `hydrate`/`run`), `runners/` (standalone per-engine scripts run
  inside hydrated envs — stdlib + engine only, no modelvault imports),
  `cli.py`.
- `docs/MANIFEST.md` — field-by-field manifest schema reference.
  `docs/MVB-FORMAT.md` — single-file export format spec.
  `docs/HYDRATION.md` — bundle→runnable-install design (envs, runner
  contract, hydration.json).
  **Update these whenever manifest fields or the export format change**, and
  bump `SCHEMA_VERSION` (`__init__.py`) / `MVB_FORMAT_VERSION` (`export.py`)
  appropriately (major = breaking).
- `vault/` — archived bundles. **Gitignored; never commit bundles.** The
  reference bundle is `vault/qwen--qwen3-0.6b/c1899de289a0/` (Qwen3-0.6B).

## Invariants — do not break

- **Payload immutability:** nothing under a bundle's `model/` is ever
  modified after archiving. All tool-written state goes to bundle-root
  metadata files; the bundle hash covers `model/` only.
- **Export determinism:** the same bundle state must produce a byte-identical
  `.mvb.tar` (sorted entries, marker first, normalized tar metadata, no wall
  clock inside the tar). Volatile machine-local data — export logs, hydration
  and run records — goes in `exports.json` / `hydration.json`, which are
  excluded from exports. Check: export twice, compare SHA-256.
- **Record, don't fabricate:** manifests contain only what was established
  from upstream or the payload; unknown = `null` (curator fills in later).
  Query caps must be recorded (`query_limit`), never silently truncated.
- **Verify before register:** `import` must fully re-hash a payload before a
  bundle enters the vault; failures register nothing and exit non-zero.
- **Generated vs hand-edited:** bundle `README.md` is a derived view
  (`regen`); `curation.md` is the curator's file and must never be
  overwritten once it exists.
- **Extensibility:** new artifact types are added via the `ARTIFACT_TYPES`
  registry in `schema.py`, and new inference runtimes via the `ENGINES`
  registry in `hydrate.py` — not by special-casing elsewhere.
- **Hydration is disposable:** envs live outside bundles (under
  `<vault>/.runtime/`, shared and content-keyed); deleting `hydration.json`
  or any env must never lose archival data. Inference runs are offline
  (`HF_HUB_OFFLINE=1`) so a passing run proves payload self-sufficiency.

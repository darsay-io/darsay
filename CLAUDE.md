# darsay

the genesis machine of archives

Tools for archiving full model ecosystems as museum-grade but directly usable
bundles. One Python package: `darsay` in `src/darsay/`, CLI entry
point `darsay` (argparse, subcommands in `cli.py`). The mission and the
vocabulary every surface shares — work, negative and print, precision,
family / generation / member — are in `docs/NORTH-STAR.md`; the words
are not optional, and *master* is not one of them.

## Environment

- Python 3.14 venv at `.venv` (`.venv/bin/darsay`, `.venv/bin/python`).
- Core dependency is `huggingface_hub` only (Hugging Face provider); `blake3`, `tokenizers`,
  `transformers`/`torch` are optional extras — every feature that needs them
  must degrade gracefully (record `skipped` with a reason, never crash).
- Test suite is pytest (`pip install -e ".[dev]"`; `pytest`). Unit +
  integration are hermetic (fake `test:` provider, no network). Live Hub e2e
  is opt-in (`pytest --run-e2e` or `DARSAY_E2E=1`) and uses
  `sshleifer/tiny-gpt2`. See `docs/TESTING.md`.

## Layout

- `src/darsay/` — `archiver.py` (download + manifest assembly),
  `transfer.py` (pin ledger, reconciliation, resumable/budgeted per-file
  transfer through a fetch → hash → commit pipeline — `_FileJob`, `--jobs`
  streams, one hash thread, ledger writes only on the main thread —
  cooperative lanes/assembly, sibling-blob reuse, the rate-cap
  leaky bucket, and reconnect-after-network-loss via the shared `Link`),
  `progress.py` (archive-level live transfer panel: percent, bytes, rate,
  ETA, offline/reconnecting/verifying states; captures stray stdout/stderr
  *and* library `StreamHandler`s above the panel),
  `estimate.py` (read-only preflight: sizes/params/disk from source metadata),
  `config.py` (machine-local TOML settings — user file + `<vault>/config.toml`
  + env + flag layers; the transfer free-space floor, rate cap, and
  offline patience live here; config is operator preference, never bundle
  content),
  `relocate.py` (`darsay mv` / `darsay cp`: relocate or replicate a registered
  bundle into another vault — `mv` renames on one filesystem, else copy →
  verify at the destination → remove the source; `cp` copies → verifies →
  keeps the source and records the replica in both manifests; partials are
  refused, `assemble --handoff` is their verb),
  `migrate.py` (`darsay migrate`: brings a record written under an earlier
  schema major to the schema this darsay writes — offline, from the record
  and the payload, hashing nothing; the one place an older major is read;
  `import` calls it as an older export lands),
  `lineage.py` (the name grammar: family, generation, member, variants,
  formats, size — read from a work's name and labeled so; mirrored in
  `website/src/lib/lineage.ts` against `tests/fixtures/lineage-names.json`),
  `precision.py` (release-precision labels from `config.json` /
  dtypes / GGUF names, and measured bytes per parameter; mirrored in
  `website/src/lib/precision.ts`),
  `classify.py` (negative / print / support / unknown verdicts over a
  repo's weight sets; the archive default keeps the negatives),
  `verify.py`, `standalone_verify.py` (stdlib-only; frozen into `.mvb.tar`
  as `darsay-verify.py` — changing it is an MVB minor bump), `smoke.py`,
  `export.py` (.mvb.tar), `readme_gen.py`,
  `metadata.py`, `licensing.py`, `hashing.py`, `safetensors_meta.py`,
  `schema.py` (artifact-type registry), `hydrate.py` (ENGINES registry, env
  management, `hydrate`/`run`), `runners/` (standalone per-engine scripts run
  inside hydrated envs — stdlib + engine only, no darsay imports),
  `sources.py` (source-ref grammar + provider registry), `providers/`
  (acquisition backends; Hugging Face is the first plugin; a provider
  classifies its transport's transient failures via
  `transient_network_error` — transfer.py never imports httpx),
  `cli.py` (every subcommand runs under `_run`: no tracebacks reach users
  unless `DARSAY_DEBUG=1`).
- `tests/` — pytest pyramid: `unit/`, `integration/` (fake `test:` provider),
  `e2e/` (live Hub, opt-in). See `docs/TESTING.md`.
- `docs/NORTH-STAR.md` — the mission and the model of the models.
  `docs/GETTING-STARTED.md` — first-bundle walkthrough for new users.
  `docs/CONCEPTS.md` — vault, bundle, pin, payload vs metadata, negatives
  and prints, precision, lineage, closed works.
  `examples/README.md` — copy-paste cookbook.
  `docs/README.md` — documentation home / reading map.
  `docs/MANIFEST.md` — field-by-field manifest schema reference.
  `docs/MVB-FORMAT.md` — single-file export format spec.
  `docs/HYDRATION.md` — bundle→runnable-install design (envs, runner
  contract, hydration.json).
  `docs/QUANTIZATION.md` — negatives and prints, precision and bytes per
  parameter: what gets archived vs derived when a model has quantized
  variants.
  `docs/DESIGN.md` — implementation rationale: why Python, why transfer
  concurrency stays in Python threads (measurements; the Xet tradeoff),
  and why bundle longevity rests on the formats, not the tool.
  `docs/DATASETS.md` — dataset bundles: Hub-address refs, per-type payload
  roots, dataset manifest sections.
  `docs/INCREMENTAL.md` — incremental archiving: idempotent resumable transfer — pin →
  reconcile → plan → transfer → register, `transfer.json` ledger, session
  budgets, local-source adoption.
  `docs/DISTRIBUTION.md` — how releases are consumed (PyPI / pipx / uvx, personal Homebrew tap)
  and why frozen binaries are not the primary install path.
  `docs/SOURCES.md` — acquisition providers; the public source-ref grammar;
  Hugging Face as a plugin.
  `docs/TESTING.md` — test pyramid (unit / integration / e2e) and CI.
  `docs/FAQ.md` — moving and copying bundles between disks, rsync's standing, `mv` vs
  `assemble --handoff`, what travels with a bundle and what stays, records
  from an older darsay (`migrate`).
  **Update these whenever manifest fields or the export format change**, and
  bump `SCHEMA_VERSION` (`__init__.py`) / `MVB_FORMAT_VERSION` (`export.py`)
  appropriately (major = breaking). Keep GETTING-STARTED / CONCEPTS /
  examples in sync with the CLI (do not document unshipped flags as live).
  **A new `docs/*.md` page needs one line in `scripts/release.py`:** either
  `CLI_DOCS`, if a user reads about flags there — its darsay flags must then
  all be live — or `UNCHECKED_DOCS` with the reason (a page naming a flag to
  say it does *not* exist, or another program's command lines, belongs
  there). A page in neither refuses the release. Nothing else is needed:
  darsay.io publishes every `docs/*.md` on its own, and the release gate
  resolves the page's links wherever it lives.
- `vault/` — archived bundles. **Gitignored; never commit bundles.** The
  reference bundle is `vault/qwen--qwen3-0.6b/c1899de289a0/` (Qwen3-0.6B).

## Invariants — do not break

- **Payload immutability:** nothing under a bundle's `model/` is ever
  modified after archiving. All tool-written state goes to bundle-root
  metadata files; the bundle hash covers `model/` only.
- **Partial bytes are authoritative and portable:** `transfer.json` is
  disposable acceleration state; full payload files and bundle-local
  `.cache/huggingface/` partials must survive budgets, SIGINT, ledger loss,
  and copying to a different vault. Never put source-machine absolute paths
  in the ledger.
- **rsync is a first-class copy:** an out-of-band `rsync` / `cp -a` of a
  bundle (or its payload) into the usual vault layout is the same as a
  darsay copy. The next command trusts dest files already `verified` in
  dest's ledger (size match), downloads only what is still missing, and
  adjusts metadata. It must not re-download a file whose digest matches,
  and it must not pull dest back over a network mount to re-hash a copy.
  Hash dest where it is a local disk (`assemble --rehash` / `verify`).
  **This stays true forever:** `darsay mv` and `darsay cp` are that same
  contract folded into verbs (copy, verify where it landed, then remove or
  keep the source; `mv` is a rename on one filesystem) and must never become
  a requirement — nothing they record is needed to open, verify, run, or
  export a bundle. Vocabulary:
  *move* = a registered bundle changes vault (`mv`); *hand-off* = a
  partial's verified files cross vaults one by one, leaving a skeleton
  (`assemble --handoff`, ledger state `handed_off`). Never call the second
  one a move.
- **Export determinism:** the same bundle state must produce a byte-identical
  `.mvb.tar` (sorted entries, marker first, normalized tar metadata, no wall
  clock inside the tar). Volatile machine-local data — export logs, hydration
  and run records — goes in `exports.json` / `hydration.json`, which are
  excluded from exports. `transfer.json` / `transfer.lock` are excluded too.
  Check: export twice, compare SHA-256.
- **Record, don't fabricate:** manifests contain only what was established
  from upstream or the payload; unknown = `null` (curator fills in later).
  Query caps must be recorded (`query_limit`), never silently truncated.
  Anything derived says what from: lineage read from a name carries
  `read_from: "name"`, bytes per parameter is measured from the priced
  bytes, a precision label names its source.
- **Fix forward:** darsay is greenfield. A change to the model of the
  models bumps the schema major and every reader follows; no
  compatibility knobs, no fields kept to describe how things used to be,
  and no reader reads around an older record. What moves is the record:
  `darsay migrate` re-reads a record of an earlier major under the current
  model and writes it in the current shape — `migrate.py` is the one
  place an older major is read, the payload is never touched, and
  `archive.migrations` says it happened. Breaking changes are named in
  the changelog with their recovery, and the recovery is `migrate`, never
  a re-download.
- **Verify before register:** `import` must fully re-hash a payload before a
  bundle enters the vault; failures register nothing and exit non-zero.
- **Generated vs hand-edited:** bundle `README.md` is a derived view
  (`regen`); `curation.md` is the curator's file and must never be
  overwritten once it exists.
- **Extensibility:** new artifact types are added via the `ARTIFACT_TYPES`
  registry in `schema.py`, new inference runtimes via the `ENGINES`
  registry in `hydrate.py`, and new acquisition hosts via `SourceProvider`
  in `providers/` (registered in `sources.py`) — not by special-casing
  elsewhere.
- **Hydration is disposable:** envs live outside bundles (under
  `<vault>/.runtime/`, shared and content-keyed); deleting `hydration.json`
  or any env must never lose archival data. Inference runs are offline
  (`HF_HUB_OFFLINE=1`) so a passing run proves payload self-sufficiency.

# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
Tool version (`pyproject.toml` / `darsay.__version__`) is independent of
`schema_version` in manifests and of `MVB_FORMAT_VERSION` in exports.

## [Unreleased]

### Added

- **Live archive progress** — `darsay archive` draws a three-line transfer
  panel on a TTY: percent of the whole payload, bytes in / total, smoothed
  rate with a sparkline, time remaining in plain language, files done, and
  the file now in flight (or hashing). Piped / logged runs emit a status
  line every 10 seconds. `DARSAY_PROGRESS=0` turns it off; `=line` forces
  the log form even on a TTY. The Hub client's
  per-file bar is replaced; budgets still use the same network-byte
  callbacks. See [Incremental transfer](docs/INCREMENTAL.md).
- **Standalone verifier** — `src/darsay/standalone_verify.py` is stdlib-only
  (no `huggingface_hub`, no `blake3`, no darsay imports). It re-hashes a
  bundle directory or a `.mvb.tar` against `manifest.json` and writes
  nothing. Every export copies it verbatim into the tar as
  `darsay-verify.py`. MVB format **1.2** (minor; 1.x imports still
  accepted). Changing the script is an MVB minor bump so export
  determinism holds. Spec: [docs/DESIGN.md](docs/DESIGN.md),
  [docs/MVB-FORMAT.md](docs/MVB-FORMAT.md).

### Changed

- GitHub repositories moved to the `darsay-io` org: [`darsay-io/darsay`](https://github.com/darsay-io/darsay)
  and [`darsay-io/homebrew-darsay`](https://github.com/darsay-io/homebrew-darsay).
  Install with `brew install darsay-io/darsay/darsay`. Old
  `jeremynorris/darsay` URLs redirect. PyPI Trusted Publishing must use
  owner `darsay-io` (already configured on the GitHub side).

## [0.7.0] - 2026-08-26

### Breaking

- The default vault is `$DARSAY_HOME` or `~/darsay` (no longer `./vault`).
  Commands that open the vault print the path on stderr when the default
  is in use. `--vault` is accepted before or after the subcommand. Existing
  `./vault` trees keep working if you pass `--vault ./vault` or set
  `$DARSAY_HOME`.

### Added

- **Catalogs** — a curated, shareable list of sources (want-list). The vault
  is the same list, realized. `darsay catalog new/add/drop/adopt/regen`,
  `darsay list CATALOG`, `darsay estimate CATALOG`, `darsay archive --next
  CATALOG`. Possession is an overlay view; `archive` does not rewrite
  `catalog.json`. Catalog schema **1.0.0** (independent of bundle schema
  1.6.0). Spec: [docs/CATALOGS.md](docs/CATALOGS.md).
- Manifest `kind: "darsay.bundle"` on new archives. 1.x files with the
  field missing are still read as `darsay.bundle` (0.6.0 vaults and
  `.mvb.tar` files keep loading). Any other `kind` is a load error.
- `archive --include GLOB` (repeatable) pins a subset of the upstream
  snapshot. Matching files plus sidecars (config, tokenizer, license, card)
  are transferred; `source.subset` records the include patterns and the
  full upstream file list. Manifest schema **1.6.0** (additive).
- Bundle-taking commands (`info`, `run`, `verify`, `export`, …) accept a
  filesystem path, a bundle id (`name@revision12`), or a unique prefix of
  either. HAVE in `list` is that id.
- `darsay rm` deletes bundles (confirmation unless `--yes`). `darsay du`
  reports on-disk size of bundles and `.runtime`. `list --json` / `--ids`
  for scripts (`on_disk_bytes` vs `payload_bytes`). `darsay complete
  bash|zsh|fish` prints a completion script (generated from the command
  list so it cannot drift).
- `darsay run` joins unquoted prompt words, and `--repl` keeps the model
  loaded for a multi-turn loop (`/quit` to exit). New `mlx` engine on
  Apple Silicon (`--engine mlx`; auto-detected for `*.npz` payloads).
- Documentation: [getting-started](docs/GETTING-STARTED.md) walkthrough,
  [concepts](docs/CONCEPTS.md), and [examples cookbook](examples/README.md).
  A personal Homebrew tap lives at
  [`jeremynorris/homebrew-darsay`](https://github.com/jeremynorris/homebrew-darsay).
- Ruff as the project linter and formatter. The `dev` extra installs it;
  CI runs `ruff check` and `ruff format --check`. Config is `[tool.ruff]`
  in `pyproject.toml`.

### Changed

- `darsay list` is one table for the vault and for a catalog: STATUS,
  DESIRE, SOURCE, HAVE, SIZE, NOTE. Uniformly empty DESIRE/NOTE columns
  hide; vault `list` omits “remaining” when nothing is unfinished. PATH /
  LICENSE / INTEGRITY / ARCHIVED stay in `list --json` and `info`.
  `list --json` without a catalog remains an array (additive keys).
  `list CATALOG --json` is an overlay envelope. `list --next` prints a
  copy-pasteable `darsay archive` line (source + `--revision` + `--include`).
- Slug-shaped catalog specs resolve only under `catalogs/`; filesystem
  specs require `./`, `~/`, or an absolute path.
- `archive --next` / `--sort next` prefer `partial` over `want`.
- Unknown top-level catalog keys round-trip; stored estimates are
  projected onto digest keys (no disk paths). Known-provider locators
  that do not parse fail `load_catalog`; `unknown` is reserved for
  unregistered schemes.
- bash/zsh/fish complete catalog ids; `archive` completes them only after
  `--next`.
- `load_manifest` requires `schema_version` and refuses major `> 1`.
  `import` checks the marker's embedded schema major before unpacking.
  Unknown top-level manifest keys round-trip. `darsay run` does not stamp
  the tool's current schema onto the record.
- Export events in `exports.json` record `written_by` (tool + version).
  The in-tar marker still names only the format family, so packing the same
  bundle state is byte-identical across tool releases.
- `hydrate` / `run` preflight the payload: refuse non-causal-LM
  architectures on the transformers runner, compare estimated RAM to
  this machine, and announce the first-time package install. Override
  with `--ignore-preflight`. Causal heads include `*ForCausalLM` and
  `*LMHeadModel` (GPT-2 family).
- A passing `verify` after a restored payload heals `integrity_status`
  back to `verified-against-upstream` (the unexpected-changes log stays
  append-only). `info` no longer rewrites `manifest.json`.
- Hydration rebuilds install the package versions recorded in
  `hydration.json` (`engine_packages`) instead of floating `torch` /
  `transformers`. `--force` refreshes to current PyPI.

### Fixed

- `archive --next` and `list --next` share one idle contract (empty
  catalog / unknown remaining are errors; all-have is idempotent success
  on stderr). `list --want` on a complete vault no longer says the vault
  is empty.
- `catalog add` no longer crashes when the file already has an
  unknown-provider row.
- `estimate CATALOG` regenerates `README.md` (sizes were cached in JSON
  only).
- A finished dataset archive suggests `darsay info`, not `darsay run`.
- `info` prints the bundle path (PATH left the human `list` table).
- `archive` of a full-repo source will not silently resume a subset pin of
  the same revision. `catalog drop` can select the full-repo sibling with
  `--full`. Cached estimates with unknown upstream sizes print remaining
  as `+ ?`, not `0 B`. `list CATALOG --want` no longer calls an
  unknown-only catalog complete. `import` refuses a marker with no
  `schema_version` before unpacking.

## [0.6.0] - 2026-08

Acquisition providers. Manifest schema 1.5.0.

### Added

- Source-ref grammar: `huggingface:Qwen/Qwen3-0.6B`,
  `huggingface:datasets/owner/name`. Hugging Face URLs and unprefixed
  `owner/name` / `datasets/owner/name` remain shorthand.
- `SourceProvider` registry (`src/darsay/sources.py`,
  `src/darsay/providers/`). Hugging Face is the first plugin;
  `archiver` / `estimate` / `transfer` no longer import `huggingface_hub`.
- Manifest `source.provider` and `source.address` (additive).
- [docs/SOURCES.md](docs/SOURCES.md).
- Apache-2.0 `LICENSE` and `NOTICE`.
- GitHub Actions workflows to install the package on supported Pythons and to
  attach an sdist + wheel to tagged GitHub Releases.
- [docs/DISTRIBUTION.md](docs/DISTRIBUTION.md): how to consume releases, and
  why frozen binaries are not the primary install path.
- Test suite (pytest) as a three-layer pyramid: unit tests, integration tests
  against an in-process `test:` acquisition provider, and an opt-in Hub e2e
  path (`sshleifer/tiny-gpt2`). See [docs/TESTING.md](docs/TESTING.md).
- CI on push and pull request: hermetic tests on Python 3.10/3.12/3.14,
  live Hub e2e, and the existing wheel/sdist job.

### Changed

- CLI `estimate` / `archive` take `source` rather than `repo_id`.
  Python entry points are `archive()` and `estimate()`; `archive_model` /
  `estimate_repo` remain as wrappers.
- **Rename:** the package, CLI, and environment variables are now `darsay`
  (`darsay` / `$DARSAY_HOME` / `$DARSAY_RUNTIME` / `$DARSAY_PYTHON` /
  `$DARSAY_E2E`). This is a clean break: there are no `MODELVAULT_*`
  fallbacks. Existing bundles that record `"tool": "modelvault"` still
  verify and import; new archives and exports write `"tool": "darsay"`.
  The GitHub repository is `jeremynorris/darsay`. Schema and MVB
  format versions are unchanged.
- Documentation: landing README with the project logo, a documentation
  index at [`docs/README.md`](docs/README.md), and consistent navigation
  across the docs set.
- Project URLs now point at `jeremynorris/darsay`.

## [0.5.0] - 2026-08

Incremental archiving. Manifest schema 1.4.0.

### Added

- Idempotent resumable transfer: pin → reconcile → plan → transfer → register.
- `transfer.json` ledger, session budgets (`--max-gb` / `--max-bytes` /
  `--max-minutes`), HTTP Range partials, cooperative `--shard N/T` lanes,
  offline `assemble`, and sibling-bundle blob reuse.
- Partial bundles are relocatable; the ledger holds no source-machine
  absolute paths.

## [0.4.0] - 2026-08

Provenance: Hub gates and true lineage edges. Manifest schema 1.3.0.

## [0.3.0] - 2026-08

Dataset bundles as a second artifact type. Manifest schema 1.2.0.

## [0.2.0] - 2026-08

Hydration and `modelvault run`. Manifest schema 1.1.0 (`runtime.tested_hardware`).

## [0.1.0] - 2026-08

Initial model-bundle archiver: estimate, archive, verify, smoke, export/import,
manifest schema 1.0.0.

# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
Tool version (`pyproject.toml` / `darsay.__version__`) is independent of
`schema_version` in manifests and of `MVB_FORMAT_VERSION` in exports.

## [Unreleased]

### Added

- Bundle-taking commands (`info`, `run`, `verify`, `export`, …) accept a
  filesystem path, a bundle id (`name@revision12`), or a unique prefix of
  either. `darsay list` prints a `PATH` column so the id is a usable handle,
  not just a label.
- `archive --include GLOB` (repeatable) pins a subset of the upstream
  snapshot. Matching files plus sidecars (config, tokenizer, license, card)
  are transferred; `source.subset` records the include patterns and the
  full upstream file list. Manifest schema **1.6.0** (additive).

### Changed

- The default vault is `$DARSAY_HOME` or `~/darsay` (no longer `./vault`).
  Commands that open the vault print the path on stderr when the default
  is in use. `--vault` is accepted before or after the subcommand.
- `hydrate` / `run` preflight the payload: refuse non-causal-LM
  architectures on the transformers runner, compare estimated RAM to
  this machine, and announce the first-time package install. Override
  with `--ignore-preflight`.
- A passing `verify` after a restored payload heals `integrity_status`
  back to `verified-against-upstream` (the unexpected-changes log stays
  append-only). `info` no longer rewrites `manifest.json`.

- **Rename:** the package, CLI, and environment variables are now `darsay`
  (`darsay` / `$DARSAY_HOME` / `$DARSAY_RUNTIME` / `$DARSAY_PYTHON` /
  `$DARSAY_E2E`). This is a clean break: there are no `MODELVAULT_*`
  fallbacks. Existing bundles that record `"tool": "modelvault"` still
  verify and import; new archives and exports write `"tool": "darsay"`.
  The GitHub repository will be `jeremynorris/darsay`. Schema and MVB
  format versions are unchanged.
- Documentation: landing README opens with three commands and the
  vault/bundle mental model. New [getting-started](docs/GETTING-STARTED.md)
  walkthrough, [concepts](docs/CONCEPTS.md), and
  [examples cookbook](examples/README.md). Documentation home at
  [`docs/README.md`](docs/README.md); every spec page shares the same
  navigation and user-facing specs lead with one sentence. `darsay --help`
  matches that first impression. Install from
  [PyPI](https://pypi.org/project/darsay/)
  (`pipx install darsay` / `uvx darsay` / `uv tool install darsay`).
  A personal Homebrew tap lives at
  [`jeremynorris/homebrew-darsay`](https://github.com/jeremynorris/homebrew-darsay)
  (`brew install jeremynorris/darsay/darsay`); this is not homebrew/core.
- Project URLs now point at `jeremynorris/darsay`.

### Added

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

## [0.6.0] - 2026-08

Acquisition providers. Manifest schema 1.5.0.

### Added

- Source-ref grammar: `huggingface:Qwen/Qwen3-0.6B`,
  `huggingface:datasets/owner/name`. Hugging Face URLs and unprefixed
  `owner/name` / `datasets/owner/name` remain shorthand.
- `SourceProvider` registry (`src/modelvault/sources.py`,
  `src/modelvault/providers/`). Hugging Face is the first plugin;
  `archiver` / `estimate` / `transfer` no longer import `huggingface_hub`.
- Manifest `source.provider` and `source.address` (additive).
- [docs/SOURCES.md](docs/SOURCES.md).

### Changed

- CLI `estimate` / `archive` take `source` rather than `repo_id`.
  Python entry points are `archive()` and `estimate()`; `archive_model` /
  `estimate_repo` remain as wrappers.

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

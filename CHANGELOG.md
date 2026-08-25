# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
Tool version (`pyproject.toml` / `modelvault.__version__`) is independent of
`schema_version` in manifests and of `MVB_FORMAT_VERSION` in exports.

## [Unreleased]

### Added

- Apache-2.0 `LICENSE` and `NOTICE`.
- GitHub Actions workflows to install the package on supported Pythons and to
  attach an sdist + wheel to tagged GitHub Releases.
- [docs/DISTRIBUTION.md](docs/DISTRIBUTION.md): how to consume releases, and
  why frozen binaries are not the primary install path.

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

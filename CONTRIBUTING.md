# Contributing

Full documentation lives in [`docs/`](docs/README.md).

## Setup

Python 3.10+ (development uses 3.14). From the repo root:

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[fast-hash,smoke,dev]"
.venv/bin/darsay --version
.venv/bin/pytest                    # unit + integration, no network
```

The suite is a pyramid: hermetic unit and integration tests (fake `test:`
provider) on every run, live Hub e2e opt-in. See
[docs/TESTING.md](docs/TESTING.md).

```bash
.venv/bin/pytest --run-e2e -m e2e   # or DARSAY_E2E=1; uses sshleifer/tiny-gpt2
```

Never archive into the gitignored `vault/` you use for real archives. GitHub
Actions runs the hermetic suite and the Hub path on every push and pull
request.

## Invariants

The short list that must not break:

- Nothing under a bundle's payload root is modified after archiving.
- `transfer.json` is disposable; full files and bundle-local partials are
  the portable state. No absolute host paths in the ledger.
- The same bundle state must export to a byte-identical `.mvb.tar`.
- Manifests record what was established; unknown is `null`.
- `import` re-hashes before registering; failures write nothing.
- `README.md` inside a bundle is generated (`regen`); `curation.md` is not
  overwritten once it exists.
- New artifact types go in `ARTIFACT_TYPES`; new runtimes go in `ENGINES`;
  new acquisition hosts go in `sources` / `providers/`, not as CLI flags.
- Hydration is disposable and inference is offline.

Field changes to `manifest.json` or `.mvb.tar` need a docs update
(`docs/MANIFEST.md` / `docs/MVB-FORMAT.md`) and a schema / format version
bump.

## Versioning

Bump **both** `project.version` in `pyproject.toml` and `__version__` in
`src/darsay/__init__.py`. Add a `CHANGELOG.md` section. Tag the GitHub
release as `vX.Y.Z` (the `release` workflow attaches the wheel and sdist).

`SCHEMA_VERSION` and `MVB_FORMAT_VERSION` bump independently, on format
changes only.

## License

By contributing, you agree that your contribution is licensed under the
Apache License 2.0, the same license as the rest of the project.

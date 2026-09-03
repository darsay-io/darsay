# Contributing

<p align="center">
  <a href="docs/GETTING-STARTED.md">Start here</a> ·
  <a href="docs/CONCEPTS.md">Concepts</a> ·
  <a href="examples/README.md">Examples</a> ·
  <a href="docs/README.md">All docs</a> ·
  <a href="README.md">README</a>
</p>

New to the tool? [Start here](docs/GETTING-STARTED.md). Full
documentation lives in [`docs/`](docs/README.md).

## Setup

Python 3.10+ (development uses 3.14). From the repo root:

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[fast-hash,smoke,dev]"
.venv/bin/darsay --version
.venv/bin/ruff check && .venv/bin/ruff format --check
.venv/bin/pytest                    # unit + integration, no network
```

The suite is a pyramid: hermetic unit and integration tests (fake `test:`
provider) on every run, live Hub e2e opt-in. See
[docs/TESTING.md](docs/TESTING.md).

```bash
.venv/bin/pytest --run-e2e -m e2e   # or DARSAY_E2E=1; uses sshleifer/tiny-gpt2
```

Never archive into the gitignored `vault/` you use for real archives. GitHub
Actions runs Ruff, the hermetic suite, and the Hub path on every push and
pull request.

## Lint and format

[Ruff](https://docs.astral.sh/ruff/) is the linter and formatter. It
ships in the `dev` extra; config is `[tool.ruff]` in `pyproject.toml`.

```bash
.venv/bin/ruff check          # lint
.venv/bin/ruff format         # rewrite to the project style
.venv/bin/ruff check --fix    # lint, apply safe fixes
```

CI runs `ruff check` and `ruff format --check`. Do not add flake8,
Black, or isort — Ruff replaces them.

## Invariants

The short list that must not break:

- Nothing under a bundle's payload root is modified after archiving.
- `transfer.json` is disposable; full files and bundle-local partials are
  the portable state. No absolute host paths in the ledger. rsync into a
  vault is a first-class copy: the next command trusts dest ledger + size
  and fetches only the remainder. Do not re-hash dest over a network mount.
- Hashing dest (`assemble --rehash`, adoption of unrecorded dest bytes)
  must show live progress (percent, current file, ETA) — never a silent hang.
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

## Releasing

The tool version lives in exactly one place: `__version__` in
`src/darsay/__init__.py`. `pyproject.toml` derives it via
`[tool.setuptools.dynamic]`, so there is no second copy to keep in sync.

Keep release notes under `## [Unreleased]` in `CHANGELOG.md` (or under
`## [X.Y.Z]` if you already named the version). The script will not invent
notes. Then:

```bash
.venv/bin/python scripts/release.py 0.11.0
```

It refuses to start unless the tree is clean, you are on `main`, level
with `origin/main`, and the tag is free both locally and on the remote.
Then it bumps the version, promotes `[Unreleased]` (or stamps a missing
date), leaves a fresh `[Unreleased]` stub, updates the docs version
table, and runs the full CI gate: lint, format, tests with the coverage
floor, `python -m build`, `twine check`. Only then does it commit and
tag. If the gate fails it reverts the bump and commits nothing.

The gate also reads the docs' links. Every relative link in `README.md`,
`CONTRIBUTING.md`, `examples/README.md`, and every `docs/*.md` — Markdown
and HTML both — must name a file that is here (`check_docs_links`). The
page list is globbed, not typed, so a docs page added today is checked
today. darsay.io publishes those same pages through a transform that
refuses a link it cannot resolve; this is that rule one repository
earlier, before the tag exists.

The flag checks cannot be globbed the same way, and the gate makes that
explicit rather than silent. They read a page as *every darsay flag named
here is live*, which is wrong for a page that names a flag to say it does
not exist — `SOURCES.md`'s "Do not add a `--provider` flag", `DATASETS.md`'s
"no `--type` flag", `QUANTIZATION.md`'s labelled `hydrate --quantize`
proposal. So a new `docs/*.md` page takes one line: `CLI_DOCS` if a user
reads about flags there, or `UNCHECKED_DOCS` with the reason it is exempt.
A page in neither stops the release by name (`check_docs_pages_classified`),
which is the difference that matters — three pages had drifted in
unclassified before this existed, and nothing said so.

The script pushes nothing. Releasing is pushing the tag:

```bash
git push origin main
git push origin v0.8.1      # this is the release
```

`--dry-run` runs every check and writes nothing, `--skip-build` drops the
slowest gate, and `--push` pushes `main` so the tag is the only step left.

The `release` workflow attaches the wheel and sdist to the GitHub Release
and publishes them to PyPI (Trusted Publishing, environment `pypi`),
re-checking that the tag matches the artifacts it just built. PyPI's
index cache can keep `pipx upgrade` on the previous version for about
ten minutes; the force-refresh is in
[docs/DISTRIBUTION.md](docs/DISTRIBUTION.md).

darsay.io `/docs/` pins that same GitHub Release in `darsay-io/website`'s
`docs.lock.json`, and publishes every `docs/*.md` plus `examples/README.md`
from it — a new page needs no edit over there. The `Docs site transform`
job runs that repository's real transform against this checkout on every
pull request, and the `Release` workflow will not publish until it passes,
so a docs change that the site could not publish fails here rather than in
the website's sync hours after the tag. It runs against the website's `main`,
so on the rare occasion it fails on a tag the tag exists but nothing was
published: fix the website, then re-run the `Release` workflow. After the tag, `Sync CLI docs`
commits the pin straight to `main` there and `Deploy` ships it; if it ever
does fail, it opens one `docs-sync` issue for the tag instead of retrying
the same input every hour.

`SCHEMA_VERSION` and `MVB_FORMAT_VERSION` bump independently, on format
changes only. Editing `src/darsay/standalone_verify.py` is an MVB minor
bump: that file is copied byte-for-byte into every `.mvb.tar`.

## License

By contributing, you agree that your contribution is licensed under the
Apache License 2.0, the same license as the rest of the project.

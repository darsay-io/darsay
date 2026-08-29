<p align="center">
  <a href="GETTING-STARTED.md">Start here</a> ·
  <a href="CONCEPTS.md">Concepts</a> ·
  <a href="../examples/README.md">Examples</a> ·
  <a href="README.md">All docs</a> ·
  <a href="../README.md">README</a>
</p>

# Distribution and releases

> **In one sentence.** `pipx install darsay` (or `uvx darsay` with no
> install). Isolated CLI, pure-Python wheel, one runtime dependency.

How people should install darsay, what a GitHub Release contains, and
when (not) to ship a frozen binary.

## What to consume

darsay is published to [PyPI](https://pypi.org/project/darsay/) as a
**pure-Python** package (no compiled extensions). One wheel installs on
every platform that has Python 3.10+:

`darsay-X.Y.Z-py3-none-any.whl`

The only runtime dependency is `huggingface_hub`. Optional extras
(`fast-hash`, `smoke`, `inference`, `datasets`) stay optional. `darsay
run` does not need them — hydration builds its own isolated env.

### Recommended: PyPI, as an isolated CLI

```bash
pipx install darsay
uv tool install darsay
# one-shot, no install
uvx darsay estimate sshleifer/tiny-gpt2
```

`pipx` / `uv tool` / `uvx` are the idiomatic way to consume a Python CLI:
the tool gets its own environment, it does not pollute the user's global
site-packages, and upgrades are one command (`pipx upgrade darsay`,
`uv tool upgrade darsay`). Nothing upgrades itself: `darsay --version`
reports what the isolated environment holds, so after a release the
release machine needs that command too.

### Other sources

Homebrew **tap** (not [homebrew/core](https://github.com/Homebrew/homebrew-core)).
`brew install darsay` will not find it. This is an org tap until the
project is old enough and notable enough for a core formula — do not open
that PR yet.

```bash
brew install darsay-io/darsay/darsay
```

The formula is in [`darsay-io/homebrew-darsay`](https://github.com/darsay-io/homebrew-darsay).
It installs the PyPI sdist into an isolated venv (same idea as pipx).
Hydrate/run still need a real interpreter.

From a downloaded wheel (GitHub Release asset):

```bash
pipx install ./darsay-X.Y.Z-py3-none-any.whl
```

From a git tag or an unreleased commit (not the primary path):

```bash
pipx install git+https://github.com/darsay-io/darsay@vX.Y.Z
uvx --from git+https://github.com/darsay-io/darsay@vX.Y.Z \
    darsay estimate sshleifer/tiny-gpt2
```

### Editable (development)

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[fast-hash,smoke,dev]"
.venv/bin/pytest
```

## GitHub Release contents

Each tagged release (`vX.Y.Z`, matching `darsay.__version__`)
should attach:

| Asset | Why |
|---|---|
| `darsay-X.Y.Z-py3-none-any.whl` | The installable artifact. One file, every OS. |
| `darsay-X.Y.Z.tar.gz` | sdist: source, docs, license. Required for `pip` from git and for auditors. |
| Release notes | `CHANGELOG.md` section for that version, plus the generated commit list. |

Do **not** attach vault bundles, `.mvb.tar` files, or hydrated envs.
Those are archival payloads, not software releases.

The workflow in `.github/workflows/release.yml` runs on a `v*` tag. It
builds the wheel and sdist, attaches them to the GitHub Release, and
publishes the same files to PyPI via Trusted Publishing (OIDC, no API
token). The job uses the GitHub Environment `pypi`, which should require
a reviewer so a tag cannot publish unattended.

## Self-contained binaries

Yes, Python can produce a download-and-run executable. It is **not** the
idiomatic primary distribution for this project, and it is a poor fit as
the only install path.

### What "binary" usually means in Python

There is no compiler that turns an arbitrary Python CLI into a small static
Go-style binary. The working options are:

| Approach | What the user gets | Needs a system Python? | Offline on first run? |
|---|---|---|---|
| **Wheel + pipx/uvx** | Isolated CLI | Yes (or uv, which can fetch one) | After install |
| **zipapp / pex / shiv** | One `.pyz` file | Yes | Yes |
| **PyApp / Hatch app build** | Native stub that bootstraps Python + the package | No, after first run | **No** — first run downloads |
| **PyInstaller / Nuitka onefile** | One OS-specific executable bundling CPython + deps | No | Yes |
| **PyInstaller onedir** | A folder with an executable + libs | No | Yes |

The freeze tools (PyInstaller, Nuitka, cx_Freeze) are the only ones that
produce a true self-contained binary. They work by shipping a Python
interpreter and every imported module. Typical costs:

- **Per-platform builds.** Linux / macOS / Windows (and often x86_64 vs
  arm64) are separate artifacts, signed and notarized on Apple, and
  frequently quarantined by Windows antivirus.
- **Size.** A CLI whose only dep is `huggingface_hub` still packs to tens
  of megabytes because CPython comes along.
- **Startup.** Onefile extracts to a temp dir on every launch.
- **Hidden imports.** Freezers miss dynamically imported modules;
  `huggingface_hub` has several. This is maintainable, not free.
- **Hydration.** `darsay hydrate` / `run` create virtualenvs and
  install `torch` / `transformers` / `llama-cpp-python` into them. A frozen
  binary is not a usable `venv` seed. Hydration already expects a real
  interpreter (`$DARSAY_PYTHON` / `--python`; `uv` can fetch one). A
  freeze that cannot hydrate is an incomplete product; a freeze that also
  bundles a second, unfrozen CPython is a zip of a Python install, which
  uv/pipx already are.

PyApp-style stubs fail a different requirement: an archival tool should
install without a network on air-gapped machines. First-run download of
CPython plus the wheel is convenient for developers, hostile for archivists.

### What we will ship

1. **Now:** a `v*` tag. The release workflow publishes the wheel and
   sdist to GitHub and to PyPI. Consume with `pipx install darsay` /
   `uvx darsay` / `uv tool install darsay`.
2. **Only if a real audience has no Python:** a PyInstaller **onedir**
   (not onefile) for Linux x86_64 and macOS arm64 covering
   `estimate` / `archive` / `verify` / `export` / `import` / `list` /
   `info`. Hydrate/run would require `--python` pointing at a system
   interpreter. That split should be documented on the Release page, not
   papered over. Do not build this until someone actually needs it.

A stdlib-only **standalone verifier** ships as
`src/darsay/standalone_verify.py` and is copied verbatim into every
`.mvb.tar` as `darsay-verify.py` (MVB 1.2). That is a better long-term
binary than freezing the whole CLI: one `.py` file, no deps, runs with
whatever Python exists. See [DESIGN.md](DESIGN.md) and
[MVB-FORMAT.md](MVB-FORMAT.md).

### Why this does not threaten the archive

Longevity is carried by the bundle formats, not the installer. A wheel, a
frozen binary, and a hand-written Python script are all replaceable
readers of the same JSON manifest and uncompressed tar. See
[DESIGN.md](DESIGN.md).

---

[Documentation index](README.md)

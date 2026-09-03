<p align="center">
  <a href="GETTING-STARTED.md">Start here</a> ·
  <a href="CONCEPTS.md">Concepts</a> ·
  <a href="../examples/README.md">Examples</a> ·
  <a href="README.md">All docs</a> ·
  <a href="../README.md">README</a>
</p>

# Testing

> **In one sentence.** Unit and integration tests never touch the Hub.
> Live Hub e2e is opt-in and tiny (`sshleifer/tiny-gpt2`).

darsay's test suite is a pyramid. Fast hermetic tests are the default;
the live Hub path is opt-in and small.

```
              e2e               live Hugging Face Hub, one tiny model
          ------------
       process safety           signals, competing processes, goldens
      ----------------
         integration            fake `test:` provider, real filesystem
    --------------------
              unit              pure functions, tmp files, no network
```

There is no test that talks to the Hub unless you ask for one. That is
deliberate: archive, transfer, verify, and export must keep working when
GitHub Actions cannot reach huggingface.co, and they must stay cheap enough
to run on every commit.

## Layers

| Layer | What it may do | What it must not do | Where |
|---|---|---|---|
| **Unit** | Call one module. Use `tmp_path` for files. Import optional extras and record `skipped`. | Network. Register a provider. Build a full bundle unless the function under test requires one. | `tests/unit/` |
| **Integration** | Drive `archive` / `verify` / `export` / `assemble` / catalogs / the CLI against a `TestProvider` that serves bytes from memory. Touch a temp vault. Stub `_invoke_runner` to prove `run` records without installing torch. | Hugging Face, torch installs, hydration `ensure_env`. Catalog tests stay hermetic (`test:` + fixture JSON). | `tests/integration/` |
| **Process safety** | Launch real CLI processes against disposable `test:` bundles; deliver SIGKILL at deterministic journal/commit boundaries; force lock contention; compare reviewed public-contract goldens. | Network, wall-clock race assumptions, user configuration, or real vaults. | `tests/integration/test_doctor_safety.py`, `tests/doctor_fixtures/`, `tests/golden/` |
| **E2E** | `estimate` → `archive` → `list` / `info` → `verify` → `export` → `import` of `sshleifer/tiny-gpt2`. Asserts `kind`. | Large repos, gated repos, `darsay run`. | `tests/e2e/` |

The fake provider (`tests/fakes.py`) is a real `SourceProvider`. Registering
it is the extensibility check: archive and estimate never import
`huggingface_hub` themselves.

## Invariants the suite is there to keep

These match the list in `CLAUDE.md` / `CONTRIBUTING.md`:

- Payload bytes under `model/` (or `data/`) do not change when the tool
  rewrites metadata.
- `transfer.json` is relocatable: no source-machine absolute paths, a copied
  partial resumes in another vault, a copied lock is reclaimed. An rsync
  of payload bytes into dest, then `archive` / `assemble --handoff`, must not
  re-download those bytes and must not re-hash dest files the ledger
  already marks verified (unless `--rehash`). `darsay mv` / `darsay cp` do
  no less than rsync + verify (+ rm): the copy is re-hashed at the
  destination before the source is removed or the replica is recorded, a
  failed verification leaves the source untouched and registers nothing,
  and a partial is refused (`tests/integration/test_relocate.py`).
- The same bundle state exports to a byte-identical `.mvb.tar` (marker first,
  volatile files excluded, `darsay-verify.py` always the canonical copy).
- Manifests record what was established; unknown is `null`; query caps are
  stored as `query_limit`.
- `import` re-hashes before registering; a failed import writes nothing.
- `README.md` is regenerated; `curation.md` is not overwritten once it exists.
- Hydration is disposable: deleting `hydration.json` never touches the payload.

## Commands

From a checkout, with the project venv:

```bash
.venv/bin/pip install -e ".[dev]"
.venv/bin/ruff check && .venv/bin/ruff format --check
.venv/bin/pytest                         # unit + integration (e2e skipped)
.venv/bin/pytest -m unit
.venv/bin/pytest -m integration
.venv/bin/pytest --run-e2e -m e2e        # or DARSAY_E2E=1
.venv/bin/pytest --cov=darsay --cov-report=term-missing
.venv/bin/pytest tests/unit/test_doctor.py tests/integration/test_doctor.py
.venv/bin/pytest tests/integration/test_doctor_safety.py
```

`--run-e2e` and `DARSAY_E2E=1` are equivalent. CI sets the env var on the
e2e job and runs the hermetic suite on every push and pull request.

The doctor process-safety layer is repository-owned and hermetic. It verifies
real-process SIGKILL recovery before and after target commit, two-process lock
exclusion, repeat-fix idempotence, byte-exact undo, detector repeatability, and
the public capabilities golden. Its contract matrix and known coverage scope
are in `tests/doctor_fixtures/README.md` and `DISCREPANCIES.md`.

To approve an intentional capabilities-contract change:

```bash
UPDATE_GOLDENS=1 .venv/bin/python -m pytest \
  tests/integration/test_doctor_safety.py::test_capabilities_match_golden
git diff -- tests/golden/doctor_capabilities.json
```

Normal test and CI runs never update goldens. A mismatch writes an ignored
`.actual` file, prints a unified diff, and uploads that file from the CI safety
job for review.

## CI

`.github/workflows/ci.yml` on push to `main`, pull requests, and
`workflow_dispatch`:

1. **Lint** — Python 3.12: `ruff check` and `ruff format --check`.
2. **Tests** — Python 3.10, 3.12, 3.14: `pytest -m "not e2e"` with
   `--cov-fail-under=73`, including repository-owned doctor process safety,
   plus a CLI `--version` / `--help` smoke.
3. **E2E** — Python 3.12, cached Hub downloads, `sshleifer/tiny-gpt2`.
4. **sdist and wheel** — build, `twine check`, install the wheel in a clean
   env and confirm runner scripts shipped.

The existing `release.yml` workflow is unchanged: it publishes artifacts on
`v*` tags.

## Adding tests

- New pure helpers go in `tests/unit/test_<module>.py`.
- New doctor repair guarantees need an in-process regression first. Add a
  process-safety case only when a real signal, process boundary, project lock,
  or public contract is the behavior under test. Process tests must use the
  hermetic fixture and deterministic injection points, never timing alone.
- Transfer progress: formatters, the meter, and the TTY panel live in
  `tests/unit/test_progress.py` — including the panel's defenses: fixed-width
  columns across digit rollovers, sparkline cadence, stray-output capture
  (`_LineProxy` and rerouted library `StreamHandler`s), the interrupt
  notice, the offline / reconnecting states, the held ETA, and the final
  record line. Network loss and the rate cap: `Throttle`, `Link`, and the
  reconnect wait are unit-tested with injected clocks in
  `tests/unit/test_transfer.py`; end to end, `TestProvider.fail_next(path,
  *errors)` injects transport failures so integration tests drive a real
  outage → wait → resume through `archive` with `RECONNECT_WAITS_S`
  shortened. The
  Ctrl-C ladder and stop plumbing are in `tests/unit/test_transfer.py`;
  `test_sigint_escalates_across_presses` delivers real SIGINTs and skips
  itself off the main thread. The Hub `tqdm_class` wrapper is in
  `tests/unit/test_huggingface.py`. Integration drives `archive` against
  `TestProvider` and asserts log lines (`DARSAY_PROGRESS=line` so a TTY
  does not flip the test into live mode). The live Hub `tqdm` path is the
  e2e archive of `sshleifer/tiny-gpt2`.
- Anything that needs a bundle uses `TestProvider.add_repo(...)` and
  `archive_quiet` from `tests/integration/conftest.py`. Synthetic payloads
  live in `tests/payloads.py`.
- Records of an earlier schema major: `tests/fixtures/schema-1.8.0/`
  holds manifests (and one export) written by darsay 0.14.10 — the last
  1.x writer — so `darsay migrate` is tested against what an older darsay
  actually wrote, never an imitation. `make.py` there regenerates them
  from a git worktree at that commit; the payloads are rebuilt from
  `tests/payloads.py`. `tests/integration/test_migrate.py` holds a
  migrated record equal to a fresh `archive` of the same source, section
  by section. A future major adds a `schema-2.x.y/` set the same way,
  produced by the last writer of that major before the bump.
- Config files and the free-space floor: an autouse fixture in
  `tests/conftest.py` points `$XDG_CONFIG_HOME` at a nonexistent directory
  and sets `$DARSAY_MIN_FREE=0`, so this machine's
  `~/.config/darsay/config.toml` and its real free disk never reach a test.
  Floor tests opt back in — `min_free=` on `archive`, a vault
  `config.toml`, or a `no_env_floor`-style `delenv` — and fake
  `darsay.transfer.shutil.disk_usage` rather than filling a disk.
- `run` / `hydrate` (non-dry-run) must not install torch. Stub
  `darsay.hydrate._invoke_runner` and write a fake `hydration.json` whose
  `python_executable` exists (`sys.executable`).
- Do not call the Hub from unit or integration tests. If a behavior can only
  be proven against a real snapshot, add it under `tests/e2e/` and keep the
  repo tiny.
- Optional extras (`blake3`, `tokenizers`, `torch`, `pyarrow`) must degrade
  to a recorded `skipped` — tests should assert that, not require the extra.

---

[Documentation index](README.md)

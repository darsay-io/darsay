# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
Tool version (`darsay.__version__`) is independent of
`schema_version` in manifests and of `MVB_FORMAT_VERSION` in exports.

## [Unreleased]

### Changed

- **rsync is a first-class copy.** An out-of-band `rsync` / `cp -a` of a
  bundle (or its payload) into the usual `<vault>/<slug>/<rev>/` layout is
  the same as a darsay copy. The next `archive` / `assemble` /
  `assemble --move` hashes what landed against the pin, fetches only what
  is still missing, and rewrites transfer metadata. It does not re-download
  a file whose digest matches.
- **`assemble --move` shows hashing on the live transfer panel** (percent,
  current file, rate, ETA) so an rsync-then-assemble run does not look
  hung. Dest files already present are not recopied.
- **`assemble --move` into a registered destination** verifies dest
  read-only (payload stays frozen) and skeletonizes the source. `--move` of
  a registered source is refused: rsync the finished bundle, then
  `darsay rm` the source.

## [0.13.0] - 2026-08-30


### Added

- **`hints` on the catalog estimate digest — catalog schema 1.1.0.** The
  CLI now decides, once, at estimate time, which of a closed set of words
  describe a priced source — `gated`, `large` (≥ 20 GiB of priced payload),
  `quant` (a published quantized artifact: mostly-GGUF weights, or a dominant
  safetensors dtype that is not F64/F32/F16/BF16), `subset` (priced with
  `--include`) — and stores them as `estimate.hints`, a sorted list, in
  `catalog.json`. `darsay list CATALOG` grows a HINTS column (hidden when
  every cell is empty, like DESIRE and NOTE), `estimate CATALOG` and
  `catalog add --estimate` print the same words per row, the generated
  catalog `README.md` gets a Hints column, and `list --json` rows carry
  `hints`. Readers of a 1.0.0 file derive `large` / `gated` / `subset` from
  the digest they have; `quant` from a GGUF pack needs one
  `darsay estimate CATALOG`. The schema bump is additive: a 1.0.0 reader
  still loads a 1.1.0 file. One function, `catalog.hints_for`, owns the
  vocabulary and the 20 GiB line; the darsay.io board draws the same line.
- `darsay estimate` records `payload.dominant_format` — the extension
  carrying most of the weight (or data) bytes, e.g. `gguf` — in the live
  estimate. It feeds the `quant` hint and is not stored in the digest.

### Changed

- The SIZE cell of `darsay list CATALOG` no longer carries a `GATED`
  suffix; gating is the `gated` hint in the HINTS column. The catalog
  `README.md` license column likewise drops `(gated)`.
- `save_catalog` writes the schema version this darsay conforms to
  (`1.1.0`) instead of echoing the loaded file's, so a file that carries
  `hints` says so.

- **Archive one pin across two disks that never meet — `assemble --move` and
  skeletons.** When the disk with the bandwidth and the disk with the room
  are never mounted together (a laptop at the café, the big drive at home),
  fetch a half where the network is, hand it over with `darsay assemble
  <partial> --move`, and the source keeps a **skeleton**: the pin and every
  recorded hash stay, the moved bytes are deleted. The source can then fetch
  the *other* half without re-downloading what it carried over — a moved
  file is fetched by no one and registers nowhere. The move is
  verify-then-delete, per file: only a file the destination has re-hashed
  against the pinned upstream digest is released, so a bad copy never costs
  the source its only copy. A skeleton with every file moved out is removed
  (exactly what a plain `mv` would leave); any other reports what it still
  owes or holds. If moved bytes reappear at the source, reconciliation
  re-adopts them — the record is a hint, and matching bytes always win;
  bytes that come back wrong are removed and the record stays `moved`.
- **`moved` is a fourth per-file transfer state**, beside verified / partial
  / missing, entered only through `assemble --move`. `darsay list` shows a
  skeleton's progress as bytes-anywhere and names the moved amount
  (`archiving: 52% (28.9/55.6 GB, 7/22 files verified, 27.8 GB moved out)`);
  `darsay estimate` and the transfer plan grow a `moved` line; `list --json`
  gains `moved_bytes`. An `archive` run with nothing left to fetch here but
  files moved away pauses cleanly (`end_reason: "moved"`, exit 10) with the
  hint to assemble the halves. No change to the manifest, the `.mvb.tar`
  export format, or the catalog schema — `moved` is machine-local transfer
  state, excluded from exports, and never reaches a registered bundle.

## [0.12.0] - 2026-08-30


### Added

- **A full disk is a pause, never a traceback** — `ENOSPC` while writing a
  file, copying a sibling blob, or saving the ledger now ends the session
  exactly as the free-space floor does: `end_reason: "disk"`, exit 10, the
  partial kept, and the "free disk space, then re-run" hint (`disk:
  destination is full — no space left on device while writing
  model-00023-of-00141.safetensors`). The floor also checks headroom
  *before* each file begins — `model-00023-of-00141.safetensors needs
  5.0 GiB more, but only 3.9 GiB is free above the 2.0 GiB floor` — so a
  shard that cannot finish is never started, and the Hub client's own
  per-file "Not enough free disk space" `UserWarning` (two of them, three
  lines each, per shard) is gone with it.
- **The preflight says where it will stop, and asks** — an insufficient
  plan's warning now continues: `the transfer will pause at the
  free-space floor / after about 381.4 GiB more (67 of 140 remaining
  files), roughly 9h at 12.3 MiB/s.` (the pace is the rate cap or the
  ledger's earlier sessions, whichever is slower) and `Free space (or
  move the vault to a larger disk), then re-run to continue.` On a
  terminal `archive` then asks `Continue anyway? [Y/n]`;
  Enter proceeds, `n` pauses cleanly before any byte moves (exit 10,
  `end_reason: "disk"`). `--yes` / `-y` skips the question; pipes and cron
  never see it.
- **Free space on the panel** — while the destination cannot hold the
  rest of the payload, the panel's second line ends in `· free 381.2 GiB`
  (probed every 2 s; the log line carries it too), so the number that
  will end the session is on screen. It goes away once space is freed.
- **`retrying` is a panel state** — the Hub client's own attempts to
  resume a cut stream (up to five, ten seconds apart) now read `retrying`
  in amber with `retry 2 · 23s without bytes` in the tail, instead of a
  bare `stalled`; the first byte back clears it. Providers hand those
  through `transfer_session(on_retry=…)`.
- **Which build ran** — `archive` prints `darsay <version>` as its first
  line, and every ledger session records `"tool": "darsay <version>"`,
  so a pasted terminal or a `transfer.json` identifies the release
  without matching traceback line numbers.

### Changed

- **Steadier ETA** — time remaining is paced by the last five minutes of
  transfer (the rate field still shows the last eight seconds), so a
  day-long estimate no longer twitches with every chunk, and anything over
  a month reads `> 30 days left` rather than `272774d 13h left`.
- **The record line says how it ended** — the dim scrollback line the
  panel leaves behind now closes with `complete`, `paused: disk` /
  `budget` / `offline`, `stopped: Ctrl-C`, `aborted`, or
  `error: <Class>`.

## [0.11.0] - 2026-08-29

### Added

- **Unprefixed dataset ids resolve at pin time** — `darsay archive
  saidutta69/fable-5-premium` (or `huggingface:saidutta69/fable-5-premium`)
  looks up the Hub as a model first; if that repo is missing and a dataset
  of the same id exists, pin rewrites the canonical to
  `huggingface:datasets/saidutta69/fable-5-premium` and archives under
  `data/`. Explicit `datasets/` is never rewritten as a model, and when
  both namespaces exist the unprefixed form stays a model. `estimate`
  and `catalog add --estimate` store the resolved address so overlay
  matches the bundle. `darsay list` prints a TYPE column (`model` /
  `dataset`).
- **Network loss is a panel state, not a stack trace** — when the
  connection drops mid-transfer (laptop leaves Wi-Fi, router reboots,
  DNS goes away, the Hub answers 5xx), `archive` banks what arrived,
  keeps the partial, and waits for the link: the panel's time-remaining
  slot reads `offline` in amber with `retry in 8s · 2 min 10s offline`
  in the tail, then `reconnecting` while an attempt is in flight, and
  the in-flight file keeps its banked bytes on screen. Retries follow a
  2 → 4 → 8 → 15 → 30 s schedule; the first byte that arrives flips the
  panel back and records one scrollback line — `Reconnected after
  4 min 12s (7 attempts).` — with a matching `Network unreachable (DNS
  lookup failed) — waiting to reconnect …` line when it went. Ctrl-C,
  budgets, and the free-space floor keep their meaning while waiting.
  After `transfer.max_offline` (default 1 h; `--max-offline 30m`;
  `0` pauses at the first failure) the session pauses cleanly with
  `end_reason: "offline"`, exit 10, and an "once the network is back,
  re-run" hint. Sessions record `reconnects`; the ledger logs
  `network_lost` / `network_restored` events. Providers classify their
  transport's failures via `SourceProvider.transient_network_error`;
  the Hub plugin covers httpx connect/read/timeout errors, transient Hub
  statuses, and a stream cut short. A host that cannot be reached at
  pin time now ends in one line — `error: cannot reach Hugging Face to
  resolve … — DNS lookup failed` — for `estimate` and `archive` alike.
- **Bandwidth cap** — `transfer.max_rate` in config (`"5M"` = 5 MiB/s),
  `$DARSAY_MAX_RATE`, or `--max-rate 5M` per run (`0` lifts a configured
  cap) paces the whole transfer with a leaky bucket across every worker,
  so an archive can run all day without owning the connection; the
  bucket never banks credit, so the rate holds at the cap from the first
  chunk rather than bursting past it after every pause. The plan
  block prices it — `rate: capped at 5.0 MiB/s — about 40h for the
  remaining 703.8 GiB` — the panel shows `· cap 5.0 MiB/s` in its tail,
  and the Hub client reads in quarter-second chunks under a cap so the
  rate stays smooth rather than bursty.

### Changed

- **Panel defenses** — library loggers that bound a `StreamHandler` to
  the terminal before the panel started (the Hub client does, at
  import) are routed above the panel too, so a warning can no longer
  push panel rows into scrollback and leave a "double panel" behind.
  The Hub client's own retry commentary is filtered during a transfer;
  darsay tells that story itself. When bytes stop, the ETA holds its
  last good estimate instead of ballooning or flipping to `starting`
  until `stalled` is declared; `stalled` is amber. Log mode
  (`DARSAY_PROGRESS=line`) polls every second so outage notices land
  promptly while status lines keep their 10 s cadence.
- **No more tracebacks for users** — an unexpected error ends every
  subcommand as one line naming the darsay frame it came from
  (`darsay: unexpected ConnectError: … (huggingface.py:301)`) with a
  `DARSAY_DEBUG=1` hint for the full traceback; provider errors
  (`SourceError`) print their message and exit 1.

## [0.10.0] - 2026-08-28

### Added

- **Free-space floor** — `archive` pauses cleanly when the destination's
  free space drops below a floor (default 2 GiB) instead of running an
  unattended machine into a full partition. The floor is checked at the
  same points as byte and time budgets, probed at most every 2 s, and
  stops every worker once tripped. The session records
  `end_reason: "disk"`, the CLI exits 10 with a "free disk space, then
  re-run" hint, and the same command resumes from verified and partial
  bytes. `--min-free SIZE` overrides per run (`0` disables). The plan
  line and `estimate` price the floor in:
  `needs 40.0 GiB, free 45.0 GiB (10.0 GiB floor) — INSUFFICIENT`.
- **Config file** — machine-local settings in TOML:
  `~/.config/darsay/config.toml` (`$XDG_CONFIG_HOME`, or `$DARSAY_CONFIG`)
  and `<vault>/config.toml`, which travels with an archive drive. Layers
  resolve default → user file → vault file → `$DARSAY_MIN_FREE` → flag.
  Unknown keys warn so a typo cannot silently disarm a guard; unknown
  tables are ignored so a newer darsay's vault config still loads.
  `darsay config` prints the effective values and which layer set each
  (`--json` for scripts). Config is operator preference, never archival
  fact: it lives outside bundles and is never exported. See
  [Incremental transfer](docs/INCREMENTAL.md#6-session-budgets).

## [0.9.0] - 2026-08-27

### Added

- **Estimate download panel** — `darsay estimate` draws a download block
  in the live transfer panel's look: the pinned total as the headline, a
  bar of banked bytes (verified, size-matched unverified, provider
  `.incomplete` partials, missing), and a disk verdict that prices only
  the remaining network bytes plus scratch. A registered bundle, an
  in-progress `transfer.json`, or a ledger-less payload all classify the
  same way as reconciliation, without writing or hashing. `--json` gains
  a `transfer` section and `bundle.state`; the catalog digest is
  unchanged. See [Incremental transfer](docs/INCREMENTAL.md).

### Changed

- **Live archive panel** — the TTY panel keeps its columns still across
  digit rollovers, repaints in place (no flicker), captures stray Hub /
  logging lines above the panel instead of tearing through it, suppresses
  the terminal's `^C` echo while live, and leaves one dim record line in
  scrollback on stop. The rate sparkline advances one cell per ~5 s
  instead of one per chunk callback.
- **Ctrl-C** — first press requests a clean stop (panel shows "stopping",
  current chunk is banked, CLI exits 10 with the resume hint). Second
  press aborts a stalled connection or in-flight hash and still pauses
  cleanly. Third press hard-kills after restoring the cursor. Queued
  small-file downloads are cancelled on stop instead of drained. See
  [Getting started](docs/GETTING-STARTED.md).

## [0.8.0] - 2026-08-27

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

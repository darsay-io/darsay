# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
Tool version (`darsay.__version__`) is independent of
`schema_version` in manifests and of `MVB_FORMAT_VERSION` in exports.

## [Unreleased]

### Added

- **GitHub is the second source provider.** `darsay archive github:owner/repo`
  (`gh:owner/repo`, or a github.com repository URL) pins one commit,
  lists its tree, and fetches every blob with the standard library alone —
  no new dependency. Each file carries the git blob SHA-1 the tree names,
  so `verify` has an upstream expectation for every byte; a Git LFS
  pointer is resolved to the object's SHA-256 and true size at pin time,
  and the object, never the pointer, is what lands. `HEAD` is the default
  revision (the repository's default branch, whatever its name);
  `--revision` takes a branch, tag, or commit, and a URL that buries one
  (`…/tree/v1.2`, `…/commit/…`) is refused with the command that says it
  plainly. `GITHUB_TOKEN` / `GH_TOKEN` read private repositories and lift
  the API allowance; an exhausted allowance is a refusal naming the reset
  time. A tree GitHub cannot list in one call is refused, never pinned
  partially. [Sources](docs/SOURCES.md).
- **Code is the third artifact type.** A repository is archived as a
  `code` bundle: a source tree at a pinned commit under `code/`, with the
  same anatomy as a model or dataset bundle — manifest, `SHA256SUMS`,
  README, `curation.md`, export, import, `mv` / `cp`, `verify`. The
  record's `code_metadata` carries what upstream said (description,
  homepage, topics, languages by bytes, default branch, whether upstream
  had already archived it, the submodules and symlinks the tree names)
  and, read from the inventory, which standard build/run files the tree
  holds — `container`, `compose`, `python`, `node`, `rust`, `go`, `nix`,
  `make`, `env_template`, `shell` — evidence of what the tree can do,
  never a verdict on what it is for. Lineage records a fork's parent as
  upstream declares it and the fork count at archive time. `estimate`
  prices a repository and prints those facts before anything is fetched;
  `hydrate` / `run` refuse a code bundle by name; `smoke` records nothing
  for it. Manifest schema 2.2.0, additive. [Code bundles](docs/CODE.md).

### Changed

- The `To archive:` line `estimate` prints adds `--revision` only when the
  ref is not the provider's default — `main` on Hugging Face, `HEAD` on
  GitHub — instead of comparing every provider against `main`.
- `estimate` and `info` on a non-model bundle name the bundle's type in
  the lines that do not apply to it (`engines`, RAM) instead of saying
  "dataset" for everything that is not a model.
- AGPL-3.0 joined the license table, so an AGPL repository's rights flags
  are recorded instead of marked for manual review.
- `info` omits the smoke line for a bundle that has no smoke tests.

## [0.14.16] - 2026-09-05


### Added

- **Whole-drive verbs — `verify`, `mv`, and `cp` take more than one bundle.**
  `--all` acts on every registered bundle in the vault, the way a whole drive
  is actually moved or attested. `verify --all` re-hashes each and exits
  non-zero naming any that failed; `cp --all VAULT` / `mv --all VAULT` copy or
  move the lot, each verified at the destination before the source is kept or
  removed. Naming bundles and passing `--all` together is refused, so the two
  ways never half-combine.
- **`darsay list <other-vault>` — what a drive holds that this vault does
  not.** It overlays another vault — a mounted drive — against this one,
  read-only: each bundle on the drive shows as `new` (not here), `have`
  (here, same bundle hash), `differ` (here, bytes differ), or `partial`.
  `--ids` prints the actionable set (everything not already identical here),
  for piping into `mv` / `cp`.
- **The vault the working directory is in.** With neither `--vault` nor
  `$DARSAY_HOME` set, darsay now finds the vault the working directory is in
  — a git-style walk up to the nearest ancestor that holds darsay bundles —
  before falling back to `~/darsay`, so `cd /Volumes/drive && darsay list`
  reads the drive. The home directory and above are never treated as a vault,
  and the chosen vault is announced.
- `darsay list` now points at bundles on disk that are not in the
  `<name>/<revision>` layout it reads — a bundle dragged onto a drive by
  hand at the wrong depth, which used to be invisible. It is an advisory
  footnote naming each path and the `cp` that places it; the canonical walk
  is unchanged, so nothing acts on a loose bundle automatically.
- `mv` / `cp` refuse before copying when a payload file is larger than the
  destination filesystem can hold — a model shard onto a FAT32 stick, which
  otherwise fails mid-copy with a cryptic errno. The refusal names the
  filesystem, the files over the 4 GiB limit, and the fix (reformat as
  exFAT). exFAT and every modern filesystem are unaffected.

### Changed

- An explicitly named vault that does not exist is reported as such, and
  a removable disk that is not mounted is named as the likely cause:
  `list`, `du`, `verify`, and `doctor` say so instead of `no bundles` /
  `no bundle matching`, and `mv` / `cp` refuse to copy onto an empty stub
  where a volume should be mounted (the folder an eject leaves behind),
  which would silently fill the boot disk. The default `~/darsay` missing
  on first run is still a normal empty vault, not an error.
- The FAQ gained the sneakernet routines these verbs are for: moving,
  copying, or attesting a whole drive at once; reading a mounted drive by
  `cd`-ing into it; asking a drive what it holds that this vault does not;
  and the two refusals above (an unmounted mount point, a FAT32 stick).

### Fixed

- A bundle archived on this machine and never moved is no longer asked to
  re-verify because the hostname flapped: `migrate`, `verify`, and
  `doctor` compare a stable machine name (the hostname's first label, so a
  macOS `.local` / DHCP suffix does not matter), settable with
  `$DARSAY_MACHINE_ID`. Older records that stored a full hostname still
  match the same machine.
- `mv` / `cp` now flush the copied payload and the rewritten record to
  the medium before printing that the copy is verified, and the manifest
  is written atomically (temp, fsync, rename, fsync the directory). On a
  removable disk the verification read and the success line could both come
  from the page cache, so a drive pulled right after "Copied … verified"
  could keep a record that said verified over bytes that never landed;
  fsync closes that window across a same-filesystem rename too.
- Every pasted next-step command that names a bundle by id now carries
  `--vault` when the vault is not the default one, so `migrate`'s verify
  line, `info`'s hydrate hint, `archive`'s completion and already-exists
  hints, and `hydrate`'s run hint resolve from where they are pasted. A
  bundle named by a path prints the path, which needs no vault.
- `darsay migrate` on a bundle addressed by a path outside the vault — an
  arrival migrated where rsync left it, as the refusal's own hint spells
  it — ends with `darsay verify <that path>`. The id it printed before is
  a search of the vault, so the pasted command answered `no bundle
  matching`. `info` names such a bundle the same way in its hydrate hint.

## [0.14.15] - 2026-09-05


### Added

- An interactive collection room for fresh multi-variant GGUF archives:
  explicit 4-bit, 4+8-bit, and whole-publication starting points; complete
  shard groups; separate projector choices; live disk totals; field notes;
  and pinned-scope review before archive creation. Board and terminal share
  the guidance. No quality, hardware-fit, or recreation-cost claim is inferred
  from an encoding label.

### Changed

- The whole publication has no selectors. `/*` — typed, chosen in the
  collection room, or carried by a board row — is the repository itself, so
  it shares the identity, size basis, and resume behaviour of an unqualified
  archive and the pin records no subset for it. `--full` remains the
  retention switch that keeps hash-identical duplicates too.
- The terminal collection room fits an ordinary 80×24 terminal: eight groups
  at a time with a count of what lies above or below, messages wrapped instead
  of clipped, a compact key bar on narrow terminals, a side guide that says
  when `?` holds more, and a step indicator that lights the current step.
- `--force` names what it would remove before it re-pins: every payload file
  outside the new scope, with its size. An interactive run asks first, a dry
  run lists them and removes nothing, and the collection room opens with the
  existing pin selected and says what a different choice discards.
- Direct-source archive reruns resume the pinned collection without repeating
  its include patterns. Board/catalog jobs still require their row's identity.
  Explicit includes, `--full`, shards, non-interactive runs, and `--yes` bypass
  the picker. `--yes` uses the default archive policy, not a default quant.
- Conflicting explicit includes are refused instead of being ignored in favor
  of another pin's scope. This protects reviewed collections and board jobs,
  including a pin created by another process while the picker was open.
  `--force` deliberately re-pins partial as well as completed collections.
- Automatic omission requires hash-identical content for every omitted file retained
  in the same bundle. GGUFs beside candidate source weights and weights
  matching a remote base remain retained; a vault address match is not
  recovery evidence. Classified selections preserve every retained support
  path, including files outside the sidecar list. Existing pins remain frozen;
  use `archive --force --full` when a complete new repository pin is intended.
- Collection guidance distinguishes scope, artifact identity and lineage,
  recovery evidence, and the retention decision. Negative/print labels do not
  imply originality, irrecoverability, or permission to omit. The GLM case
  study compares the 2.315 TiB Unsloth pack, 305.8 GiB publisher FP8 release,
  and 186.0 GiB selected Q4 variant.
- Catalog schema 3 records whether a size measures the repository,
  an explicit selection, or a classified archive, with separate repository
  totals, classification summaries, and GGUF variants. Catalog readers accept
  schema 3 only; recreate earlier catalogs from their source rows and refresh
  their estimates. Existing model payloads need no re-download.
- GGUF parameter counts use Hub metadata. Variant sizes sum every shard;
  projectors are separate. Multi-variant packs and incomplete selections do
  not produce a per-model bytes-per-parameter or memory estimate.
- Classification keeps or skips a complete GGUF shard group together.
  Hydration selects its first shard and refuses incomplete groups. Estimates
  respect the selection frozen in an existing archive pin.

## [0.14.14] - 2026-09-03


### Added

- **`SHA256SUMS` — verify a bundle with coreutils alone.** Every bundle
  now carries the payload's hash list at its root in the format
  `sha256sum` has read for decades: one `<sha256>  <path>` line per
  payload file, sorted by path. `sha256sum -c SHA256SUMS` from the bundle
  root checks the bytes with no darsay and no Python; `sha256sum
  SHA256SUMS` prints the manifest's `inventory.bundle_hash.value`,
  because the bundle hash has always been the SHA-256 of exactly that
  text — the list is bound to the record by one command and the bytes to
  the list by the other. It is a derived view: `archive` writes it,
  `regen`, `verify`, and `migrate` rewrite it, `export` generates it into
  the tar from the record (**MVB format 1.3**; an on-disk copy is ignored
  like `darsay-verify.py`), `import` unpacks it, and `darsay doctor`
  reports one that is missing or does not match the inventory
  (`bundle.sha256sums`, a stat-free check that runs under `--quick`) and
  regenerates it under `--fix`, journaled and undoable like the README.
  `darsay-verify.py` stays as the complete check when Python is present.
- **`[host]` — hash on the machine that owns the disk.** A vault's own
  `config.toml` can name the machine that owns its disk and the vault's
  path there (`[host] ssh = "root@nas"`, `path = "/volume1/darsay/vault"`;
  `darsay --vault VAULT config host.ssh=… host.path=…` writes it, keeping
  comments). With it, `verify`, `mv`, and `cp` hash the payload on that
  machine over one ssh call: a fifteen-line POSIX `sh` script goes over on
  stdin, the machine hashes the files where they are with whichever of
  `sha256sum`, `shasum`, `sha256`, or `openssl` it has, and one line per
  file comes back — no Python, rsync, or darsay needed there, so a
  Ubiquiti or Synology box qualifies. A landing hashes twice there, the
  files already present and then the copies; a fresh copy hashes its
  staging directory by its path on the host. The plan says `hash: on
  root@nas` and the network warning is gone. A machine that is down or
  has no sha256 tool is a refusal naming the fix, never a silent fall
  back to reading the mount. `[host]` is read from the vault file alone;
  a user file that sets it is warned about and ignored. The reconcile
  pass `archive` and `assemble` make over what a destination already
  holds — adopting an rsync'd `model/`, `--rehash` — hashes there too:
  one call for the LFS files and one, with the git blob sha1, for the
  files git holds itself, so the check against the pin is the same one a
  local pass makes; assemble's plan says `hashed on root@nas` and its
  network warning goes away. A host that returns nothing for a file the
  mount shows is refused as a wrong `host.path`, never verified. Without
  `[host]`, the network warning now ends with the `config` line to run,
  the host prefilled from the SMB or NFS mount source when it can be
  read.
- `darsay config KEY=VALUE …` writes the vault's `config.toml`; `-n`
  prints what it would write. `darsay verify` and the landing pass
  announce each file over 256 MiB as it is hashed, here or there.
- **The network-mount warning ends with the commands to paste.** When a
  `mv` / `cp` destination is on SMB or NFS, the plan names the vault,
  says what the wire would carry — two trips of the payload for a fresh
  copy, one read of the files already there for a landing — and prints
  the local way with both paths filled in: the rsync line (`-aP`,
  `--exclude` for `hydration.json`, `transfer.lock`, and `.DS_Store`,
  trailing slashes on both sides, never `--delete`), `darsay verify` on
  the host that owns the disk, and for `mv` the `darsay rm` here. With
  the bytes already there the rsync carries only the record. The verb
  then continues over the wire and says that Ctrl-C leaves the source
  untouched; `-n` shows the same lines and continues nothing.

### Changed

- **`darsay verify` records where it ran.** A pass or a fail is a fact
  about bytes at a path on a host, so `archive.location` and
  `archive.host` become where the payload was read, `README.md` is
  regenerated when they changed, and the verb prints the old and new
  location. An rsync'd copy verified where it landed now carries a true
  location without a `mv`; a bundle verified in place changes nothing.
  Each `verification.json` entry carries `location` and `host`, and
  `VERIFICATION.md` prints them.
- Every rsync line in the docs is the one the warning prints, and the FAQ
  answers whether it should use `--delete`: no, and why.

## [0.14.13] - 2026-09-03


### Breaking

- **`darsay mv` and `darsay cp` land on a destination that already holds
  the bundle; `--force` is gone.** A copy already at `<vault>/<name>/<rev>/`
  — an rsync made earlier, a backup being refreshed, a partial someone
  started there — is a landing site, not a conflict. Every payload file
  there at its recorded size is hashed in place and kept when it matches;
  only what is missing, at another size, or hashes wrong is copied; the
  bundle's own record, views, notes, and ledger replace the copy's (a
  `hydration.json` there is that vault's own and stays); the verification
  is recorded; then the source is removed (`mv`) or the replica recorded
  on both sides (`cp`). This is the *rsync is a first-class copy*
  invariant applied to the verbs: rsync, then `darsay mv`, reads the
  destination once and copies nothing that matched — where before it
  refused, and `--force` threw the rsync'd bytes away and copied them
  again. The copy's record is never read, so an rsync made before a
  `darsay migrate` lands fine. The plan says `(exists)` and how many
  files are already there; `archive.moves` records `method: adopt` with
  `adopted` and `copied` counts and, for files that were there but hashed
  wrong, `replaced`. What a landing refuses: a destination holding payload
  files the record does not list — another pin of the same revision — is
  named, with `darsay rm` for either side. When the destination's
  `curation.md` differs from the bundle's, the plan says so before
  anything moves; the bundle's lands. Recovery: drop `--force` from any
  script — a second `cp` refreshes a backup by itself.

### Changed

- **`darsay migrate` sends only arrivals to `darsay verify`.** A record
  whose last verification passed at this path on this host — a bundle
  archived here and never moved — still stands after migration, and the
  `payload:` line and the closing `done:` line say so with the date.
  `next: darsay verify …` is printed for a record whose last pass was
  somewhere else (a bundle that arrived by rsync) or whose payload no
  longer matches the record by path and size; under `--all`, only those
  are listed. Migration never touched the payload, so the record's own
  verification is as true after it as before.
- `verify.py` is two steps — `hash_payload` reads the payload where it
  lives, `record_verification` writes the outcome — so `mv` / `cp` can
  record their single landing pass without hashing anything twice.
- **A docs page no longer breaks the website after the tag.** The release
  gate reads the docs' links: every relative link in `README.md`,
  `CONTRIBUTING.md`, `examples/README.md`, and every `docs/*.md` — Markdown
  and HTML both — must name a file that is here. The page list is globbed,
  so a page added today is checked today. `tests/unit/test_release_script.py`
  runs the same check on every CI run.
- The two docs *flag* checks now cover a page list that is total rather
  than partial. `CLI_DOCS` gained `docs/DOCTOR.md` and `docs/CATALOGS.md`
  (user-facing pages that had never been added), and every remaining page
  is named in `UNCHECKED_DOCS` with its reason. A `docs/*.md` page in
  neither list stops the release: those checks read a page as "every darsay
  flag named here is live", which is wrong for a page that names a flag to
  say it does *not* exist — a non-goal, a labelled proposal, another
  program's command line — so the judgement is per page, and the gate asks
  for it once, by name, instead of letting a page drift in unchecked.
- CI gained a **Docs site transform** job (`.github/workflows/docs-site.yml`,
  reusable): it checks out `darsay-io/website`, runs that repository's real
  `sync-docs.mjs` against this checkout, and then its suite and build. It
  runs on every pull request, and `release.yml` now `needs:` it, so nothing
  is published unless darsay.io could have published the docs shipped with
  it. That site now derives its page list from `docs/*.md`, so a new page
  publishes itself; what this job catches is an unresolvable link or a
  renamed heading, which used to fail hours after a release, in the other
  repository, while darsay.io served the previous release's docs.

## [0.14.12] - 2026-09-03


### Added

- **`darsay migrate` — a record moves forward.** A bundle whose
  `manifest.json` was written under an earlier schema major (any 1.x)
  is re-read under the current model and rewritten in the current
  shape — offline, from the record and the payload: family, generation,
  member, variants, formats, and size re-read from the name; precision,
  `precision_detail`, and bytes per parameter re-derived from
  `config.json` and the weight headers; `relationships` translated into
  `lineage` with each parent's relation and provenance (from the
  archive-time card when `transfer.json` travelled with the bundle,
  else from the record and its upstream tags); the subset policy and
  verdicts renamed `negatives` / `negative`; everything else carried as
  recorded, unknown top-level keys included. No payload byte is touched
  or re-hashed — `verify` is the next line the command prints. `-n`
  prints, per section, what the record will say and where that came
  from; `--all` walks the vault; `--json` is for scripts. Manifest
  schema **2.1.0** (additive): `archive.migrations` records each move
  as `{at, from_schema, to_schema, darsay}`.
- Every refusal of an older record ends with the `darsay migrate` line
  to paste. `darsay list` marks such bundles `migrate` (schema in NOTE,
  a count in the header); `darsay doctor` reports them as a
  `bundle.manifest` warning with the command; `darsay import` verifies
  an older `.mvb.tar`'s payload against its embedded record and
  migrates the record as it lands. A record of a newer major is still
  refused everywhere — a record does not move back.
- `tests/fixtures/schema-1.8.0/` — records and one export written by
  darsay 0.14.10, the last 1.x writer, regenerable by `make.py` from a
  worktree at that commit. `test_migrate.py` holds a migrated 1.8.0
  record equal to a fresh `archive` of the same source, section by
  section.

### Changed

- The fix-forward invariant, restated: readers still read one major and
  never read around an older record; the recovery for a breaking schema
  change is `darsay migrate`, not a re-archive. `migrate.py` is the one
  place an older major is read. (`docs/NORTH-STAR.md`, `CLAUDE.md`,
  [MANIFEST.md → Migration](docs/MANIFEST.md#migration).)

## [0.14.11] - 2026-09-02


### Breaking

- **Negatives and prints, one vocabulary.** *Master* is gone from every
  surface. Classification verdicts are `negative | print | support |
  unknown`; the archive default is the `negatives` policy;
  `source.subset.policy` and the catalog digest's `policy` read
  `"negatives"`; `estimate` says `negatives:` where it said
  `masters-first:`; `classify` prints `negative`. Recovery: none needed
  for behavior — the rules are unchanged — but any script matching the
  old words must match the new ones.
- **Manifest schema 2.0.0.** `identity` now carries `family`,
  `generation`, `member`, `variants`, `formats`, `size`, and
  `read_from: "name"` (the name grammar) and no longer carries `version`
  or the architecture-as-family reading; `relationships` is replaced by
  `lineage` (`parents` with relation and provenance, `descendants`,
  curator `successors` / `related`, `as_of`, `query_limit`);
  `model_metadata.precision` is the release-precision label, with
  `precision_detail` and `bytes_per_param` beside it. A 1.x manifest is
  refused by `info`, `verify`, `regen`, `run`, and `export`, and a 1.x
  `.mvb.tar` does not import: re-archive the source (sibling-blob reuse
  makes a re-archive of a still-present payload zero-network). `list`
  still walks 1.x bundles, since it reads only the inventory.
- **Catalog schema 2.0.0.** The digest gains `precision`,
  `bytes_per_param`, `architecture`, and `parents`; the status `unknown`
  is now `closed`; `overlay_stats` reports `closed`. A 1.x `catalog.json`
  is refused with a re-add hint. The one live darsay.io board is ported
  by one `darsay estimate <board-url>` after the site deploys.
- **`SourceProvider.relationships()` is `lineage()`** and returns the
  manifest's `lineage` section.

### Added

- A board's page address with `.json` — `https://darsay.io/b/<id>.json`,
  the board as a document, the address darsay.io hands a program — is
  accepted wherever a board URL is (`estimate`, `list`, `archive --next`
  / `--board`, `catalog add` / `drop` / `adopt`).
- `docs/NORTH-STAR.md` — the mission and the model of the models: work,
  negative and print, precision, lineage — and the principle that every
  label is a doorway. `docs/proposals/lineage-and-precision.md` records
  the design.
- **Precision on every model.** `estimate` prints `precision:` — the
  release precision (`BF16`, `FP8`, `MXFP4`, `AWQ INT4`, `Q4_K_M`) from
  `config.json`'s `quantization_config` (wherever a multimodal config
  nests it), the dominant dtype, or a GGUF-only repo's file names — and
  the measured bytes per parameter with a plain reading of it. Packed
  dtypes are marked in the `parameters` line; counts print in T above a
  trillion. The catalog digest and the board carry both facts.
- **Lineage, read from names and declarations.** `lineage.py` reads
  family, generation, member, variants, formats, and size from a work's
  name by a documented grammar (shared with darsay.io through
  `tests/fixtures/lineage-names.json`); parent edges come from the card
  and the Hub's `base_model:*` tags with their relation and provenance.
  `estimate` prints `family:`, `architecture:`, and `lineage:`; `list`
  shows FAMILY and PRECISION columns and `--sort family` reads a catalog
  as the tree; the catalog README draws a **Families** section; bundle
  READMEs have a Lineage section.
- **Closed works.** `catalog add` accepts a home URL on a host with no
  provider — an API-only model, an announced release — as a `closed`
  row: no price, nothing to fetch, its place in the family held. A
  closed work refuses `--revision` / `--include`; `estimate CATALOG`
  leaves closed rows in place and says so.
- `estimate`'s `negatives:` line says what the price is made of when
  nothing is skipped — how many negative sets, and how many sets darsay
  would not guess about — instead of a bare "the whole repo".

### Fixed

- A weight index larger than 10 MiB no longer turns a repo's only weight
  set into `unknown`: the JSON read cap is 64 MiB, matching the GGUF
  header cap (Kimi-K3's `model.safetensors.index.json` is 60 MB).
- tiktoken vocabularies (`tiktoken.model`, `*.tiktoken`) satisfy the
  tokenizer completeness rule, and a `custom_code` model's Python rides
  along as a sidecar of any `--include` subset.
- A `quantization_config` nested under `text_config` (multimodal
  configs) is found by both the classifier and the precision facts, so
  a native MXFP4 release classifies as a quantized negative rather than
  a full-fidelity one, and the newer `dtype` config key is read beside
  `torch_dtype`.

## [0.14.10] - 2026-09-02


### Fixed

- `archive --next <board-url>` no longer re-fetches a row the board
  already checks off as have. Board status never enters `catalog.json`,
  so the local overlay alone would still want a row someone else finished
  or checked off by hand; `--next` now reads the board's own status and
  skips those rows, and the idle message says how many were had vs
  claimed. Deliberately re-fetching a have row is still one command away:
  name the source with `archive SOURCE --board <board-url>`.

### Changed

- Board claims no longer default to the machine's raw hostname. The
  default client id is now a stable pseudonym derived from a hash of
  hostname + user (e.g. `amber-heron-3f`): a board URL travels, and the
  hostname of who holds it should not travel with it. `board.client` in
  config still wins, exactly as before.
- `archive SOURCE --board <board-url>` marks its claim as a deliberate
  re-fetch (`refetch`), and prints a note when the named row is already
  checked off as have. Boards that enforce the new claim contract refuse
  un-marked claims on have rows, which stops out-of-date `--next` clients
  from re-downloading what the group already holds.

## [0.14.9] - 2026-09-01


### Added

- `darsay mv BUNDLE VAULT` — move a registered bundle into another vault.
  Same filesystem: a rename. Otherwise: copy into a staging directory,
  re-hash every payload file there against the manifest, stamp the new
  `archive.location`, rename into place, and only then remove the source;
  a failed verification removes the copy and leaves the source untouched.
  Refuses partials (naming `assemble --handoff`), a destination vault that
  does not exist (an unmounted disk is not a vault), and an existing
  destination without `--force`. `hydration.json` and `transfer.lock` stay
  behind; everything else travels. `-n` previews. rsync into
  `<vault>/<name>/<rev>/` remains a first-class copy — `mv` is that
  contract as one verb, never a requirement.
- `darsay cp BUNDLE VAULT` — a verified second copy of a registered bundle
  in another vault: copy (copy-on-write clones where the filesystem offers
  them), re-hash at the destination, rename into place, keep the source.
  Both manifests record the replica (`archive.replicas`, `backup_status:
  replicated`); a `--force` refresh updates the entry rather than adding
  one. Same refusals and preview as `mv`.
- Manifest schema 1.8.0 (additive): `archive.moves`, a list of
  `{at, from_location, method}` written by `mv`; `archive.replicas`
  entries `{at, location, host}` and `backup_status: replicated`, written
  on both sides by `cp`.
- `docs/FAQ.md` — moving bundles, rsync's standing, `mv` versus
  `assemble --handoff`, what travels and what stays.

### Changed

- `assemble --move` is now `assemble --handoff`, and the per-file ledger
  state `moved` is `handed_off` (`moved_at` → `handed_off_at`; events
  `handoff_out` / `handoff_in`; `archive` pauses with `end_reason:
  handed_off`; `list` says `handed off`, `list --json` reports
  `handed_off_bytes`; the plan line reads `handoff:`). *Move* now means
  one thing — a registered bundle changes vault (`mv`) — and *hand-off*
  the other: a partial's verified files cross vaults one by one, leaving a
  skeleton. The mechanism is unchanged. A skeleton ledger written before
  this release carries `moved` records; they are treated as missing and
  re-fetched — slower, never lossy.

- `darsay archive SOURCE --board <board-url>` — you pick the source,
  the board still gets the claim and the progress gauge. A source with
  no matching row archives unclaimed with a warning; a row another
  client holds is refused with the holder named. `--next` remains the
  let-the-board-pick form.

## [0.14.8] - 2026-08-31


### Added

- Classification rule R15: a weight set byte-identical, file for file
  (LFS SHA-256), to another kept set in the same repo is a print and is
  skipped by default — the strongest print there is, bit-recoverable
  from the twin the bundle keeps. Multi-pipeline repos ship the same
  60 GiB text encoder three times; MiniMax-H3 drops from 464 GiB to
  330 GiB with zero information lost. Identical sets count once toward
  GGUF source ambiguity.
- Include patterns accept a leading `/` to anchor at the repo root and
  disable the filename fallback — how a selection keeps a root
  `model.safetensors` and not its byte-identical twin in a subdirectory.
  Policy selections fall back to anchored patterns automatically.

### Fixed

- `archive --next <board-url>` hands its claim back when the archive
  refuses before transfer starts (a gated repo, a bad revision) instead
  of leaving the row claimed until the 24 h staleness expiry. A clean
  pause and Ctrl-C still keep the claim — those runs resume.

## [0.14.7] - 2026-08-31


### Changed

- Catalog and board refresh is livelier and faster: every row announces
  itself before its work (`[n/total] source ...`), each network read
  ticks a dot on the open line, and classification header files are
  read concurrently (8 workers) instead of one at a time.

## [0.14.6] - 2026-08-31

### Fixed

- Board requests identify themselves (`User-Agent: darsay/<version>`):
  Cloudflare's Browser Integrity Check bans the default Python-urllib
  signature outright (403, error 1010), which broke every board round
  trip against darsay.io.
- A push to a board whose site predates catalog import (the old worker
  answered `POST catalog.json` with the download itself) is now a clear
  error naming the missing deploy, not a phantom "Pushed" success.

## [0.14.5] - 2026-08-31


### Added

- **darsay.io boards are catalog addresses.** A board URL
  (`https://darsay.io/b/<id>`) now works wherever a catalog slug or path
  does: `darsay estimate <board-url>` fetches the board's catalog,
  refreshes every row with classification, and pushes the result back —
  one command keeps a board's prices, hints, and masters markers honest;
  `catalog add`/`drop`/`adopt` round-trip the same way; `darsay list
  <board-url>` overlays the board against the local vault read-only.
  The board URL is the capability — treat it like a secret.
- **`darsay archive --next <board-url>` claims its row.** The client
  (config `board.client`, default the hostname) claims the board's next
  unfinished row before fetching — rows another client holds a live
  claim on are skipped — and reports progress at archive boundaries
  (start, clean pause, done), which the board renders as a progress
  gauge. Reporting done flips the row to have and teaches an empty
  holders field the client id; a dry run hands its claim back. Claims
  are board-side coordination and never enter catalog.json.
- Config gains `board.client` (`DARSAY_BOARD_CLIENT`).

## [0.14.4] - 2026-08-31


### Breaking

- **`darsay archive` of a model repo is masters-first by default.** It
  was: fetch the whole repo. It is now: classify the repo's weight files
  from bounded header reads and fetch the masters, everything
  unclassifiable, and all support files, skipping mechanically derivable
  prints (a GGUF with no imatrix beside exactly one full-fidelity weight
  set; files byte-identical to an archived base). Every skip is named in
  the preflight and recorded in the manifest with the full omitted
  inventory and hashes. Recover the old behavior per run with
  `darsay archive <source> --full`. Existing pins are untouched:
  re-runs resume whatever their pin selected.
- **`darsay estimate` and stored catalog digests price the default
  acquisition, not the shipping box.** A fresh model source is
  classified the same way archive classifies it; `estimate --full`
  prices the whole repo. Catalog refresh (`darsay estimate CATALOG`)
  classifies model rows under a recorded read budget (256 requests /
  512 MiB per run; rows past it price the full repo and say so).
  Re-run `darsay estimate <catalog>` once to re-price existing boards.

### Added

- `darsay classify SOURCE` — per-weight-set master/print verdicts with
  rule ids, evidence, a read receipt, and the exact selection
  (`--revision`, `--include`, `--json`). Refusal is a finding: what
  darsay cannot establish is `unknown` and fetched, never guessed.
- Manifest schema 1.7.0 (additive): `source.subset.policy` and
  `source.subset.classification` record a masters-first selection and
  its rationale.
- Catalog schema 1.2.0 (additive): the digest gains `policy`; the hint
  vocabulary gains `redundant` (priced weight bytes ≥ 1.75× one copy at
  the published per-dtype parameter counts). Estimate prints the
  redundancy ratio when it fires. A null-include catalog entry is
  satisfied by a masters-policy bundle; `list` shows such bundles as
  `[masters]`.
- `SourceProvider.read_bytes` — bounded remote byte-range reads,
  implemented for Hugging Face (Range requests via the Hub session) and
  the hermetic test provider; the base default degrades gracefully.
- `gguf_meta` (stdlib GGUF KV header parser that leapfrogs bulk numeric
  arrays and enforces a recorded fetch cap) and
  `safetensors_meta.read_header_via` (the same header parse over a
  fetch callable).

### Fixed

- `--include '*.safetensors'` against a sharded repo now keeps the
  weight map: `*.index.json` and `video_preprocessor_config.json` ride
  along as subset sidecars, so the archived subset actually loads.

## [0.14.3] - 2026-08-31


### Changed

- Kept the native release script independent of external orchestration by
  removing its last integration-specific source comment. Repository metadata
  remains the sole optional attachment point, and the script stays directly
  usable on its own.

## [0.14.2] - 2026-08-31


### Added

- **`-n` / `--dry-run` on every command that writes.** `rm`, `export`,
  `import`, `assemble`, `regen`, `run`, `dehydrate`, `envs --prune`, the
  `catalog` verbs, and `estimate CATALOG` join `archive`, `hydrate`, and
  `doctor --fix`. A dry run performs the same checks (and gives the same
  refusals), prints the same report in the conditional — `Would remove:`,
  `Would import …`, `Would assemble into …` — writes nothing, and ends with
  the real command to paste. `assemble --dry-run` prices the copy (files,
  bytes, disk), says what would be hashed at dest, how far along the pin
  would be afterwards, and which source files `--move` would release, and
  creates nothing — not even the destination directory. `archive --dry-run`
  now says when it recorded a pin.
- `rm` lists sizes before asking; `regen` and `catalog regen` report whether
  README.md actually changed (`+3 -1 lines` / `unchanged`); `catalog adopt`
  lists the entries it took.

### Changed

- **Large files are pipelined and streamed.** They used to run
  download → hash → next in strict sequence, one HTTP stream, so the
  network sat idle for every digest pass and a single CDN connection set
  the ceiling. Each large file now moves through a fetch → hash → commit
  pipeline: `--jobs` fetch streams (the same width small files already had,
  default 4), one dedicated hash thread digesting finished files alongside
  (`hashlib` releases the GIL, so the two do not contend), and the ledger
  written only on the main thread as digests finish. If the disk cannot
  keep up, fetching pauses at `--jobs` + 1 files awaiting digest rather
  than running ahead of the ledger. The floor guard counts every in-flight
  stream at the bytes it has yet to land before another file begins
  (`model-00023… with 2 in flight needs 12.0 GiB more …`). Streams share
  the rate cap and the byte budget; a stop leaves up to `--jobs` resumable
  partials. The invariants are unchanged: a digest abandoned by Ctrl-C
  leaves the complete file for the next run's reconcile to adopt, a budget
  lets queued digests finish (they cost no network and no disk), a mismatch
  is discarded and fetched once more with `force`, and an error anywhere
  halts the other streams at their next chunk. Non-live logs gain a
  `Verified …` line per large file, since completion order can now differ
  from start order. `--jobs 1` restores a single stream, still hashing in
  parallel. Why this stays in Python threads rather than a Rust core, and
  where Xet fits: [docs/DESIGN.md](docs/DESIGN.md#transfer-concurrency).

### Fixed

- The live panel no longer reads `stalled` while `archive` digests a file it
  just fetched. When hashing is the only work in flight the time slot says
  `verifying`, the held ETA survives, and the file line shows digest
  throughput (`hashing model-00001… 42.8%  2.1 GiB / 5.0 GiB · 1.4 GiB/s`).
  `assemble` re-hashing dest keeps its ETA, since those bytes move the bar.
- `archive --force --dry-run` no longer deletes the existing bundle's
  `manifest.json` or rewrites its ledger; a forced dry run plans from a fresh
  pin on paper only.

## [0.14.1] - 2026-08-30

A follow-up to the 0.14.0 rsync work. The warning that was supposed to stop
`assemble` from silently pulling a payload back over the wire could not fire
on macOS, and it watched the flag instead of the read.

### Fixed

- **Network-mount detection now actually detects.** The 0.14.0 probe shelled
  out to `stat -f %T`, which on macOS is BSD stat's *file* type specifier —
  it prints `Directory`, never a filesystem name — so the warning could not
  fire in its own headline scenario (`--vault /Volumes/<share>` from a
  laptop), and the Linux table missed `smb2`, what a current cifs mount
  reports. `is_network_filesystem()` replaces it: on macOS it reads
  `mount(8)` and trusts the kernel's own `local` flag (absent on `smbfs`,
  `nfs`, `afpfs`, `webdav`, and FUSE); on Linux it parses
  `/proc/self/mountinfo` with GNU `df -l`'s remote rule (`host:path`,
  `//host/share`, a short list of cluster filesystems by type). Both are
  pure parsers, unit-tested against canned mount tables, and answer
  "unknown" rather than guessing.
- **The warning follows the cost, not the flag.** It now sits under the
  "Hashing N files already at the destination" line and fires whenever that
  pass would read a dest on a network mount. That covers the case `--rehash`
  never saw: rsync `model/` alone (no `transfer.json`), then `assemble` over
  the mount, and every file is hashed as adoption — the same full read back
  over the wire, previously silent. `--rehash` only adds the "or omit
  `--rehash`" advice.
- **`assemble` no longer over-reports what the destination holds.** The
  "Destination already held N verified files" count subtracted only
  *present-but-unverified* files from the expected set, so files missing
  from dest counted as trusted — a half-fetched partial claimed every
  expected file. That line is gone.

### Changed

- One trust note — `trusted dest ledger + size` or `re-hashed dest against
  the pin` — is now shared by the registered and unregistered `assemble`
  paths, replacing the duplicated and inconsistent "Destination already
  held" lines. The registered-destination path states plainly that it
  copies and downloads nothing, and a test asserts it never reads dest.

## [0.14.0] - 2026-08-30


### Added

- **`darsay doctor` — offline vault diagnostics with reversible repair.** The
  default pass validates configuration, manifests, payload paths/sizes/hashes,
  generated bundle READMEs, transfer locks, and disposable hydration records.
  It never follows payload symlinks, uses the network, changes payload bytes,
  overwrites `curation.md`, or fabricates archival facts. `--quick`, `--since`,
  `--budget`, `--only`, and `--skip` bound the scan; `health`, `explain`,
  `capabilities --json`, and `robot-docs` give operators and agents stable
  discovery surfaces.
- **Locked, journaled `doctor --fix` and byte-exact `doctor undo`.** The only
  automatic repairs regenerate derived `README.md` files or quarantine proven
  stale/disposable `transfer.lock` and `hydration.json` state. Each mutation is
  contained inside the vault, backed up verbatim, recorded before its atomic
  replace/rename, and protected by a non-blocking global lock. Runs, JSON and
  Markdown reports, actions, private backups, quarantine, history, diffs, and
  undo scripts live under `<vault>/.doctor/`; exit codes distinguish findings,
  partial repair, unsafe refusal, contention, usage, and I/O failure.
- **Fail-closed interrupted-run recovery.** Prepared journal actions block new
  work until an explicit strict undo validates their before/after hashes. Undo
  accepts only the three canonical mutable bundle filenames, records a recovered
  action set atomically, compensates if that marker cannot be persisted, and may
  reclaim only a proven dead same-host lock written by the interrupted doctor.
- Doctor JSON reports now state `artifacts_created`, `network_attempts`, and
  `target_actions` directly; shallow `health --json` reports no artifacts, no
  network attempts, and no target actions.
- **Repository-owned doctor safety conformance.** Hermetic pytest coverage now
  delivers real SIGKILL immediately after durable intent and after target
  commit, forces two-process contention, proves repeat-fix idempotence and
  byte-exact undo, checks detector repeatability, and freezes the scrubbed
  `capabilities --json` contract as a reviewed golden. CI runs the complete
  layer without relying on an external audit workspace or skill installation.

### Changed

- **rsync is a first-class copy.** An out-of-band `rsync` / `cp -a` of a
  bundle (or its payload) into the usual `<vault>/<slug>/<rev>/` layout is
  the same as a darsay copy. The next `archive` / `assemble` /
  `assemble --move` trusts dest files already `verified` in dest's ledger
  (size match), fetches only what is still missing, and rewrites transfer
  metadata. It does not re-download a file whose digest matches, and it
  does not re-read dest to re-hash those files (hashing an SMB dest from
  the laptop would pull the payload back over the wire).
- **`assemble` hashes dest only under `--rehash`** (same flag as `archive`)
  or when dest has bytes with no verified ledger record (adoption). The
  live panel still covers that hashing pass. `--rehash` on a network
  filesystem (`smbfs`, `nfs`, …) warns: run it on the dest host. Default
  `--move` after rsync is metadata + source delete, not a dest-wide read.
- **`assemble --move` into a registered destination** trusts dest ledger +
  size (payload stays frozen) and skeletonizes the source. `--move` of a
  registered source is refused: rsync the finished bundle, then
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

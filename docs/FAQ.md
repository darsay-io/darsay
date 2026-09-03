<p align="center">
  <a href="GETTING-STARTED.md">Start here</a> ·
  <a href="CONCEPTS.md">Concepts</a> ·
  <a href="../examples/README.md">Examples</a> ·
  <a href="README.md">All docs</a> ·
  <a href="../README.md">README</a>
</p>

# FAQ

Short answers to the questions that come up once a vault is more than one
disk. Each answer names the document that owns the detail.

## Moving bundles

### How do I move a bundle to another disk?

```bash
darsay mv qwen--qwen3-0.6b /Volumes/big
```

`mv` takes a registered bundle and a destination **vault root** — a
folder that already exists. On the same filesystem it is a rename.
Across filesystems it copies into a staging directory beside the
destination, re-hashes every payload file *there* against the manifest,
stamps the manifest's new `archive.location`, renames the copy into
`<vault>/<name>/<rev>/`, and only then removes the source. A failed
verification removes the staging copy and leaves the source exactly as
it was. `-n` shows which of the two it will do and where the bundle
lands.

Or rsync it — next question. Both are correct. `mv` is the one with the
refusals built in, and it is happy to finish what an rsync started: a
copy already at the destination is hashed in place, not copied again
(two questions down).

### Is rsync still a valid way to move or copy a bundle? Will it stay valid?

Yes, and yes. **rsync is a first-class copy** is an invariant of the
project — listed in [CLAUDE.md](../CLAUDE.md) and on the
[docs index](README.md#invariants) — not a compatibility mode. A vault is
a folder tree with no database; `darsay list` is a walk of it. Anything
that puts the right bytes at `<vault>/<name>/<rev>/` has put a bundle in
the vault: rsync, `cp -a`, a USB stick, a restic restore.

```bash
rsync -a ~/darsay/qwen--qwen3-0.6b/<rev>/ /Volumes/big/qwen--qwen3-0.6b/<rev>/
darsay --vault /Volumes/big verify qwen--qwen3-0.6b     # on the host that owns the disk
darsay rm qwen--qwen3-0.6b --yes
```

Or two steps: rsync, then `darsay mv` — which finds the copy already
there, hashes it in place, copies nothing that matched, and does the
bookkeeping and the removal for you (next question).

`darsay mv` is those three steps as one verb with the refusals attached.
It is sugar over the contract rsync already satisfies, and it will never
become a requirement: nothing `mv` records is needed to open, verify, run,
or export the bundle. What `mv` does that rsync does not is bookkeeping —
`archive.location`, the `archive.moves` record, a regenerated `README.md`
whose path is true — never a change to the payload. The design and the
reasons are in [Incremental transfer §1](INCREMENTAL.md#1-why-this-can-beat-rsync-at-fetching--and-why-rsync-still-copies-disks);
the tests that hold it are `tests/integration/test_transfer.py`
(rsync then darsay) and `tests/integration/test_relocate.py`.

### I rsync'd the bundle to the other disk earlier. What does `mv` do with the copy that is already there?

Lands on it. `mv` and `cp` treat a destination that already holds
`<name>/<rev>/` as a landing site, not a conflict. Each payload file
already there at its recorded size is hashed *where it is* and kept when
it matches; a file that is missing, at another size, or hashes wrong is
copied from the bundle; then the bundle's own record, views, notes, and
ledger replace the copy's, the verification is recorded, and the source
is removed. Nothing that already matched crosses the cable again. This
is adoption — the first step `archive` and `assemble` take over bytes
they find on disk — and it is what makes "rsync, then `darsay mv`" cost
one read of the destination and nothing else.

```
$ darsay mv zai-org--glm-4.5v /Volumes/PIXEL/darsay/vault
Moving zai-org--glm-4.5v@ed47433b3711
  from:     ~/darsay/zai-org--glm-4.5v/ed47433b3711
  to:       /Volumes/PIXEL/darsay/vault/zai-org--glm-4.5v/ed47433b3711  (exists)
  how:      all 56 payload files are already there at the recorded size — hash them in place, copy nothing, then remove the source
  disk:     needs 61.4 KiB, free 1.8 TiB (2.0 GiB floor) at /Volumes/PIXEL/darsay/vault/zai-org--glm-4.5v/ed47433b3711 — OK
Landing on /Volumes/PIXEL/darsay/vault/zai-org--glm-4.5v/ed47433b3711: hashing 56 payload files in place, copying 0 (0 B) ...
Verification: PASS (56 files; 0 modified, 0 missing, 0 extra)
Moved zai-org--glm-4.5v@ed47433b3711 → /Volumes/PIXEL/darsay/vault/zai-org--glm-4.5v/ed47433b3711  (56 payload files verified at the destination — 56 already there, 0 copied — before the source was removed)
```

The copy's record may be older than the bundle's — an rsync made before
a `darsay migrate`, as here — and that is fine: `mv` never reads it. The
record that lands is the one you are moving. `archive.moves` says
`method: adopt`, how many files were already there, how many were
copied, and which, if any, were there but hashed wrong (`replaced`).

Two things a landing will not do silently. If the destination holds
payload files this bundle's record does not list — another pin of the
same revision, or a stray file — `mv` refuses and names them; `darsay rm`
whichever side you do not want, then rerun. And if the destination's
`curation.md` differs from the bundle's, the plan says so before anything
moves: the bundle's is the one that lands. A copy you have been curating
*at* the destination is the live one — `darsay rm` the source instead of
moving onto it.

### What is the difference between `darsay mv` and `assemble --handoff`?

They act on different objects and are not interchangeable. Each refuses
the other's object and names the right verb.

| | `darsay mv BUNDLE VAULT` | `darsay --vault VAULT assemble PARTIAL --handoff` |
|---|---|---|
| Acts on | a **registered** bundle (`manifest.json` present) | a **partial** (`transfer.json`, no manifest) |
| Unit | the whole bundle | one payload file at a time |
| What crosses | payload, manifest, `curation.md`, generated views, ledger, export log | verified payload files the destination can vouch for |
| Verification | re-hashes the copy at the destination before the source is removed | trusts dest's own `verified` record (ledger + size after an rsync, or `--rehash`) |
| Left at the source | nothing; the empty `<name>/` folder is swept | a **skeleton** — pin, expected inventory, every hash, `handed_off` records, no payload bytes — until the last file is handed over, then nothing |
| Refuses | partials; a destination vault that does not exist; a destination holding payload files this record does not list (a copy of *this* bundle already there is landed on, not refused) | registered sources — a registered payload is frozen |
| For | a finished bundle changing disks | one pin crossing two disks that never mount together |

`darsay cp` is `mv` without the removal — same copy, same verification at
the destination, source kept, and both manifests record the replica.

Full mechanics of the hand-off: [Incremental transfer §5](INCREMENTAL.md#across-disks-assemble---handoff-and-skeletons).

### Why did `assemble`'s move flag become `--handoff`?

Until 0.14.8 the flag was spelled with the word *move*. Then `darsay mv` arrived. Two things called *move* that do different
things to different objects — one deletes a source, the other leaves a
skeleton — is a trap for the reader and for the shell history. Now *move*
means one thing (a registered bundle changes vault) and *hand-off* means
the other (a partial's verified files cross vaults one by one).

The per-file ledger state followed the verb: `moved` → `handed_off`,
`moved_at` → `handed_off_at`; `darsay list` says `handed off`,
`list --json` reports `handed_off_bytes`, `archive` pauses with
`end_reason: handed_off`, and the ledger events are `handoff_out` /
`handoff_in`. Nothing about the mechanism changed. A skeleton ledger
written before the rename still carries `moved` records; the new tool
treats them as missing and re-fetches those files — slower, never lossy.

### I rsync'd a bundle. What do I have to tell darsay?

Nothing. Put it at `<vault>/<name>/<rev>/` — not in the vault root — and
`darsay list` sees it. Run `darsay verify` **on the host that owns the
disk** if you want the copy attested — rsync's default check is size and
mtime, not content, unless you run rsync with `--checksum`. Machine-local files
travel harmlessly: `hydration.json` points at the old vault's `.runtime`,
so `darsay hydrate` again there (or `darsay dehydrate` it); `darsay doctor`
flags a record whose interpreter is gone.

### `mv` warned that my destination is a network mount. Why, and what should I do?

Verifying the copy means reading it back. Over SMB or NFS that is the
whole payload crossing the wire a second time — the thing darsay promises
never to do *silently* after an rsync. For a NAS, prefer the three-step
form: rsync (one trip), `darsay verify` on the NAS itself (a local read),
`darsay rm` on the laptop. `mv` still works and still verifies; it just
says up front what that costs.

### `mv` on the same disk was instant. Was anything verified?

No, and nothing needed to be: a rename does not rewrite a byte. The
manifest's `archive.moves` entry records `method: rename`; across disks
it records `method: copy` and the verification happened before the source
was removed. `darsay verify` re-hashes at any time.

### Do I need `export` / `import` to move a bundle?

No. `export` makes one deterministic `.mvb.tar` for a shelf, a USB stick,
or a stranger; `import` unpacks it and re-hashes before registering. That
is two hash passes and, briefly, twice the space. Use it when the
destination is a *file*, not a vault. [MVB format](MVB-FORMAT.md).

### Why does `mv` refuse to create the destination vault?

Because `/Volumes/big` with the disk unmounted is a folder on the boot
disk. Copying 60 GB into it would succeed and be wrong. Mount the disk,
or make the folder on purpose, and rerun.

### What travels with `mv` / `cp`, and what stays behind?

The same set for both. Travels: the payload (`model/` or `data/`) untouched; `manifest.json` with
`archive.location`, `archive.host`, and `archive.moves` updated;
`curation.md` verbatim; `README.md` and `VERIFICATION.md` regenerated so
the path they print is true; `verification.json`; `exports.json`;
`transfer.json` (disposable, relative paths only). Stays behind:
`hydration.json` (vault-local) and `transfer.lock` (process-local).
`.DS_Store` files are not copied. Symlinks are refused; a bundle never
contains one, and if you put one there, rsync it deliberately. Onto a
copy already at the destination, the same set lands and replaces the
copy's — except a `hydration.json` there, which is that vault's own.

### Does moving change the bundle hash?

No. The bundle hash covers the payload only, and the payload is never
touched. The manifest does change (`location`, `host`, `moves`), so an
export taken after the move is not byte-identical to one taken before —
the same is true after a `verify`. Export determinism is per bundle
*state*: [MVB format](MVB-FORMAT.md).

### How do I keep a second copy on another disk?

```bash
darsay cp qwen--qwen3-0.6b /Volumes/backup
```

`cp` is `mv` without the removal: copy into a staging directory
(copy-on-write clones where the filesystem offers them), re-hash the
copy at the destination, rename it into place, keep the source. Then
*both* manifests record the other as a replica — `archive.replicas`
gains `{at, location, host}` and `backup_status` becomes `replicated` —
so either side can answer "where else does this live?". Run the same
`cp` again to refresh the backup: the copy is hashed in place, only what
is missing or differs is copied, and the replica entry — keyed by
location — updates its timestamp rather than listing the disk twice. Or
rsync into the same `<name>/<rev>/` layout and `darsay verify` there —
the bytes are equally good; only the bookkeeping differs.

## Older records

### A bundle someone sent me says "manifest schema 1.8.0 predates this darsay". What now?

```bash
darsay migrate ./zai-org--glm-4.5v/ed47433b3711 -n    # read what will change, and where it comes from
darsay migrate ./zai-org--glm-4.5v/ed47433b3711       # write the record in this darsay's schema
darsay verify  ./zai-org--glm-4.5v/ed47433b3711       # then hash the payload where it landed
```

The bundle is fine. Its record — `manifest.json` — was written by an
older darsay in the shape the model of the models had then, and this
darsay reads one schema major. `migrate` reads the recorded facts back
under the current model and writes them in the current shape: family,
generation, and member re-read from the name; precision and bytes per
parameter re-derived from `config.json` and the weight headers;
`relationships` translated into `lineage` with each parent's relation and
provenance; the subset policy's words renamed; everything else carried
exactly as recorded. It is offline, it reads a few small files under the
payload and hashes none of them, and it says what it did in
`archive.migrations`. The refusal you saw ends with the exact command;
`-n` shows the plan first. The whole table is in
[MANIFEST.md → Migration](MANIFEST.md#migration).

### Does migrating touch the payload or the bundle hash?

No. Nothing under `model/` (or `data/`) is written, and the inventory —
the hash record — is carried as recorded. So is the record's own last
verification: a bundle archived at this path on this host and never
moved still stands, and `migrate` ends with `done:` and the date it
passed. A record whose last pass was somewhere else — a bundle that
arrived by rsync — ends with `next: darsay verify …` instead: the record
is now readable, and hashing the payload where it landed is the same
step it always was after an rsync.

### I run a vault that takes submissions. What is the routine?

```bash
darsay list                 # rows marked `migrate` are records this darsay cannot read yet
darsay migrate --all -n     # every such record: what it will say, and where that comes from
darsay migrate --all        # write them; the last lines are the verify commands for the ones that arrived from elsewhere
darsay verify <bundle>      # each of those, on the host that owns the disk
```

`darsay doctor` reports them too (`bundle.manifest`, a warning with the
command). An older `.mvb.tar` needs nothing extra: `darsay import`
verifies its payload against the embedded record and migrates the record
as it lands, so a submission by file is one command either way.

### The sender's darsay is newer than mine. Can I migrate down?

No — a record moves forward, never back. Upgrade darsay; the refusal says
which major it needs.

## Partials

### Can I move a half-finished archive?

Copy its whole directory — payload `.cache` and `transfer.json` included —
to the other vault's `<name>/<rev>/` and rerun the same `archive` command
there; the pin, completed files, and the longest Range partial all
continue. `mv` refuses partials on purpose and points here. To free the
first disk *and* keep fetching on it, that is a hand-off:
[Archive in halves across two disks](../examples/README.md#archive-in-halves-across-two-disks).

## Vocabulary

| Word | Means | Verb |
|---|---|---|
| **move** | a registered bundle changes vault; the source is gone | `darsay mv` — or rsync, `verify`, `rm` |
| **copy** | a verified second copy of a registered bundle in another vault; both manifests list each other under `archive.replicas` | `darsay cp` — or rsync, then `verify` |
| **hand-off** | a partial's verified files cross vaults one by one; the source keeps a skeleton | `darsay assemble … --handoff` |
| **skeleton** | a partial whose verified bytes were handed off: pin + hashes, no payload | made by a hand-off, dissolved by the last one |
| **relocate a partial** | copy a partial's directory to another vault and continue there | `cp -a` / rsync, then the same `archive` |
| **adoption** | bytes already on disk that hash to the record are kept, wherever they came from | what `archive` / `assemble` do first; what `mv` / `cp` do onto a copy already at the destination |
| **export / import** | pack a bundle into one verified file / unpack it with verification | `darsay export`, `darsay import` |
| **migrate** | a record written under an earlier schema major is re-read under the current model and rewritten in the current shape; the payload is untouched | `darsay migrate` — `import` does it for an older export |

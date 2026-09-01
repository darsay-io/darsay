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
refusals built in.

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

`darsay mv` is those three steps as one verb with the refusals attached.
It is sugar over the contract rsync already satisfies, and it will never
become a requirement: nothing `mv` records is needed to open, verify, run,
or export the bundle. What `mv` does that rsync does not is bookkeeping —
`archive.location`, the `archive.moves` record, a regenerated `README.md`
whose path is true — never a change to the payload. The design and the
reasons are in [Incremental transfer §1](INCREMENTAL.md#1-why-this-can-beat-rsync-at-fetching--and-why-rsync-still-copies-disks);
the tests that hold it are `tests/integration/test_transfer.py`
(rsync then darsay) and `tests/integration/test_relocate.py`.

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
| Refuses | partials; a destination vault that does not exist; a destination already holding the bundle (unless `--force`) | registered sources — a registered payload is frozen |
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
contains one, and if you put one there, rsync it deliberately.

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
so either side can answer "where else does this live?". Refresh a
backup with `--force`; the replica entry is keyed by location, so the
timestamp updates rather than the disk being listed twice. Or rsync
into the same `<name>/<rev>/` layout and `darsay verify` there — the
bytes are equally good; only the bookkeeping differs.

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
| **adoption** | bytes already on disk that hash to the pin are kept, wherever they came from | what `archive` / `assemble` do first |
| **export / import** | pack a bundle into one verified file / unpack it with verification | `darsay export`, `darsay import` |

<p align="center">
  <a href="GETTING-STARTED.md">Start here</a> ·
  <a href="CONCEPTS.md">Concepts</a> ·
  <a href="../examples/README.md">Examples</a> ·
  <a href="README.md">All docs</a> ·
  <a href="../README.md">README</a>
</p>

# Incremental archiving — idempotent, resumable transfer

> **In one sentence.** `archive` is interruptible by default. Rerun the
> same command. `--max-gb` stops cleanly. `--shard N/T` shares the work
> across people; `assemble --move` shares it across disks that never meet.
> Copy-paste: [pause and resume](../examples/README.md#pause-and-resume-a-large-archive).

Companion to [QUANTIZATION.md](QUANTIZATION.md) (what to archive) — this
document is about *how the bytes get here* when "what" is tens of
gigabytes to terabytes.

A catalog overlay treats an in-progress pin as status `partial` — unfinished
work, same as a want. `darsay archive --next CATALOG` prefers a `partial`
over a `want` (desire is the tiebreak among partials, then among wants) so
budgeted sessions finish bytes already on disk. See [Catalogs](CATALOGS.md).

`darsay archive` is an operation you can interrupt at any
moment, re-run any number of times, and spread across days of budgeted
sessions — and every run does the minimum network work required to converge
on the same verified bundle:

```bash
darsay archive Qwen/Qwen3.8-27B --max-gb 10     # tonight: first 10 GB
darsay archive Qwen/Qwen3.8-27B --max-gb 10     # tomorrow: next 10 GB
darsay archive Qwen/Qwen3.8-27B --dry-run       # what's left? (rsync -n)
darsay archive Qwen/Qwen3.8-27B                 # finish, verify, register
```

No new resume subcommand or mode: **idempotent convergence is the default
behavior of `archive`**. An interrupted run continues and a damaged partial
heals. A registered bundle remains protected by the existing already-exists
guard; `--force` performs a fresh pin and adopts matching payload bytes
instead of downloading them again.

## 1. Why this can beat rsync at fetching — and why rsync still copies disks

rsync is the disk-to-disk copy. darsay does not replace it. An `rsync` (or
`cp -a`, a USB, a restic restore) of payload bytes into a vault is a
**first-class copy**: the next `archive` / `assemble` / `assemble --move`
trusts dest files the ledger already marks `verified` (size match), fetches
only what is still missing, and rewrites transfer metadata. It must not
re-download a file whose digest matches, and it must not pull dest back
over the wire to re-hash a copy. Users who rsync then run darsay should
never be astonished by a second trip of the majority — not as a Hub
download, and not as an SMB dest-hash. Hash dest where it is a local disk
(`assemble --rehash` / `darsay verify`).

Where darsay beats rsync is the *upstream* transfer. rsync's source is
*mutable*, so it must interrogate both sides (size/mtime quick-check, then
rolling checksums) to discover what changed. Our source is a **pinned,
content-addressed git revision**: once `archive` resolves ref → commit on
the first run, the transfer set is frozen and fully enumerable from one
metadata call — every file's path, exact size, and upstream digest (LFS
SHA-256 for large files, git blob SHA-1 for small ones) are known *before a
single payload byte moves*.

That turns the whole problem into set arithmetic:

```
remaining = expected(pinned revision) − verified(local bytes)
```

Both sides of that subtraction are recomputable at any time from durable
facts — upstream metadata and local file hashes. Everything else in this
design (the state file, budgets, ordering) is acceleration and bookkeeping
around that one line.

## 2. Design rules

1. **The pinned revision is the transfer set.** The first run resolves the
   requested ref to a commit and records it; every later run uses the pin
   and never re-resolves. Upstream moving `main` cannot change, corrupt, or
   restart an archive in progress (resume deliberately does not query the
   moving ref). One revision, one bundle dir, one convergence target — this is
   what makes re-running safe.
2. **Bytes are the authority; transfer state is a disposable cache.** The
   per-file ledger in `transfer.json` (§4) exists to avoid re-hashing
   terabytes on every run — but deleting it loses nothing: reconciliation
   (§3) rebuilds it by hashing what's on disk against upstream expectations.
   The one fact the bytes cannot tell you is that a *deliberately* absent
   file is verified in another vault (a `moved` file — a skeleton, §5); the
   ledger records that, and losing it costs network, never truth. Same
   doctrine as hydration: derived state must never hold archival truth.
3. **Verify each file once, immediately, at its freshest.** A file is hashed
   the moment its last byte lands and checked against the upstream digest
   right there — not in a giant pass at the end (hashing 500 GB is itself
   hours, and end-of-archive verification checks week-old bytes). The
   recorded hash is reused for manifest assembly; post-registration rot is
   `darsay verify`'s job, exactly as today.
4. **Never fetch a byte you can prove you have.** Verified files are never
   re-downloaded. Present-but-unrecorded files are hashed and *adopted* if
   they match. Partial files resume with Range requests from the byte where
   they stopped. Identical blobs already verified in sibling bundles are
   copied locally instead of fetched (§5).
5. **rsync is a first-class copy.** Out-of-band copies (`rsync`, `cp -a`,
   USB, restic restore) of a bundle — or just its payload — into the usual
   `<vault>/<slug>/<rev>/` layout are the same as a darsay copy. The next
   command trusts dest files already `verified` in dest's ledger whose size
   still matches (the same gate `archive` uses), downloads only what is
   still missing, and adjusts metadata (`transfer.json`, `moved`). It does
   not re-read dest to re-hash those files: hashing dest on an SMB mount
   from the laptop pulls the whole payload back over the wire. Hash dest
   where the bytes live (`assemble --rehash` / `darsay verify` on the dest
   host). Present dest files with no verified record are still hashed
   (adoption). A registered payload stays frozen: rsync a finished bundle,
   `verify` dest on the dest host, `darsay rm` the source. `assemble --move`
   is the verb for partials.
6. **Every stop is clean, every run converges.** Budgets, Ctrl-C, a
   filling disk, network loss, power loss — all leave a state a later run
   continues from. The
   state file is written atomically (temp + rename) after every file
   completion, so the worst possible loss is one in-flight file's progress
   record, which reconciliation recovers anyway.
7. **Record, don't fabricate — for the transfer itself.** Sessions, bytes
   moved, adoptions, local-copy sources, retries, and digest mismatches are
   all logged. A bundle assembled over fourteen sessions says so in its
   manifest; a file that came from a sibling bundle instead of the network
   is attributed. The registration bar does not move: no bundle gets a
   manifest until every expected file is verified.

## 3. The loop: pin → reconcile → plan → transfer → register

Every `archive` run executes the same five phases; completed phases fall
through instantly.

**Pin** — if no `transfer.json` exists: resolve ref → commit with
`files_metadata=True`, record the expected file list (path, size,
`lfs_sha256` / `git_sha1`) and the gate/card snapshot needed later, create
the bundle dir. If one exists: load it, use the pinned commit, make **zero
Hub API calls** — resume sessions talk only to the CDN.

**Reconcile** — walk the expected list against the payload dir and the
ledger; classify each file:

| Local evidence | State | Action |
|---|---|---|
| Ledger says verified, size matches on stat | `verified` | trust (or re-hash under `--rehash`) |
| Present, size matches, no ledger entry | `unverified` | hash now; matching digest → **adopt** (zero network); mismatch → demote |
| Ledger says moved, absent | `moved` | trust — the bytes are verified in another vault (§5); never fetch, never demote |
| Ledger says moved, present | `unverified` | the bytes came back: hash and adopt as `verified` (matching bytes win over the hint); on mismatch, remove the bytes and keep `moved` |
| `.incomplete` bytes in the payload's `.cache/huggingface/` | `partial` | resume via Range from current length |
| Absent | `missing` | fetch |
| Present but wrong size, or hash mismatch | `mismatch` | delete, log the event, treat as missing |

Reconciliation is what makes the system self-healing: a lost ledger, a
crash between file-write and state-write, or bytes copied in by hand all
converge to the correct classification from evidence.

**Plan** — report the arithmetic before moving anything: files and bytes
verified / partial / missing, bytes already banked inside partials,
estimated remaining network transfer, and a disk preflight of remaining
bytes against free space above the free-space floor (§6; the same
headroom check `estimate` uses). `--dry-run`
stops here — this is the rsync `-n` of the system, and the answer to "what
have we got, what remains" at any moment. `darsay estimate` performs the
same read-only classification (without hashing, so adoptable bytes report
as `unverified`) and renders it as a static download bar in the live
panel's style: banked bytes over the pinned total, the
verified/unverified/partial breakdown, and a disk verdict that counts
only the remaining network bytes.

**Transfer** — fetch `remaining` in ascending size order (stable tie-break
by path): configs, tokenizer, card, and license complete in the first
minutes — making the partial payload inspectable early — and each budgeted
session completes whole files rather than leaving many large stubs.
While bytes move, a TTY shows one panel for the *whole* payload — percent
complete, bytes in / total, smoothed rate, a slow sparkline of recent rate
(one cell per ~5 s), time remaining, files done, the file now in flight —
because a per-file bar resetting at 0% on every shard is the wrong unit for
a 50 GB archive. Numeric fields render at fixed widths so digit rollovers
never shift columns, frames repaint in place without flicker, and while the
panel is live any other output aimed at the terminal (Hub client warnings,
log lines, library loggers that bound their handler to the terminal before
the panel started) is captured and printed *above* it instead of tearing
through it. The time-remaining slot is also where the transfer's state
lives: `stalled` when bytes stop, `offline` / `reconnecting` when the
network is gone (§6), `stopping` after Ctrl-C — the waiting states in
amber, the one accent besides brand cyan. A configured rate cap shows as
`· cap 5.0 MiB/s` in the tail. Piped or logged runs emit the same facts as
a status line every 10 seconds. `DARSAY_PROGRESS=0` disables it;
`DARSAY_PROGRESS=line` forces the log form even on a TTY.
Per file: check local sources (§5) → else `hf_hub_download` at the pinned
commit (the library's `.incomplete` + Range machinery provides byte-level
resume) → hash → compare to upstream digest → append to ledger →
atomically rewrite `transfer.json`. A digest mismatch on a freshly
completed file is retried once (transit corruption), then logged as an
upstream mismatch and set aside — it never silently blocks the rest of the
transfer.

**Concurrency.** Small files (< 8 MiB) fetch through a bounded worker
pool (`--jobs`, default 4 — dataset bundles with thousands of parquet
shards need this). Large files fetch through `--jobs` concurrent streams
as well, each file one HTTP stream (a single CDN connection rarely fills a
fast link), and a finished file's digest runs on one separate hash thread
while the streams keep downloading, so the network never idles for
verification (`hashlib` releases the GIL; a 5 GiB shard hashes in seconds
on a local SSD — see [DESIGN.md](DESIGN.md#transfer-concurrency) for the
measurements). If the disk cannot keep up with the network, fetching
pauses at `--jobs` + 1 files awaiting digest rather than piling up
unverified files. Workers never write the ledger: results are committed on
the main thread as digests finish, so completion order can differ from
start order. The floor guard counts every in-flight stream at the bytes it
has yet to land before another file begins. Streams share one rate cap and
one byte budget; a stop leaves up to `--jobs` resumable partials.

**Stops inside the pipeline.** Only Ctrl-C abandons a running digest — the
file stays on disk and the next run's reconcile adopts it after hashing.
A byte or time budget lets queued digests finish, since they spend no
network and no disk. An error on one file halts the other streams at their
next chunk, so a 5 GiB fetch does not run to the end for a session that
is already lost.

darsay deliberately selects `hf_hub_download`'s HTTP path even
when `hf_xet` is installed: current Xet aborts discard in-flight
reconstruction state, while HTTP Range leaves a durable `.incomplete` file
that survives budgets, SIGINT, process restarts, and filesystem copies. The
Hub client still owns authentication, redirects, Range requests, and retries.

**Register** — runs only when every expected file is `verified` (persistent
upstream mismatches are carried into `checksum_verification` as `fail`,
exactly like today's one-shot flow — recorded, never hidden). Only now do
the completion-time steps run: metadata extraction, licensing record,
ecosystem queries, manifest assembly from the ledger's recorded hashes,
README/VERIFICATION/curation template — and only now is the payload's
`.cache/huggingface/` bookkeeping removed (it must survive between sessions:
it holds the partials; `iter_payload_files` and hashing skip it until then).
Manifest written ⇒ bundle registered ⇒ payload immutable. The
existing rule that a manifest's presence blocks re-archiving (absent
`--force`) is unchanged; with this design `--force` becomes cheap — a fresh
pin whose reconciliation adopts every verified byte, i.e. a re-verification
plus manifest rebuild, not a re-download.

Session accounting: every run appends a session record (started, ended, end
reason — `complete` / `budget` / `disk` / `offline` / `moved` / `interrupt` /
`assemble` / `error` — the darsay build that ran it, bytes from network,
bytes adopted, files completed, reconnects, host).
Ctrl-C escalates: the
first press requests a clean stop (the panel shows "stopping", the current
chunk is banked, the state write finishes, and the CLI exits 10 with the
resume hint), a second press aborts immediately — even a stalled connection
or an in-flight hash — and still pauses cleanly because the ledger and
payload bytes are always durable, and a third press hard-kills the process. Cooperative
archive sessions also record their advisory `shard: "N/T"`; assembly records
the number of merged inputs.

**Exit codes:** `0` — bundle completed and registered by this invocation;
`10` — clean partial stop, more remains (budgets, the free-space floor or
a full disk, a declined preflight, a network gone longer than
`max_offline`, a skeleton whose remainder lives in another vault
(`end_reason: moved`, §5), and interrupts), so wrappers can loop; `1` —
error.

```bash
# unattended completion over nightly cron: rerun until exit 0
darsay archive Qwen/Qwen3.8-27B --max-minutes 240 || [ $? -eq 10 ]
```

A loop like that is safe on a filling disk: a floor stop also exits 10,
each rerun re-checks the floor before moving bytes, and the reason is in
the session record and on the terminal — so the loop idles at the floor
until an operator clears space, rather than driving the partition to zero.

## 4. `transfer.json` — the transfer ledger

Bundle-root, machine-local, excluded from `.mvb.tar` exports exactly like
`exports.json` / `hydration.json`; deletable at any time per rule 2. Shape:

```jsonc
{
  "transfer_version": 1,
  "repo_id": "Qwen/Qwen3.8-27B", "repo_type": "model",
  "revision": "<full commit>", "revision_ref": "main",
  "pinned_at": "2026-08-24T21:04:11+00:00",
  "expected": [
    {"path": "model-00001-of-00012.safetensors", "size": 4966786096,
     "lfs_sha256": "…", "git_sha1": null}
  ],
  "files": {
    "config.json": {"status": "verified", "sha256": "…", "blake3": "…",
                    "git_sha1": "…", "verified_at": "…",
                    "source": "network", "attempts": 1},
    // status "moved": a verified record whose bytes were handed to another
    // vault (skeleton, §5). Every hash is kept; "moved_at" is added.
    "model-00003-of-00012.safetensors":
                   {"status": "moved", "size": 4966786096, "sha256": "…",
                    "blake3": "…", "git_sha1": null, "verified_at": "…",
                    "verified_against_upstream": true, "source": "network",
                    "attempts": 1, "moved_at": "2026-08-25T09:12:40+00:00"}
    // source: "network" | "adopted" | "local:<bundle_id>"
  },
  "sessions": [
    {"started": "…", "ended": "…", "end_reason": "budget",
     "tool": "darsay …",
     "bytes_network": 10737418240, "bytes_adopted": 0,
     "bytes_local_sources": 4966786096, "retries": 0, "reconnects": 1,
     "files_completed": 3, "host": "…", "shard": "1/3"}
    // end_reason also includes "moved": nothing left to fetch here, but the
    // pin is not complete here — assemble the halves to register (§5).
  ],
  "events": [
    {"at": "…", "path": "…", "event": "digest_mismatch", "detail": "…"},
    {"at": "…", "path": null, "event": "moved_out",
     "detail": "handed 7 files (27.8 GB) to vault 'big' on jn-mbp"},
    {"at": "…", "path": null, "event": "moved_in",
     "detail": "received 7 files (27.8 GB) from a skeleton on jn-mbp"},
    {"at": "…", "path": "…", "event": "network_lost",
     "detail": "DNS lookup failed; waiting to reconnect"},
    {"at": "…", "path": "…", "event": "network_restored",
     "detail": "resumed after 3 reconnect attempts"}
  ]
}
```

A `transfer.lock` (pid, host, started, physical bundle identity) guards
against two concurrent runs on one bundle; a lock whose pid is dead on the
same host is stale and reclaimed with a notice. A lock copied with a partial
bundle is also reclaimed when its recorded device/inode differs, while two
paths to the same physical directory still exclude one another. The
half-state signal is directional and unambiguous:
`transfer.json` without `manifest.json` = archive in progress;
`manifest.json` present = registered, ledger is history.

### Relocating a partial transfer

The durable `transfer.json` and resume cache contain repository-relative paths
and pinned upstream facts, never the source machine's vault path. HTTP partials
and Hub metadata live under the payload's `.cache/huggingface/`; the Xet cache
is pointed there too
as a defensive fallback. Copy the partial bundle directory—including its
payload `.cache` and `transfer.json`—under another vault's same two-level
`<repo-slug>/<revision12>/` layout, then re-run the same archive command:

```bash
cp -a /Volumes/USB/qwen--qwen3-0.6b/c1899de289a0 /srv/vault/qwen--qwen3-0.6b/
darsay --vault /srv/vault archive Qwen/Qwen3-0.6B
```

The copied lock, if any, is recognized as belonging to the original physical
directory and reclaimed. Reconciliation adopts completed files, Range resumes
the copied partial, registered siblings in the destination vault remain
eligible local sources, and no pin/metadata refresh changes the convergence
target.

## 5. Minimizing network traffic

In descending order of bytes saved:

1. **Adoption** (rule 4): any local bytes whose digest matches upstream are
   kept, whatever put them there — a prior run, a crashed run that outpaced
   its ledger, a manual copy from another machine. Cost: one local hash.
2. **Local sources — rsync's `--link-dest`, vault-wide.** Before fetching a
   file, look up its `lfs_sha256` in an index built from the *registered*
   sibling manifests of the same repo (other revisions of a model share
   most weight shards unless retrained). On a hit, copy from the verified
   sibling bundle instead of the network — on APFS via clonefile
   (copy-on-write: instant, no extra space until divergence; plain copy as
   fallback and on other filesystems). The copy is hashed like any download
   (the sibling could have rotted since its last `verify`). Attribution:
   ledger `source: "local:<bundle_id>"`, and the manifest's existing
   `mirrors_used` gains the sibling reference. Hard links are rejected —
   they couple bundles' mutability; reflinks keep each bundle an
   independent file tree.
3. **Byte-level resume** of partials via Range requests — a 9.8 GB stub of
   a 10 GB shard costs 200 MB to finish, not 10 GB.
4. **Metadata thrift**: the plan is fetched once at pin time; resume
   sessions make no API calls, and registration makes only the
   completion-time ecosystem queries. Card facts come from the pin ledger.

### Coordinating a download: shards, assembly, and skeletons

One pin can be too big for one session, one person, or one disk. All three
splits ride the same durable partial and the same `assemble` verb — because
a pinned revision is an *enumerable, content-addressed set*, the same set
arithmetic that lets a single run resume also lets many partials of the same
pin merge into one verified bundle. Three shapes of the same idea:

- **Across sessions** — `--max-gb` (§6). One machine, many evenings.
- **Across people** — `--shard N/T`, below. Distinct starts, merge later.
- **Across disks that never meet** — `assemble --move`, below. Fetch a half,
  hand it over, keep a skeleton, fetch the rest.

#### Across people: cooperative shard keys

`--shard N/T` is an advisory transfer-order key for T collaborators. It does
not filter the expected set: every participant still proceeds through all
lanes and can independently finish the identical bundle. Files are assigned
deterministically to T lanes with longest-file-first byte balancing; participant
N fetches lane N first, then cycles through every other lane. For three people:

```bash
# Alice, Bob, and Carol use the same repo/revision and budget, but distinct starts.
darsay archive Qwen/Qwen3.8-27B --shard 1/3 --max-gb 20
darsay archive Qwen/Qwen3.8-27B --shard 2/3 --max-gb 20
darsay archive Qwen/Qwen3.8-27B --shard 3/3 --max-gb 20
```

The plan prints the chosen lane's file count, byte size, percentage, and full
lane order. This is whole-file scheduling: a normally sharded weight set
distributes well, while one monolithic weight file cannot yield three distinct
starting thirds without a different Range protocol.

Matching partials are then combined with no network access:

```bash
darsay --vault ./combined assemble /usb/alice/<bundle> /usb/bob/<bundle> /usb/carol/<bundle>
darsay --vault ./combined archive Qwen/Qwen3.8-27B
```

`assemble` requires identical repo/type/pin/expected inventories before it
creates a destination. It clone-copies or copies full payload files, re-hashes
them against the pin, merges portable cache metadata, keeps only the longest
copy of each matching `.incomplete`, and records an `assemble` session without
embedding source-machine paths in the ledger. The final `archive` invocation
continues any remainder—or, when assembly reached 100%, performs registration
with zero payload network bytes. `assemble --dry-run` prints that plan first —
what each partial would contribute, what would be hashed at dest, the disk
verdict for the copy, how far along the pin would be afterwards, and (with
`--move`) which source files would be released — and creates nothing, not
even the destination directory.

#### Across disks: `assemble --move` and skeletons

The cooperative case has every partial on hand at merge time. The two-disk
case is the mirror image: the disk with the bandwidth and the disk with the
room are *never mounted together*. A laptop can fetch half a 60 GB model at
the café, but only the big drive at home can hold it — and the laptop is full
of the half it already carried home. Deleting that half to make room for the
rest would normally re-download it: reconcile treats an absent verified file
as `missing` (a crash between file-write and ledger-write must heal), so a
plain `rm` is a re-fetch.

`assemble --move` is the honest way to free that space. It runs an ordinary
assembly into the big vault, then — *only for files the destination already
holds as `verified` (ledger + size, or hashed under `--rehash`)* — deletes
the source's copy and rewrites the source record from `verified` to
**`moved`**: verified, and now living in another vault. A partial with any `moved` file is a **skeleton**: the pin,
the expected inventory, and every recorded hash stay behind; the payload
bytes do not. The laptop can then fetch the *other* half without re-fetching
what it handed over, because a `moved` file is fetched by no one and
registers nowhere — it counts toward neither remaining network nor this
bundle's completeness.

```bash
# laptop, at the café — the first 30 GB
darsay archive Qwen/Qwen3.8-27B --max-gb 30

# laptop plugged into the big drive — hand the half over, keep the skeleton
darsay --vault /Volumes/big assemble ~/darsay/qwen--qwen3.8-27b/<rev> --move

# laptop, back at the café — the other 30 GB; the moved half is never re-fetched
darsay archive Qwen/Qwen3.8-27B --max-gb 30

# big drive again — the second hand-over completes it and dissolves the skeleton
darsay --vault /Volumes/big assemble ~/darsay/qwen--qwen3.8-27b/<rev> --move
darsay --vault /Volumes/big archive Qwen/Qwen3.8-27B          # registers, zero network bytes
```

Already `rsync`'d or `cp -a`'d the partial into the destination vault? Same
command. `assemble --move` copies nothing for files dest already holds,
trusts dest files the ledger already marks `verified` (size match), then
deletes the matching source bytes and leaves a skeleton. It does **not**
re-hash dest: dest's `verified` records travelled with the rsync, and
hashing dest from a laptop over SMB would pull the payload back across the
wire. Hash dest where it is a local disk (`assemble --rehash`, or run
assemble on the dest host). A second `--move` is a no-op for files already
`moved`. The destination path is the usual two-level layout
(`<vault>/<repo-slug>/<revision12>/`); rsync the bundle there, not into the
vault root:

```bash
rsync -a ~/darsay/qwen--qwen3.8-27b/<rev>/ /Volumes/big/qwen--qwen3.8-27b/<rev>/
darsay --vault /Volumes/big assemble ~/darsay/qwen--qwen3.8-27b/<rev> --move
```

The move is **verify-then-delete, per file**: dest must already have the
file as `verified` (ledger + size, or a dest-local `--rehash`) before the
source copy is deleted, so an interrupted or incomplete copy leaves the
source bytes untouched. A same-size bit-flip during rsync is the same bet
`archive` after rsync already makes; catch it with `darsay verify` on the
dest host, not by ingesting dest over SMB. If the moved bytes ever reappear at the source
(restored by hand from the big vault), reconcile hashes and re-adopts them as
`verified`: the `moved` record is a hint, and matching bytes always win. Bytes
that come back *wrong* are removed and the record stays `moved` — a bad
restore never re-fetches what the other vault already holds. A source with
every file moved out holds no payload byte and is removed, leaving exactly
what a plain `mv` would have. Any other source stays a skeleton and reports
what it still owes to fetch — or still holds, when the destination could not
verify a copy.

When an `archive` run has fetched everything it can *here* but some files are
`moved` (or when it is run on a fully-drained skeleton), it does not error and
does not register: it pauses cleanly with `end_reason: "moved"`, exit 10, and
the hint to assemble the halves into one vault. A skeleton's `list` row counts
what exists anywhere and names the moved amount:

```
archiving: 52% (28.9/55.6 GB, 7/22 files verified, 27.8 GB moved out)
```

`moved` is machine-local transfer state, like every other per-file status: it
lives in `transfer.json` (§4), is excluded from `.mvb.tar` exports, never
reaches a manifest, and — losing the ledger — degrades a skeleton to a plain
partial that simply re-fetches. It is the CLI-side, *verified* counterpart to
the darsay.io board's typed "who has a copy" string: not a human claim, but
recorded hashes for bytes this pin handed to a named vault.

## 6. Session budgets

| Flag | Meaning |
|---|---|
| `--max-gb N` / `--max-bytes SIZE` (`500M`, `20G`) | stop cleanly once network bytes this session exceed the cap |
| `--max-minutes N` | stop cleanly at the deadline |
| `--min-free SIZE` | pause cleanly once destination free space drops below SIZE (default 2 GiB via config; `0` disables) |
| `--yes` / `-y` | start without asking when the disk preflight says the transfer cannot finish (a non-interactive run never asks) |
| `--max-rate SIZE` | cap network transfer at SIZE per second across every worker (`5M`, `500K`; default unlimited via config; `0` lifts a configured cap) |
| `--max-offline DURATION` | keep waiting for a lost network up to DURATION before pausing cleanly (`30m`, `2h`; default 1 h via config; `0` pauses at the first failure) |
| `--revision REF` | pin a branch, tag, or commit instead of `main`; the resolved commit is frozen for every later run |
| `-n` / `--dry-run` | pin (if new) + reconcile + plan report; move no payload bytes. Every writing command takes the same flag |
| `--rehash` | re-verify every present file by digest instead of trusting the ledger (periodic paranoia for months-long archives) |
| `--jobs N` | parallel transfer streams (default 4): N small files at once, and N large files at once with one hash thread verifying finished files alongside; `1` restores a single stream, still hashing in parallel |
| `--shard N/T` | advisory cooperative order: byte-balance files into T lanes and fetch lane N first; the expected set is unchanged |

Budgets are approximate and checked at received-chunk boundaries: every
active stream and the chunk that crosses a cap may overshoot it. In-flight
files are left as resumable partials, counted toward the next session.
`darsay list` grows
an in-progress row for bundles with a ledger but no manifest —
`archiving: 61% (34.1/55.6 GB, 9/15 files verified)` — so the vault's
overall state is visible without running anything.

### The free-space floor

Budgets bound what one session *takes*; the floor bounds what it *leaves*.
An archive left running unattended must never fill the destination
partition — a full disk breaks everything else on the machine. So
`archive` holds a **free-space floor**, 2 GiB unless configured, and
pauses cleanly rather than cross it:

- **Before each file**, the floor checks headroom: a file whose remaining
  bytes (a banked partial only needs the rest) would not fit above the
  floor is never begun. The session pauses at the file boundary with
  `disk: model-00023-of-00141.safetensors needs 5.0 GiB more, but only
  3.9 GiB is free above the 2.0 GiB floor`. Files are transferred
  smallest first, so once one does not fit nothing after it does either.
- **During a file**, the floor is checked wherever budgets are checked —
  every transfer callback and hash chunk — but the filesystem is probed
  at most every 2 s, so the guard never touches the hot path. It is
  measured at the bundle directory, so it follows a `--vault` on a
  different disk. This catches what the headroom check cannot: another
  writer filling the disk mid-shard.
- Either trips a **sticky** stop: every worker and the main thread stop
  at their next check, in-flight chunks are banked exactly as for a
  budget stop, and the session ends with `end_reason: "disk"`. The CLI
  exits 10 with the reason (`disk: destination free space fell below the
  floor (1.9 GiB free < 2.0 GiB floor)`) and a "free disk space, then
  re-run" hint.
- **`ENOSPC` is the same pause.** Should a write still hit a full disk —
  the floor disabled, a race with another writer — the session ends the
  same way (`disk: destination is full — no space left on device while
  writing …`), never as an error. If the disk is too full even for the
  ledger rewrite, the pause says so and the next run reconciles the
  payload from the bytes on disk.
- The floor is priced into headroom: plan and `estimate` verdicts compare
  free space *above the floor* to remaining bytes, and print it —
  `needs 40.0 GiB, free 45.0 GiB (10.0 GiB floor) — INSUFFICIENT`. An
  insufficient `archive` plan goes on to say where it will stop:

  ```text
  WARNING: disk preflight is insufficient; the transfer will pause at the free-space floor
    after about 381.4 GiB more (67 of 140 remaining files), roughly 9h at 12.3 MiB/s.
    Free space (or move the vault to a larger disk), then re-run to continue.
  Continue anyway? [Y/n]
  ```

  The pace is the rate cap or the ledger's earlier sessions, whichever is
  slower; the question is asked only on a terminal (Enter proceeds, `n`
  pauses cleanly before any byte moves, exit 10) and `--yes` skips it.
  Pipes and cron never see it. While the transfer runs short of space,
  the panel's second line ends in `· free 381.2 GiB`.
- Resume is the ordinary command: nothing about the pin, ledger, or
  partials is floor-specific. `--min-free 0` disables the floor (and the
  headroom check) for one run when an operator is watching.

Any disk-space check is a race against other writers; the floor is a
margin, not a reservation. Two gigabytes covers the ledger rewrite, the
Hub client's cache bookkeeping, and the rest of the machine's daily churn.
A shared partition or a fast link wants more — set it once (below) and
forget it.

### Network loss

A laptop walks out of Wi-Fi range, a router reboots, DNS disappears, the
Hub answers 503 for a minute. None of that is an error in the archive;
it is a pause the tool should take by itself. So a transport failure that
the provider classifies as *transient* (`SourceProvider.transient_network_error`
— for Hugging Face: httpx connect / read / timeout errors, `RemoteProtocolError`,
transient Hub statuses 408 / 425 / 429 / 5xx, and a stream cut short before
its expected size) does not propagate. Instead:

- Whatever arrived is already banked: the provider's `.incomplete`
  partial is durable (§10), and the next attempt resumes it with a Range
  request rather than starting over.
- The worker waits on a shared **link** state — 2 s, 4 s, 8 s, 15 s, then
  every 30 s, or until another worker's bytes prove the link is back — and
  tries the same `download_file` again. Several small-file workers hitting
  the same outage share one outage record.
- The panel says so. The time-remaining slot reads `offline` (amber) with
  `retry in 8s · 2 min 10s offline` in the tail, `reconnecting` while an
  attempt is in flight, and the file line keeps its banked bytes
  (`27.3%  1.2 GiB / 4.4 GiB`) instead of resetting to zero. One
  scrollback line marks the drop — `Network unreachable (DNS lookup
  failed) — waiting to reconnect; verified and partial bytes are banked.`
  — and one marks the return — `Reconnected after 4 min 12s (7 attempts).`
  Log mode prints the same two lines, and its status line carries the
  same words: `offline  retry in 8s · 2 min 10s offline`.
- The first network byte received by an attempt begun during the outage —
  a retry, or a fresh file — ends it. Bytes still draining from a stream
  that was open before the drop do not.
- Budgets, the floor, and Ctrl-C keep their meaning while waiting: the
  wait loop checks the stop controller every 0.2 s, so a time budget
  still expires on schedule and the first Ctrl-C still pauses within a
  slice.
- After `max_offline` — 1 h unless configured — the session pauses
  cleanly: `end_reason: "offline"`, exit 10, the reason on the terminal
  (`offline: network unreachable for 1h (DNS lookup failed)`), and an
  "once the network is back, re-run" hint. `--max-offline 0` pauses at
  the first failure for an operator who would rather know now. Sessions
  count `reconnects`; the ledger logs `network_lost` / `network_restored`
  events per file.

The Hub client's own retry commentary (`Error while downloading from …
Trying to resume download…`) is filtered while a transfer runs — darsay
tells that story itself, in one voice. A host that cannot be reached at
pin time, before anything durable exists, ends in one line for `estimate`
and `archive` alike: `error: cannot reach Hugging Face to resolve … — DNS
lookup failed. Check the connection and re-run.`

### The rate cap

An archive left running all day should not own the connection. `max_rate`
paces every received chunk through one leaky bucket shared by every
worker: a chunk is always accepted (it is already in memory) and the
caller sleeps off the debt it adds, so the running average never exceeds
the cap while TCP flow control slows the sender upstream. Debt drains at
the cap and never turns into credit, so a cold start, a hashing pause, or
a reconnect earns no burst. Under a cap the Hub
client reads in chunks worth about a quarter second (never below 64 KiB)
so pacing sleeps are short and the panel's rate stays smooth; pacing
sleeps run in 0.2 s slices and yield to a stop request at once. The plan
block prices the cap — `rate: capped at 5.0 MiB/s — about 40h for the
remaining 703.8 GiB` — and the panel tail shows `· cap 5.0 MiB/s`.
`--max-rate 0` lifts a configured cap for one run.

### Configuration

The floor, the rate cap, and the offline patience belong to a machine (or
an archive drive) rather than to one run, so darsay reads a small TOML
config. Configuration is **operator preference, never archival fact**:
nothing in it changes what a bundle records, only how this machine
behaves while producing one. Config files therefore live outside bundles
and are never exported. Layers resolve in order, later wins:

| Layer | Where |
|---|---|
| defaults | `min_free = 2 GiB`, `max_rate = 0` (unlimited), `max_offline = 1h` |
| user file | `$DARSAY_CONFIG`, else `$XDG_CONFIG_HOME/darsay/config.toml` (`~/.config/darsay/config.toml`) |
| vault file | `<vault>/config.toml` — travels with an archive drive, so the drive carries limits suited to its own disk |
| environment | `$DARSAY_MIN_FREE`, `$DARSAY_MAX_RATE`, `$DARSAY_MAX_OFFLINE` |
| flag | `--min-free SIZE`, `--max-rate SIZE`, `--max-offline DURATION` |

```toml
[transfer]
min_free = "10G"      # bytes, or a binary K/M/G/T suffix; 0 disables the floor
max_rate = "5M"       # bytes per second ("5M" or "5M/s" = 5 MiB/s); 0 is unlimited
max_offline = "1h"    # seconds, or an s/m/h/d suffix; 0 pauses at the first failure
```

Unknown keys in a known table warn on stderr — a typo (`min_fre`) must not
silently disarm a guard the operator believes is set. Unknown tables are
ignored so a vault config written by a newer darsay still loads here. A
malformed file or value is an error, not a fallback to the default.
`darsay config` prints the effective value of every setting and the layer
that set it (`--json` for scripts); it is the first thing to run when a
floor is not what you expected. `<vault>/config.toml` is a vault-root
file, not a bundle, so `list`, `du`, and exports never see it.

## 7. Manifest impact

| Field | Meaning |
|---|---|
| `source.transfer` | `{sessions, started, completed, bytes_network, bytes_adopted, bytes_local_sources, retries}` — the durable summary of how the payload got here. A one-shot archive is simply `sessions: 1`. |
| `source.download_timestamp` | Completion time of the transfer. `source.transfer.started` is first-byte time. |
| `source.mirrors_used` | `local:<bundle_id>` entries when sibling bundles supplied matching blobs. |
| `validation.checksum_verification.method` | `"per-file at download completion"` — *when* hashes were established; `at` is registration time. |

Per-file records in `inventory.files` take hashes from the ledger, not a
final mega-pass.

## 8. Failure modes

| Failure | Outcome |
|---|---|
| Power loss / crash mid-file | Partial resumes; at most one file's ledger entry is lost and reconciliation re-derives it. |
| `transfer.json` deleted or corrupt | Rebuilt by reconciliation: hash present files, adopt matches. Slow, never lossy. |
| Upstream repo gated or deleted between sessions | CDN fetches fail with the clean gated/not-found messages; the partial bundle stays resumable if access returns. Logged in `events`. |
| Persistent digest mismatch | Retried once, then recorded; registration proceeds with `checksum_verification: fail` and the warning — same contract as the one-shot flow. |
| Disk filling mid-session | Plan-phase preflight prices the free-space floor (§6), says where the transfer will stop, and asks on a terminal; the transfer pauses cleanly (`end_reason: disk`, exit 10) before a file that cannot fit above the floor, or the moment free space drops below it, leaving the margin for the rest of the machine. Clear space and rerun. With the floor disabled, a mid-session `ENOSPC` is the same clean pause with state intact. |
| Network drops mid-session | Bytes received so far are banked in the partial; the panel reads `offline` and retries on a 2 → 30 s schedule (§6) while budgets, the floor, and Ctrl-C keep working. Back within `max_offline` (1 h): the file resumes with a Range request and the outage is one scrollback line. Longer: clean pause, `end_reason: offline`, exit 10; rerun once connected. |
| Two concurrent runs | Second exits on the live lock. |
| Partial bundle copied or moved | Relative ledger/cache state resumes at the new vault; an inherited lock is reclaimed only when the physical directory identity changed. rsync then `archive` / `assemble --move` trusts dest ledger + size and fetches only the remainder. Hash dest on the dest host (`--rehash` / `verify`). |
| Cooperative inputs disagree | `assemble` rejects them before creating a destination; repo, type, full pin, and expected inventory must all match. |
| `assemble --move`, dest does not have the file as verified | That file's source bytes are kept, not deleted (verify-then-delete, per file, §5); only files dest holds as `verified` (ledger + size, or `--rehash`) are released, and a skeleton still holding such a file is never dissolved. |
| Moved bytes reappear at a skeleton | Reconciliation hashes and re-adopts them as `verified`; the `moved` record is only a hint. Bytes that fail the pin's checks are removed and the record stays `moved`. |
| Skeleton ledger lost | Degrades to a plain partial: what is on disk is re-adopted, what was moved out is re-fetched. Never corrupt, only slower. |

## 9. Considered and rejected

- **rsync's rolling-checksum delta transfer inside files.** The source is
  immutable and content-addressed; there is no "changed file" to delta
  against, only absent bytes. Cross-revision similarity is real but
  weight retraining changes shards wholesale, so whole-file local-source
  reuse (§5.2) captures nearly all of the win at none of the complexity.
- **Staging directory + atomic rename into the vault.** The vault already
  has an atomic registration point — `manifest.json` — and downloading
  in place is what makes partials resumable and visible to `list`. A
  staging dir would double peak disk for TB bundles and buy nothing.
- **A shared, HF-style global blob cache with links into bundles.** Bundles
  must remain independent, self-contained file trees (museum-grade: a
  bundle survives the vault around it being reorganized). Reflink adoption
  gives the deduplication without the coupling.
- **Multi-source / torrent-style fetch.** Out of scope; `mirrors_used`
  leaves the manifest room for it if a second origin ever exists.

## 10. Implementation notes and deliberate drift

- `huggingface_hub` 1.18 replaced stable local-dir incomplete names with
  process-unique temporary files and removes them on failure. Because
  darsay holds a stronger per-bundle lock, its transport compatibility
  wrapper restores a stable bundle-local incomplete file, then delegates the
  actual HTTP Range/retry work to the Hub client's `http_get`. Core now
  requires `huggingface_hub>=1.23`, which also reports actual network bytes
  separately from reconstruction progress.
- The design originally treated Xet as transparently resumable. Current
  `hf_xet` abort behavior did not preserve a usable mid-file checkpoint in
  validation, so v0.5.0 forces the durable HTTP/Range route for archival
  transfers. This can be revisited when Xet exposes durable cross-process
  continuation without weakening byte budgets or Ctrl-C semantics; what
  that would take, and why the case for it weakened once large files
  gained `--jobs` streams, is in
  [DESIGN.md](DESIGN.md#transfer-concurrency).
- Gated repositories marked gated at pin time receive a read-authorization
  check before any ledger or payload is created. If access disappears only
  after pinning, the partial is retained and its error is recorded.
- Cooperative lanes and `assemble` were added during implementation. They
  operate above the same pinned expected set: order and offline aggregation
  are acceleration only and cannot change the registered payload.
- Skeletons (`assemble --move`, the `moved` file state) reuse that same
  machinery: `moved` is a fourth per-file status beside verified / partial /
  missing, entered only through `assemble --move`, read by the same
  reconcile → plan → transfer loop. No new verb, no new schema, no change to
  the manifest, the export format, or the catalog — a skeleton is a partial
  whose verified bytes moved out, and the registration bar (every file
  verified *here*) does not move.

## See also

- [MANIFEST.md](MANIFEST.md) — `source.transfer` fields and local-source
  provenance.
- [QUANTIZATION.md](QUANTIZATION.md) — deciding *what* to archive; its
  `archive --include` subsets compose with this design (the
  expected set is filtered before planning, recorded as `source.subset`).
- [DESIGN.md](DESIGN.md) — why the Hugging Face provider keeps
  `huggingface_hub` as transport (`hf_hub_download` owns
  authentication/Range/retry; this design owns state, verification, and
  convergence).
- [SOURCES.md](SOURCES.md) — acquisition providers; transfer.py does not
  import a hosting-service client.

---

[Documentation index](README.md)

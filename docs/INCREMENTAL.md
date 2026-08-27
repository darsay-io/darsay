<p align="center">
  <a href="GETTING-STARTED.md">Start here</a> ·
  <a href="CONCEPTS.md">Concepts</a> ·
  <a href="../examples/README.md">Examples</a> ·
  <a href="README.md">All docs</a> ·
  <a href="../README.md">README</a>
</p>

# Incremental archiving — idempotent, resumable transfer

> **In one sentence.** `archive` is interruptible by default. Rerun the
> same command. `--max-gb` stops cleanly. `--shard N/T` shares the work.
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

## 1. Why this can beat rsync at its own game

rsync solves a harder problem than ours: its source is *mutable*, so it must
interrogate both sides (size/mtime quick-check, then rolling checksums) to
discover what changed. Our source is a **pinned, content-addressed git
revision**: once `archive` resolves ref → commit on the first run, the
transfer set is frozen and fully enumerable from one metadata call —
every file's path, exact size, and upstream digest (LFS SHA-256 for large
files, git blob SHA-1 for small ones) are known *before a single payload
byte moves*.

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
   Same doctrine as hydration: derived state must never hold archival truth.
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
5. **Every stop is clean, every run converges.** Budgets, Ctrl-C, network
   loss, power loss — all leave a state a later run continues from. The
   state file is written atomically (temp + rename) after every file
   completion, so the worst possible loss is one in-flight file's progress
   record, which reconciliation recovers anyway.
6. **Record, don't fabricate — for the transfer itself.** Sessions, bytes
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
| `.incomplete` bytes in the payload's `.cache/huggingface/` | `partial` | resume via Range from current length |
| Absent | `missing` | fetch |
| Present but wrong size, or hash mismatch | `mismatch` | delete, log the event, treat as missing |

Reconciliation is what makes the system self-healing: a lost ledger, a
crash between file-write and state-write, or bytes copied in by hand all
converge to the correct classification from evidence.

**Plan** — report the arithmetic before moving anything: files and bytes
verified / partial / missing, bytes already banked inside partials,
estimated remaining network transfer, and a disk preflight of remaining
bytes against free space (reusing `estimate`'s headroom check). `--dry-run`
stops here — this is the rsync `-n` of the system, and the answer to "what
have we got, what remains" at any moment.

**Transfer** — fetch `remaining` in ascending size order (stable tie-break
by path): configs, tokenizer, card, and license complete in the first
minutes — making the partial payload inspectable early — and each budgeted
session completes whole files rather than leaving many large stubs.
While bytes move, a TTY shows one panel for the *whole* payload — percent
complete, bytes in / total, smoothed rate, time remaining, files done, the
file now in flight — because a per-file bar resetting at 0% on every shard
is the wrong unit for a 50 GB archive. Piped or logged runs emit the same
facts as a status line every 10 seconds. `DARSAY_PROGRESS=0` disables it;
`DARSAY_PROGRESS=line` forces the log form even on a TTY.
Per file: check local sources (§5) → else `hf_hub_download` at the pinned
commit (the library's `.incomplete` + Range machinery provides byte-level
resume) → hash → compare to upstream digest → append to ledger →
atomically rewrite `transfer.json`. A digest mismatch on a freshly
completed file is retried once (transit corruption), then logged as an
upstream mismatch and set aside — it never silently blocks the rest of the
transfer. Small files (< 8 MiB) may fetch through a bounded worker pool
(`--jobs`, default 4 — dataset bundles with thousands of parquet shards
need this); large files download sequentially, one saturating stream at a
time. darsay deliberately selects `hf_hub_download`'s HTTP path even
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
reason — `complete` / `budget` / `interrupt` / `assemble` / `error` — bytes
from network, bytes adopted, files completed, host). SIGINT finishes the
in-flight state write and exits cleanly with the resume hint. Cooperative
archive sessions also record their advisory `shard: "N/T"`; assembly records
the number of merged inputs.

**Exit codes:** `0` — bundle completed and registered by this invocation;
`10` — clean partial stop, more remains (budgets and interrupts), so
wrappers can loop; `1` — error.

```bash
# unattended completion over nightly cron: rerun until exit 0
darsay archive Qwen/Qwen3.8-27B --max-minutes 240 || [ $? -eq 10 ]
```

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
                    "source": "network", "attempts": 1}
    // source: "network" | "adopted" | "local:<bundle_id>"
  },
  "sessions": [
    {"started": "…", "ended": "…", "end_reason": "budget",
     "bytes_network": 10737418240, "bytes_adopted": 0,
     "bytes_local_sources": 4966786096, "retries": 0,
     "files_completed": 3, "host": "…", "shard": "1/3"}
  ],
  "events": [
    {"at": "…", "path": "…", "event": "digest_mismatch", "detail": "…"}
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

### Cooperative shard keys and offline assembly

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

Matching partials can then be combined with no network access:

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
with zero payload network bytes.

## 6. Session budgets

| Flag | Meaning |
|---|---|
| `--max-gb N` / `--max-bytes SIZE` (`500M`, `20G`) | stop cleanly once network bytes this session exceed the cap |
| `--max-minutes N` | stop cleanly at the deadline |
| `--dry-run` | pin (if new) + reconcile + plan report; move no payload bytes |
| `--rehash` | re-verify every present file by digest instead of trusting the ledger (periodic paranoia for months-long archives) |
| `--jobs N` | small-file worker pool width (default 4; large files always sequential) |
| `--shard N/T` | advisory cooperative order: byte-balance files into T lanes and fetch lane N first; the expected set is unchanged |

Budgets are approximate and checked at received-chunk boundaries: active
small-file workers and the chunk that crosses a cap may overshoot it. The
in-flight file is left as a
resumable partial, counted toward the next session. `darsay list` grows
an in-progress row for bundles with a ledger but no manifest —
`archiving: 61% (34.1/55.6 GB, 9/15 files verified)` — so the vault's
overall state is visible without running anything.

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
| Disk fills mid-session | Plan-phase preflight warns before starting; a mid-session `ENOSPC` ends the session as `error` with state intact. |
| Two concurrent runs | Second exits on the live lock. |
| Partial bundle copied or moved | Relative ledger/cache state resumes at the new vault; an inherited lock is reclaimed only when the physical directory identity changed. |
| Cooperative inputs disagree | `assemble` rejects them before creating a destination; repo, type, full pin, and expected inventory must all match. |

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
  continuation without weakening byte budgets or Ctrl-C semantics.
- Gated repositories marked gated at pin time receive a read-authorization
  check before any ledger or payload is created. If access disappears only
  after pinning, the partial is retained and its error is recorded.
- Cooperative lanes and `assemble` were added during implementation. They
  operate above the same pinned expected set: order and offline aggregation
  are acceleration only and cannot change the registered payload.

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

# Skeletons — archiving a bundle in halves when one disk cannot hold it

| | |
|---|---|
| **Author** | TBD |
| **Date** | 2026-08-29 |
| **Status** | Implemented (unreleased) — shipped as `assemble --move` + the `moved` file state. User-facing docs: [INCREMENTAL.md §5](../INCREMENTAL.md#across-disks-assemble---move-and-skeletons), [CONCEPTS.md](../CONCEPTS.md#pin), [examples](../../examples/README.md#archive-in-halves-across-two-disks). |
| **Audience** | darsay CLI implementers; readers of `docs/INCREMENTAL.md` |
| **Related** | darsay 0.12.0 · `transfer_version` 1 · catalog schema 1.0.0 (unchanged) · manifest schema (unchanged) |

This was a **proposal**; it now records the design rationale behind the
shipped feature. Decisions are under [Key Decisions](#key-decisions);
forks that were weighed are under [Open Questions](#open-questions). Two
Open Questions were settled in the build: the skeleton **dissolves** when
fully drained (Q1), and there is **no manual claim-based verb** in this
cut (Q2) — `assemble --move` verifies every byte it deletes.

---

## Overview

Today a large archive can be spread across sessions (`--max-gb`), across
people (`--shard N/T` + `assemble`), and across machines (copy the partial
directory, re-run). It cannot be spread across **disks that are never
mounted at the same time** — the case where the laptop can hold half a
model, the big vault is somewhere else, and the network is only where the
laptop is.

This proposal adds exactly one thing to the transfer model: a fourth
per-file ledger state, **`moved`** — *verified, and the bytes now live in
another vault.* It is entered through one flag, **`assemble --move`**, and
read by the existing reconcile → plan → transfer loop. A partial whose
verified bytes have moved out is a **skeleton**: the pin, the expected
inventory, and every recorded hash stay behind; the payload does not.
`archive` on a skeleton fetches only what no vault has yet.

```bash
# laptop, at the café: half tonight
darsay archive Qwen/Qwen3.8-27B --max-gb 30

# laptop plugged into the big drive: hand the half over, keep the skeleton
darsay --vault /Volumes/big assemble ~/darsay/qwen--qwen3.8-27b/c1899de289a0 --move

# laptop, back at the café: the other half — nothing re-fetched
darsay archive Qwen/Qwen3.8-27B --max-gb 30

# big drive again: second hand-over completes it; the skeleton is dissolved
darsay --vault /Volumes/big assemble ~/darsay/qwen--qwen3.8-27b/c1899de289a0 --move
darsay --vault /Volumes/big archive Qwen/Qwen3.8-27B        # registers, zero network bytes
```

No new verb, no new schema, no change to the catalog, the manifest, or the
`.mvb.tar` format.

---

## Background & Motivation

### Current state

The transfer loop (`docs/INCREMENTAL.md` §3) is set arithmetic over a pinned
inventory: `remaining = expected − verified`. Per file, `transfer.json`
records `status: "verified"` (with hashes) or `status: "missing"`; a Range
partial is inferred from a `.incomplete` file in the payload cache.
Reconciliation (`transfer.reconcile`) rebuilds all of that from bytes on
disk, which is why the ledger is disposable.

The relevant rule in reconcile is the second row of the table:

> Ledger says verified, size matches on stat → trust.
> Ledger says verified, file absent → **demoted to missing** (event
> `verified_file_missing`).

That rule is correct for its purpose — a crash between file-write and
ledger-write must not leave a phantom "verified" — and it is precisely what
makes the two-disk workflow impossible today. Delete a verified shard to
make room and the next run downloads it again.

### The workflow that does not fit

1. Laptop with a small disk archives half of a 60 GB model at the only
   place with bandwidth.
2. Laptop travels to the big vault (a USB drive, a NAS) and hands the half
   over — `cp -a` or `assemble`.
3. Laptop returns to the bandwidth. Its disk is still full of the half it
   already handed over. Deleting those files means re-downloading them.

### What people reach for, and why it is imprecise

- **`--shard N/T`.** An *advisory order*: byte-balanced lanes, and every
  participant still walks all lanes ("any one of them can finish the bundle
  alone"). It does not know what is on the USB. Lane 2 will still include
  files already handed over once lane 1 is exhausted; lane sizes do not
  match what actually fit on the disk; and it does nothing about the full
  disk.
- **`--include`.** A *curatorial* subset, recorded in the manifest as a
  different work (`source.subset`). "The half I have not moved yet" is
  transfer logistics, not curation, and both halves must end up as one
  full-repo bundle.
- **Delete files and hope.** Reconcile demotes them to `missing`, by design.

### Why a new file state and not a smaller trick

Two alternatives that avoid a state were considered and rejected
([Alternatives](#alternatives-considered)): shrinking the skeleton's
`expected` list, and a standalone "forget these files" verb. Both either
lie to `list` about the pin's size, break `assemble`'s identical-inventory
check, or introduce a "trust me" operation with nothing verifying that the
bytes really exist elsewhere. `moved` is the honest record: the expected
set stays the pinned truth; per-file state is where logistics already
live (`source: network | adopted | local:<id>`).

---

## Goals & Non-Goals

### Goals

- Archive one pin across two disks that are never mounted together, with
  **zero re-fetched bytes** and no manual bookkeeping.
- The hand-over is **verified before anything is deleted**: a file leaves
  the source only after the destination has hashed it against the pin.
- `list`, `estimate`, the transfer plan, and the catalog overlay stay honest
  about a skeleton: how much of the pin exists anywhere, how much is here,
  how much has moved out, how much is still to fetch.
- Nothing archival depends on the new state. Lose the ledger and the
  skeleton degrades to a plain partial (re-fetch, not corruption).
- One flag, one state, one reconcile row, one plan bucket.

### Non-goals

- Moving *registered* bundles between vaults (that is `export` / `import`,
  or `cp -a` — the manifest travels with the payload).
- A "forget these files, I copied them somewhere" verb with no verification
  ([Open Questions](#open-questions) — deliberately deferred).
- Remote destinations (SSH, HTTP). The destination is a vault path; mount
  it. `assemble` already works this way.
- Recording *where* the bytes went as structured data. The ledger never
  holds source-machine absolute paths; a free-text event is enough.
- Any change to `catalog.json`, the manifest, or `.mvb.tar`.

---

## Key Decisions

1. **A fourth per-file state, `moved`, next to `verified` / `missing` /
   (inferred) `partial`.** A `moved` record is a verified record whose
   bytes are absent on purpose: it keeps `size`, `sha256`, `blake3`,
   `git_sha1`, `verified_against_upstream`, `verified_at`, `source`,
   `attempts`, and adds `moved_at`. Nothing else in the ledger changes.
   `transfer_version` stays `1` — an older darsay reads `moved` as
   "unknown status, file absent" and re-fetches, which is the graceful
   degradation the doctrine already promises.

2. **The only way in is `assemble --move`.** `assemble` is already the verb
   that carries partial bytes between vaults and already re-hashes them at
   the destination (`reconcile(..., rehash=True)`, then a reconcile per
   merged input). `--move` adds one step after that: for every source file
   whose *destination* record is now `verified`, delete the source bytes
   and rewrite the source record as `moved`. A file the destination could
   not verify stays put. This is `mv` semantics with rsync's
   `--remove-source-files` guarantee, and it makes the "I already `cp -a`'d
   it into the big vault" case work too: `assemble --move` against a
   destination that already holds the bytes copies nothing, verifies
   everything, and then marks the source.

3. **A skeleton is a partial.** No new bundle-level status. `bundle_records`
   still reports `status: "partial"`; the catalog overlay still says
   `partial`; `--next` still prefers it; `find_resume` still resumes it.
   "Skeleton" is the documentation word for "a partial with any `moved`
   file", the way "partial" is the word for "a pin with any file not
   verified". The enums stay small.

4. **`moved` is never fetched and never registers.** Reconcile trusts a
   `moved` record whose file is absent. Transfer skips it exactly as it
   skips `verified`. Registration requires every file `verified` *here*
   (`file_records` raises otherwise) — the bar does not move. A skeleton
   that has fetched everything it can ends its session cleanly with
   `end_reason: "moved"` and exit 10: *more remains — in another vault.*

5. **A `moved` file that reappears is adopted.** If the bytes come back
   (copied by hand, restored from the big vault), reconcile hashes and
   promotes them to `verified` like any present-but-unrecorded file. The
   `moved` record is a hint, and bytes always win.

6. **The doctrine gains one sentence, not an exception.** "Bytes are the
   authority; transfer state is a disposable cache" still holds: a `moved`
   record is the one fact the bytes cannot tell you — *that they are in
   another vault* — and losing it costs network, never truth. No manifest
   is ever assembled from a `moved` record.

7. **When everything has moved, the skeleton dissolves.** After
   `--move`, if every expected file is `moved` (nothing verified here,
   nothing partial, nothing missing), the source directory holds only a
   ledger and empty cache dirs; `assemble` removes it and says so. The
   source is then exactly what `mv` would have left. While anything remains
   to fetch, the skeleton stays.

8. **Remaining is remaining-to-fetch.** `remaining_network` excludes
   `moved` bytes in the plan, `list`, `estimate`, and the catalog overlay.
   The percent on a `list` row counts what exists *anywhere* (verified here
   + partial + moved), because that is what "how far along is this pin" means
   to the person who started it. The moved amount is printed beside it.

9. **Partials stay with the source.** `--move` moves verified files only.
   A `.incomplete` is the source's work in progress; the source resumes it
   at the next `archive`. `_merge_transfer_caches` behaves as today (the
   destination gets a copy of the longest partial, harmless and
   already-existing behaviour); the source's copy is not deleted.

---

## Proposed Design

### The state machine, per file

```
                 fetch + hash            assemble --move
   missing ────────────────────► verified ───────────────► moved
      ▲                             │  ▲                     │
      │   verified_file_missing     │  │   adopt (hash)      │  bytes reappear
      └─────────────────────────────┘  └─────────────────────┘
      (unintended absence)                 (intended absence, bytes back)
```

`partial` is unchanged: inferred from a `.incomplete` in the payload cache
for a file whose record is `missing`.

### Reconcile

`transfer.reconcile` gains one row. Current table (`docs/INCREMENTAL.md`
§3) with the addition in bold:

| Local evidence | State | Action |
|---|---|---|
| Ledger says verified, size matches on stat | `verified` | trust (or re-hash under `--rehash`) |
| Present, size matches, no ledger entry | `unverified` | hash now; matching digest → **adopt**; mismatch → demote |
| **Ledger says moved, absent** | **`moved`** | **skip — the bytes are verified in another vault** |
| **Ledger says moved, present** | `unverified` | hash now; matching digest → adopt as `verified` (the bytes came back) |
| `.incomplete` bytes in the payload cache | `partial` | resume via Range |
| Absent | `missing` | fetch |
| Present but wrong size, or hash mismatch | `mismatch` | delete, log, treat as missing |

In code this is one early branch before the existing
`verified_file_missing` demotion:

```python
if state.get("status") == "moved" and not path.is_file():
    continue                       # intended absence; nothing to fetch here
```

The present-and-`moved` case falls into the existing "present, size
matches" adoption path unchanged — `_verified_record` produces a fresh
`verified` record and the `moved_at` key disappears with it.

`--rehash` has nothing to hash for a `moved` file and leaves it alone.

### Plan

`transfer_plan` grows one bucket. Shape today → proposed:

```jsonc
{
  "files": {"verified": 7, "moved": 7, "partial": 1, "missing": 7, "total": 22},
  "bytes": {"verified": 0, "moved": 27811000000, "partial": 1200000000,
            "missing": 26600000000, "total": 55611000000,
            "remaining_network": 25400000000},
  "complete": false,          // every file verified HERE (unchanged meaning)
  "fetched": false            // NEW: nothing left to fetch here (verified + moved == total)
}
```

`print_plan` gains a line, printed only when the bucket is non-zero so
ordinary partials look exactly as they do today:

```
Transfer plan:
  verified: 0/22 files, 0 B
  moved:    7 files, 27.8 GB — verified in another vault (assemble to register)
  partial:  1 files, 1.2 GB banked
  missing:  14 files; estimated network remaining 25.4 GB
  disk:     needs 25.4 GB, free 31.0 GB (2.0 GB floor) at /Users/jn/darsay — OK
```

`estimate`'s static bar (`estimate.py`, `_classify` → bar) gets the same
bucket, rendered like `verified`/`unverified`/`partial` with its own
label; `still to fetch` already reads `remaining_network`.

### Transfer

`transfer_all` skips `moved` exactly as it skips `verified` (the one
comparison at `transfer.py` "remaining = [… if status != 'verified']"
becomes `not in ("verified", "moved")`). `transfer_groups` (shard lanes)
is unchanged — lanes are computed over `expected`, and a moved file in a
lane is simply skipped.

After the lanes loop, `archive` (`archiver.py`) currently treats "not
complete" as a `RuntimeError`. New rule, raised at the end of
`transfer_all` — **inside** its `with _live_transfer(display)` block, so
the panel's closing record line (which reads `complete` on a normal exit)
instead takes the `CleanStop` branch and prints `paused: moved` via the
existing `_STOP_VERDICTS` fallback:

```python
plan = transfer_plan(payload_dir, ledger)
if plan["fetched"] and not plan["complete"]:
    raise CleanStop("moved", "every file is verified here or moved to another vault; "
                             "assemble into the vault that holds the rest to register")
```

This goes through the existing `PartialTransfer` path: session
`end_reason: "moved"`, exit 10, and the CLI hint map
(`cli.py`, `{"disk": …, "offline": …}.get(stop.reason, "Re-run")`) gains
one entry:

```
Archive paused cleanly (moved: every file is verified here or moved to another vault; …).
Partial bundle: /Users/jn/darsay/qwen--qwen3.8-27b/c1899de289a0
Assemble this skeleton into the vault that holds the moved files, then run archive there to register.
```

### `assemble --move`

`assemble_partials(partials, vault, progress, *, move=False)`. The existing
body runs unchanged through the final `_merge_transfer_caches`. Then, if
`move`:

```python
for source_dir, source_ledger in sources:
    moved_files, moved_bytes = release_moved(
        source_dir, source_ledger, destination_ledger=ledger, root=root)
    ...
```

`release_moved` (new, `transfer.py`):

1. For each `path` whose **destination** record is `verified` and whose
   **source** record is `verified` and whose source file exists: delete the
   source file (`_discard_payload_file`, which also prunes empty parents),
   rewrite the source record as `{**record, "status": "moved",
   "moved_at": now}`, save the source ledger after each file (atomic
   temp+rename, same as every other ledger write — a crash mid-loop leaves
   a consistent skeleton).
2. Skip anything else: destination not verified (the copy failed its
   hash), source not verified, source already `moved`, or a `.incomplete`.
3. Record one event in the **source** ledger:
   `moved_out` — `"moved 7 files (27.8 GB) to vault 'big' on jn-mbp"` —
   vault basename and hostname, never a path. And one in the
   **destination** ledger alongside the existing `assembled_partial`:
   `"moved in from skeleton on jn-mbp"`.
4. If every expected file in the source is now `moved`: `rmtree` the
   source bundle directory (it holds no payload byte by construction) and
   print `Nothing left to fetch at <source>; skeleton removed.` Otherwise
   print `Moved 7 files (27.8 GB) out of <source>; 14 files (26.6 GB)
   remain to fetch there.`

Safety properties:

- **Verify-then-delete, per file.** The destination hash is the gate. A
  rotted copy, an interrupted copy, an `ENOSPC` on the destination — the
  source keeps those bytes.
- **Source lock.** `assemble` already takes `transfer_lock` on the
  destination; `--move` also takes it on each source (a concurrent
  `archive` on the laptop must not be fetching into a directory that is
  being emptied). A source that is the destination (two paths to one
  directory) is refused by the existing lock identity check.
- **No verification is skipped by the second hand-over.** The destination
  reconciles with `rehash=True` at the start of every assemble, so the
  bytes moved in last week are re-hashed before this week's files are
  added and before any source byte is deleted.
- **Same disk.** On APFS the copy is a clone; deleting the source frees
  nothing until the destination diverges. Semantics are still correct;
  the user's expectation of freed space is wrong only when both vaults
  share a volume, which is not this workflow.

### `list` / `du` / catalog overlay

`bundle_records` (vault.py) for a ledger-only bundle:

- `percent = (verified + partial + moved) / total`
- `remaining_bytes = remaining_network` (unchanged name; now excludes moved)
- integrity string gains a clause only when `moved` is non-zero:

```
archiving: 52% (28.9/55.6 GB, 7/22 files verified, 27.8 GB moved out)
```

`du` is disk use as before; a skeleton is small. The catalog overlay's
`partial` row is unchanged in kind; its remaining GiB drops by the moved
amount, which is what "remaining to finish" already means in
`docs/CATALOGS.md`. `list --json` rows gain `moved_bytes` (0 for
non-skeletons) so scripts can tell a skeleton from a plain partial.

### Where this sits next to the website's "who has a copy"

The darsay.io proposal keeps possession out of `catalog.json` on purpose:
`overlay()` is the only possession the CLI believes; the site's `holders`
string ("Maya, USB in Berlin") is a human claim. A skeleton is the CLI-side
counterpart, one level down: not "someone says they have it" but "this
pin's ledger has verified hashes for bytes it handed to another vault."
It stays where transfer state lives — `transfer.json`, machine-local,
excluded from exports — and never leaks into the catalog. The `moved_out`
event's free-text detail is the closest thing to a `holders` string, and
it is deliberately unstructured.

---

## Interface Changes

### CLI

| Surface | Change |
|---|---|
| `darsay assemble BUNDLE… --move` | New flag. After merging and verifying, delete each source file the destination verified and mark it `moved`; remove a source that has nothing left to fetch. |
| `darsay archive` | Exit 10 with `end_reason: "moved"` when nothing is left to fetch here but the pin is not complete here. |
| `darsay list` | `… , N GB moved out)` clause on skeleton rows; `--json` gains `moved_bytes`. |
| `darsay estimate` | `moved` bucket in the classification bar. |
| `darsay complete` | No change (`assemble` already completes directories). |

No new subcommand. `COMMANDS` / `BUNDLE_COMMANDS` unchanged.

### `transfer.json`

Additive. Per-file record:

```jsonc
"model-00003-of-00012.safetensors": {
  "status": "moved",                 // was "verified"
  "size": 4966786096, "sha256": "…", "blake3": "…", "git_sha1": null,
  "verified_against_upstream": true, "verified_at": "2026-08-29T21:04:11+00:00",
  "source": "network", "attempts": 1,
  "moved_at": "2026-08-30T09:12:40+00:00"
}
```

Events: `moved_out` (source), `moved_in` (destination). Session end reason:
`moved`. `transfer_version` stays `1`.

### Manifest, MVB, catalog

No change. `moved` never reaches a manifest (registration requires
`verified`), so `SCHEMA_VERSION` and `MVB_FORMAT_VERSION` are untouched.
`catalog_schema_version` is untouched.

### Docs

- `docs/INCREMENTAL.md`: rule 2 gains the one sentence from Key Decision 6;
  §3 reconcile table gains the two rows above; §4 ledger shape shows a
  `moved` record; §5 gains a subsection "Halves: `assemble --move`" after
  "Cooperative shard keys and offline assembly"; §6 exit-code paragraph
  lists `moved`.
- `docs/CONCEPTS.md`: under **Pin**, one paragraph: *A partial whose
  verified bytes have been handed to another vault is a skeleton — the
  pin and the hashes stay, the payload does not, and the next `archive`
  fetches only what no vault has.*
- `examples/README.md`: new recipe **"Archive in halves across two disks"**
  between "Split a download across machines" and "Move a partial bundle".
- `docs/CATALOGS.md` overlay table: `partial` row note that remaining
  excludes moved bytes.
- `CHANGELOG.md` Unreleased → Added.

---

## Alternatives Considered

### 1. Shrink the skeleton's `expected` list to the unmoved files

No new state: `--move` removes moved files from the source's `expected`.
Rejected. `list` would report the pin as half its real size; `assemble`'s
`_same_transfer_set` (identical `expected`) would reject the second
hand-over; the subset machinery would be tempted in to record what was
dropped, muddling curation (`--include`) with logistics. The expected set
is the pin. Per-file state is where "where are the bytes" belongs.

### 2. A standalone verb — `darsay skeleton BUNDLE`, `darsay rm BUNDLE --keep-pin`, `darsay shed`

Mark every verified file `moved` and delete it, trusting the operator to
have copied. Covers `rsync`-over-SSH destinations. Rejected for v1: it is
the one operation in the tool that deletes bytes on the strength of a
claim, and the `cp -a`-into-a-vault case is already covered by
`assemble --move` (it verifies the copied bytes and copies nothing). If
the SSH case matters it can be added later on the same `release_moved`
function — see [Open Questions](#open-questions).

### 3. Teach `--shard` about the other vault

Make lane assignment subtract what a named other partial holds. Rejected:
the other partial is not mounted when the laptop is at the bandwidth, and
shards are an ordering hint, not a filter; turning them into a filter
changes their meaning for the three-people case.

### 4. Make the laptop's `archive` read a "have-list" file exported from the big vault

`darsay export-inventory` on the big vault → carry a JSON home →
`archive --skip-listed`. Rejected: it is a second, sidecar ledger that
must be kept in sync by hand, and the skeleton already *is* that file —
it is the ledger the laptop had all along, with the bytes taken out.

### 5. `assemble --move` deletes nothing; a later `rm --moved` does

Two-step for safety. Rejected: the verification that makes deletion safe
happens *inside* `assemble`; separating the delete from the verify
re-creates the trust problem of alternative 2.

---

## Security & Safety

- Deletion only ever follows a destination hash match against the pinned
  upstream digest — the same gate that admits a file to a manifest.
- No archival state depends on `moved`: nothing registered, exported, or
  imported ever reads it.
- The ledger keeps its no-absolute-paths rule: `moved_out` / `moved_in`
  carry hostname and vault basename as free text only.
- A skeleton copied to a third machine behaves as a partial there; its
  `moved` records are still honest (the bytes are in the vault that
  verified them) and still skip-on-fetch.

---

## Rollout

One PR, ~300 lines with tests, no version bump beyond the tool's own.

1. `transfer.py` — `moved` in `reconcile`, `transfer_plan` (`moved`
   bucket, `fetched`), `transfer_all` skip, `print_plan` line,
   `release_moved`, `assemble_partials(move=)`.
2. `transfer.py` / `archiver.py` — `CleanStop("moved", …)` at the end of
   `transfer_all` (inside the live panel); `archiver.archive`'s
   `RuntimeError` for "not complete" is then unreachable for skeletons and
   stays as the safety net it is.
3. `cli.py` — `--move` on `assemble`; hint text for `end_reason: "moved"`;
   `list` clause and `--json` field.
4. `vault.py` — `bundle_records` percent/remaining/integrity string.
5. `estimate.py` — bucket in the static bar.
6. `progress.py` — nothing. `_live_transfer` in `transfer.py` already
   renders an unlisted clean-stop reason as `paused: <reason>`; add
   `"moved": "paused: moved"` to `_STOP_VERDICTS` only for explicitness.
7. Docs and changelog as listed.

Tests (`tests/integration/`, fake `test:` provider, no network):

- half via `--max-bytes` → `assemble --move` → source records are `moved`,
  bytes gone, destination `verified`; a destination hash failure leaves
  that source file in place.
- `archive` on the skeleton fetches only `missing`; session
  `bytes_network` equals the missing bytes exactly; a moved file is never
  requested from the provider.
- second `assemble --move` completes the destination and removes the
  skeleton; `archive` on the destination registers with `bytes_network ==
  0`.
- a skeleton whose fetchable files are all done ends with `end_reason:
  "moved"` and exit 10, not `RuntimeError`.
- reconcile: a `moved` file that reappears is adopted; with the ledger
  deleted, the same directory reconciles to a plain partial that re-fetches
  (doctrine).
- `list` / `list --json` / catalog overlay: percent, remaining, moved
  clause.
- export determinism untouched (no export path reads the ledger).

---

## Open Questions

1. **Should a skeleton dissolve on its own (Key Decision 7), or stay until
   `darsay rm`?** Proposed: dissolve, because a fully-moved skeleton has
   no payload byte and nothing to fetch, and `list` would otherwise show
   `archiving: 100%` forever. The conservative alternative is to keep it
   and render `moved out: 100% — rm when the destination registers`.

2. **A manual escape hatch for unmountable destinations** (`rsync` to a
   server over SSH). If needed: `darsay rm BUNDLE --moved` — delete verified
   payload bytes, keep the pin, mark `moved`, confirm with the same
   `Type yes` prompt `rm` uses today. Same `release_moved` function with a
   `None` destination. Not in v1.

3. **Keep small files at the source?** `--move` could leave files under
   `SMALL_FILE_LIMIT` (config, tokenizer, card) so the skeleton stays
   inspectable. Proposed: no — a skeleton is not for loading, and fewer
   rules is better. Revisit if `info` on partials becomes a thing.

4. **Name of the state.** `moved` pairs with `--move` and with `mv`.
   Alternatives weighed: `released` (overloaded by software releases),
   `offloaded` (accurate, clunky), `elsewhere` (honest, odd as an enum).

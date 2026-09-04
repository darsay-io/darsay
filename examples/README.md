<p align="center">
  <a href="../docs/GETTING-STARTED.md">Start here</a> ·
  <a href="../docs/CONCEPTS.md">Concepts</a> ·
  <a href="README.md">Examples</a> ·
  <a href="../docs/README.md">All docs</a> ·
  <a href="../README.md">README</a>
</p>

# Examples

Copy-paste recipes. Each one is a complete thought: the command, what
you should see, and the idea it is teaching.

New to the tool? [Start here](../docs/GETTING-STARTED.md) is a walkthrough.
This page is the cookbook you return to.

| I want to… | Jump |
|---|---|
| Keep a model and talk to it | [First bundle](#first-bundle) |
| Know the size before I commit | [Estimate first](#estimate-first) |
| Stop at 10 GB and continue tomorrow | [Pause and resume](#pause-and-resume-a-large-archive) |
| Keep an unattended box from filling its disk | [Leave room on the disk](#leave-room-on-the-disk) |
| Price one quant in a huge GGUF pack | [Price one quant](#price-one-quant-from-a-pack-repo) |
| Archive a dataset | [Dataset](#archive-a-dataset) |
| Load the payload myself | [Use the files directly](#use-the-files-directly) |
| Put a bundle on a USB drive | [Export](#export-to-a-usb-drive) |
| Split a download with a friend | [Cooperative shards](#split-a-download-across-machines) |
| Fetch a model in halves across two disks | [Archive in halves](#archive-in-halves-across-two-disks) |
| Move a finished bundle to another disk | [Move a bundle](#move-a-bundle-to-another-vault) |
| Keep a verified backup copy | [Copy a bundle](#copy-a-bundle-to-a-backup-disk) |
| Move a half-finished archive to another disk | [Relocate a partial](#relocate-a-partial-bundle) |
| rsync bytes, then let darsay verify | [rsync then darsay](#rsync-then-darsay) |
| Write curator notes | [Curation](#write-curator-notes) |
| Curate a want-list and share it | [Share a catalog](#share-a-catalog) |
| Re-price a darsay.io board, claim its next row | [Keep a board honest](#keep-a-darsayio-board-honest) |
| Prove the bytes have not drifted | [Verify](#verify-on-a-schedule) |
| See what a command would do first | [Dry run](#dry-run-anything-that-writes) |

---

## First bundle

The whole product.

```bash
pipx install darsay

darsay archive sshleifer/tiny-gpt2
darsay list
darsay run sshleifer--tiny-gpt2 "Hello"
```

`list` prints STATUS, SOURCE, and HAVE (the bundle id, `name@<rev>`;
`<rev>` is the first 12 of the pinned commit). Paths live in `list --json`
and `info`. `run` / `info` / `verify` accept the path, the id, or a unique
prefix. `run` builds an isolated env the first time, then generates
**offline**. The payload under `model/` is not touched.

A model you would actually keep is the same shape:

```bash
darsay archive Qwen/Qwen3-0.6B
darsay run     qwen--qwen3-0.6b "Say hello"
```

---

## Estimate first

A 27B model is a 50+ GB commitment. Price it from Hub metadata — no
download, no files written.

```bash
darsay estimate Qwen/Qwen3.8-27B
```

Typical output:

```
Qwen/Qwen3.8-27B @ main -> 1d4bf0f2ff60
  parameters:   27.78B BF16
  payload:      32 files, 51.8 GiB
  download:     ░░░░░░░░░░░░░░░░░░░░░░░░    0.0%   0 B / 51.8 GiB
                nothing banked yet — full 51.8 GiB in 32 files to fetch
  disk:         needs ~55.5 GiB, free 1022.6 GiB — OK

To archive: darsay archive Qwen/Qwen3.8-27B
```

Re-estimating a source you have already partially archived prices the
resume instead: the download bar shows how much is banked (verified,
partial, and unverified-but-adoptable bytes) and the disk verdict counts
only what still has to cross the network.

Useful flags:

```bash
darsay estimate Qwen/Qwen3.8-27B --variants          # quantized ecosystem
darsay estimate Qwen/Qwen3.8-27B --json              # machine-readable
darsay estimate unsloth/Qwen3.8-27B-GGUF --include '*Q4_K_M*'
```

`--variants` records query caps in the output so a truncated listing is
never mistaken for a complete one. Exit code is non-zero when disk is
insufficient — safe to put in front of `archive` in a script.

---

## Pause and resume a large archive

There is no resume subcommand. `archive` is idempotent: every run
converges on the same pinned bundle.

```bash
darsay archive Qwen/Qwen3.8-27B --max-gb 10     # tonight: first 10 GB
# exit code 10 — budget exhausted, bytes kept

darsay archive Qwen/Qwen3.8-27B --max-gb 10     # tomorrow: next 10 GB
darsay archive Qwen/Qwen3.8-27B --dry-run       # what's left?
darsay archive Qwen/Qwen3.8-27B                 # finish, verify, register
```

Ctrl-C is the same idea: rerun the command. Completed files are trusted;
partial files resume with HTTP Range. While it runs, a TTY shows percent
of the whole payload, bytes in / total, rate, and time remaining — not
a per-file bar that resets on every shard.

Also valid: `--max-bytes 20G`, `--max-minutes 45`.

The pin is frozen on the first run. Later runs do not chase a new
`main`. Design: [Incremental transfer](../docs/INCREMENTAL.md).

---

## Leave room on the disk

An unattended archive should never wedge a partition full. `archive`
pauses cleanly — exit 10, bytes kept — when the destination's free
space drops below a floor. The default floor is 2 GiB.

```bash
darsay archive Qwen/Qwen3.8-27B --min-free 10G   # this run: pause below 10 GiB free
# exit code 10 — disk: … 9.9 GiB free < 10.0 GiB floor; free space, rerun

darsay config                                    # which floor applies here, and why
```

Set it once per vault so an archive drive carries its own limit, or once
per machine for every vault:

```toml
# <vault>/config.toml   (or ~/.config/darsay/config.toml)
[transfer]
min_free = "10G"
```

`--min-free 0` disables the floor for one run; `$DARSAY_MIN_FREE` overrides
per shell. The plan line and `estimate` price the floor in —
`needs 40.0 GiB, free 45.0 GiB (10.0 GiB floor) — INSUFFICIENT` — so a
run that cannot finish says so before the first byte, and says where it
will stop:

```text
WARNING: disk preflight is insufficient; the transfer will pause at the free-space floor
  after about 381.4 GiB more (67 of 140 remaining files), roughly 9h at 12.3 MiB/s.
  Free space (or move the vault to a larger disk), then re-run to continue.
Continue anyway? [Y/n]
```

Enter banks what fits now (the pause comes before the first file that
would not fit); `n` exits 10 with nothing moved. `darsay archive … --yes`
skips the question, and a run without a terminal never sees it. Design:
[Incremental transfer](../docs/INCREMENTAL.md#6-session-budgets).

---

## Cap the bandwidth

An archive that runs all day should not own the connection. The cap is
one leaky bucket shared by every worker, in bytes per second.

```bash
darsay archive Qwen/Qwen3.8-27B --max-rate 5M    # this run: 5 MiB/s
#   rate:     capped at 5.0 MiB/s — about 3h for the remaining 52.1 GiB
```

Make it the default for a machine or an archive drive:

```toml
# <vault>/config.toml   (or ~/.config/darsay/config.toml)
[transfer]
max_rate = "5M"        # "5M" and "5M/s" both mean 5 MiB/s
```

`--max-rate 0` lifts a configured cap for one run; `$DARSAY_MAX_RATE`
overrides per shell. The panel shows the cap in its tail
(`… 12 min elapsed · cap 5.0 MiB/s`) and the rate settles just under it.

---

## Walk away from the Wi-Fi

Nothing to type. When the network goes, `archive` keeps what arrived,
the panel reads `offline` with the next attempt counting down, and the
transfer resumes from the banked bytes when the network is back:

```text
     0 B/s  ██▇▅▂▁··   offline           retry in 8s · 2 min 10s offline
  12/153 files · model-00141-of-00141.safetensors   27.3%   1.2 GiB / 4.4 GiB
Reconnected after 4 min 12s (7 attempts).
```

It waits up to an hour by default. Longer, and it pauses cleanly — exit
10, the reason on the terminal — so a loop can rerun it later:

```bash
darsay archive Qwen/Qwen3.8-27B --max-offline 4h   # overnight ISP outage
darsay archive Qwen/Qwen3.8-27B --max-offline 0    # pause at the first failure instead
```

```toml
[transfer]
max_offline = "4h"     # seconds, or s/m/h/d
```

Design: [Incremental transfer](../docs/INCREMENTAL.md#network-loss).

---

## Archive conservatively (the default)

Some repositories ship several variants or duplicate weight layouts.
`archive` classifies a fresh model pin and retains every weight and support
file except weight sets whose every file has a hash-identical twin in a
retained same-bundle set. Every omission is named in the preflight and recorded
in the manifest with the full inventory. A GGUF is not omitted merely
because safetensors sit beside it; no preferred precision is chosen.

```bash
darsay estimate owner/model-with-quants        # prices what archive will fetch
darsay classify owner/model-with-quants        # the per-set evidence table
darsay archive  owner/model-with-quants        # the classified archive selection
darsay archive  owner/model-with-quants --full # every published byte instead
```

A skipped duplicate's exact content remains in the bundle, but its
original path does not. Use `--full` to retain all published layouts.
Unknown weights and remote-base duplicates are retained. A negative/print
label is separate from the action; neither label proves a reconstruction
recipe exists. Policy and rules: [Quantization](../docs/QUANTIZATION.md).

---

## Read a size: precision and bytes per parameter

A repository can hold many complete variants of the same model.
`estimate` lists GGUF variants with all their shards; its payload line
names whether it prices the repository, a selection, or a classified
archive:

```bash
darsay estimate unsloth/GLM-5.3-Flash-GGUF --full
# Repository: 2.315 TiB across twelve model variants and supporting files.
# BF16: 597.6 GiB across fourteen shards.
# UD-Q4_K_XL: 186.0 GiB across six shards.

darsay estimate zai-org/GLM-5.3-Flash
# Publisher's FP8 release: 305.8 GiB including support.
# This is a different repository, not a subset of the Unsloth pack.

darsay estimate unsloth/GLM-5.3-Flash-GGUF --include '/UD-Q4_K_XL/GLM-5.3-Flash-UD-Q4_K_XL-*-of-00006.gguf'
# Selection: six model shards plus supporting metadata.
# Total model parameters: 320,759,404,382, from upstream GGUF metadata.
# Selected model weights: about 0.623 bytes per parameter.
```

These figures are pinned in the [GLM case study](../docs/QUANTIZATION.md#a-gguf-pack-in-practice).
Whole GGUF packs have no single-model bytes-per-parameter or RAM estimate.
Classification separately explains retention evidence; selecting a
precision does not establish provenance or a recovery guarantee. Choose
the publisher release, whole pack, or a variant deliberately. Selecting
one variant does not preserve every release of the work.

## Hold a place for a closed work

A model served by API only, or announced and not yet published, still
belongs in the family tree. Its home page is a valid catalog address:

```bash
darsay catalog add summer https://www.qwencloud.com/models/qwen3.8-max-0902 --desire 7
darsay list summer --sort family        # sits in Qwen 3.8 beside 2.4T-A95B and 27B
```

The row is **closed**: no price, nothing to fetch, `--next` never picks
it. When the weights ship, add the source ref and drop the home:

```bash
darsay catalog add  summer Qwen/Qwen3.8-Max --desire 7
darsay catalog drop summer https://www.qwencloud.com/models/qwen3.8-max-0902
```

## Read a catalog as a family tree

```bash
darsay list summer --sort family
```

Rows come out as the tree reads: family, then generation oldest first,
then size — with FAMILY (`Qwen 3.8`) and PRECISION (`BF16`, `MXFP4`)
columns on every row. The catalog's generated `README.md` draws the same
tree under **Families**. Family, generation, and member are read from
each work's name by a documented grammar ([Concepts → lineage](../docs/CONCEPTS.md#lineage));
parent edges (finetune, quantized, trained-on) are recorded as upstream
declares them.

## Price one quant from a pack repo

For an interactive walkthrough, run the archive command without includes:

```bash
darsay archive unsloth/GLM-5.3-Flash-GGUF
```

A fresh multi-variant pin opens the collection room. `1` starts with the
smallest complete, known-size 4-bit variant; `2` adds an 8-bit counterpart;
`3` selects every published file. Space edits the choice, `?` explains a
variant, and Enter reviews before confirming. Projectors remain explicit.
These are storage-oriented starting points, not quality or hardware claims.
See [Choose your collection](../docs/COLLECTIONS.md).

Some GGUF repos are hundreds of gigabytes of named quants. `--include`
prices a glob against Hub metadata, then archives only those files
(plus config/tokenizer/license sidecars). The manifest records the
omitted upstream files.

Select every shard of the desired variant. Variant inventories provide
exact globs for this; a small first shard does not represent the model's
full size. Include any required projector separately.

```bash
darsay estimate unsloth/Qwen3.8-27B-GGUF --include '*Q4_K_M*'
darsay archive  unsloth/Qwen3.8-27B-GGUF --include '*Q4_K_M*'
```

`--include` is a glob, repeatable. A published quant that is its own
repo (official FP8, a community GGUF people actually ran) is still an
ordinary satellite bundle with no glob:

```bash
darsay archive Qwen/Qwen3.8-27B-FP8
```

Policy: [Quantization](../docs/QUANTIZATION.md).

---

## Archive a dataset

Datasets are the second artifact type. One sentence covers the
difference: addressed as `datasets/owner/name`, payload under `data/`.
Same verbs.

```bash
darsay estimate datasets/cornell-movie-review-data/rotten_tomatoes
darsay archive  datasets/cornell-movie-review-data/rotten_tomatoes
darsay info     datasets--cornell-movie-review-data--rotten_tomatoes
```

Paste-from-browser URLs work too:

```bash
darsay archive https://huggingface.co/datasets/cornell-movie-review-data/rotten_tomatoes
```

Unprefixed `owner/name` that exists only as a dataset is resolved at pin
time to `huggingface:datasets/owner/name`.

`hydrate` / `run` do not apply — a dataset has no engine. Open `data/`
with whatever already reads the format. Design: [Datasets](../docs/DATASETS.md).

---

## Use the files directly

The payload keeps selected Hub files in their published formats. Point a
compatible loader at `model/`, with all the files and paths that loader
requires. Use `--full` when you need every upstream layout; a classified
selection can omit duplicate paths, and an explicit subset can omit
companions. Hash verification and a successful offline run test different
properties.

**Model**

```python
from pathlib import Path
from transformers import AutoModelForCausalLM, AutoTokenizer

path = Path.home() / "darsay/qwen--qwen3-0.6b/<rev>/model"
tok = AutoTokenizer.from_pretrained(path)
model = AutoModelForCausalLM.from_pretrained(path)
```

**Dataset**

```python
from pathlib import Path
import pyarrow.parquet as pq

path = Path.home() / "darsay/datasets--cornell-movie-review-data--rotten_tomatoes/<rev>/data"
table = pq.read_table(path / "train.parquet")
```

No unpacking, no conversion, no darsay import.

---

## Export to a USB drive

One bundle → one deterministic tar. Same bundle state always produces
the same bytes (sorted entries, marker first, no wall clock).

```bash
darsay export qwen--qwen3-0.6b -o /Volumes/USB/backups
# writes /Volumes/USB/backups/qwen--qwen3-0.6b@<rev>.mvb.tar

darsay --vault /other/vault import /Volumes/USB/backups/qwen--qwen3-0.6b@<rev>.mvb.tar
```

`import` streams the marker, unpacks to staging, **re-hashes the
payload**, and only then registers. Failures write nothing.

The file is plain uncompressed tar. It includes `darsay-verify.py` (stdlib
only) so a reader without darsay can still check the payload:

```bash
python3 darsay-verify.py qwen--qwen3-0.6b@<rev>.mvb.tar
tar -xf qwen--qwen3-0.6b@<rev>.mvb.tar
python3 qwen--qwen3-0.6b@<rev>/darsay-verify.py qwen--qwen3-0.6b@<rev>
```

Manual recovery without even that script: [MVB format](../docs/MVB-FORMAT.md).

---

## Split a download across machines

`--shard N/T` is a priority, not a partition. Each participant prefers
a different byte-balanced set of whole files, but any one of them can
finish the bundle alone. (One person, two disks that never meet? Same
`assemble`, with [`--handoff`](#archive-in-halves-across-two-disks).)

```bash
# alice, on her machine
darsay --vault /usb/alice archive Qwen/Qwen3.8-27B --shard 1/2 --max-gb 20

# bob, on his
darsay --vault /usb/bob   archive Qwen/Qwen3.8-27B --shard 2/2 --max-gb 20

# later, offline, no Hub required
darsay --vault ./combined assemble \
    /usb/alice/qwen--qwen3.8-27b/<rev> \
    /usb/bob/qwen--qwen3.8-27b/<rev>

darsay --vault ./combined archive Qwen/Qwen3.8-27B   # register if complete
```

`assemble` merges matching partials by content. It does not talk to the
network.

---

## Archive in halves across two disks

Same `assemble`, one disk instead of two friends. The laptop has the
bandwidth but not the room; the big drive has the room but is at home. They
are never plugged in together. Fetch a half where the network is, hand it
to the big drive with `--handoff`, and the laptop keeps a **skeleton** — the
pin and the hashes, with the handed-off bytes deleted — so it can fetch the
*other* half without re-downloading the first.

```bash
# laptop, at the café — the first 30 GB
darsay archive Qwen/Qwen3.8-27B --max-gb 30

# laptop plugged into the big drive — hand the half over, keep the skeleton
darsay --vault /Volumes/big assemble ~/darsay/qwen--qwen3.8-27b/<rev> --handoff

# laptop, back at the café — the other 30 GB (the handed-off half is never re-fetched)
darsay archive Qwen/Qwen3.8-27B --max-gb 30

# big drive — the second hand-over completes it and dissolves the skeleton
darsay --vault /Volumes/big assemble ~/darsay/qwen--qwen3.8-27b/<rev> --handoff
darsay --vault /Volumes/big archive Qwen/Qwen3.8-27B     # registers, zero network
```

Already rsync'd the partial onto the big drive? Same command — it copies
nothing already there, trusts dest files the ledger already marks
verified (size match), then skeletonizes the laptop copy. It does not
re-hash dest over SMB:

```bash
rsync -aP --exclude=hydration.json --exclude=transfer.lock --exclude=.DS_Store \
  ~/darsay/qwen--qwen3.8-27b/<rev>/ /Volumes/big/qwen--qwen3.8-27b/<rev>/
darsay --vault /Volumes/big assemble ~/darsay/qwen--qwen3.8-27b/<rev> --handoff
```

`--handoff` only ever deletes a file dest has as verified, so an incomplete
copy never costs the laptop its only copy. Hash dest on the dest host
(`assemble --rehash`) if you do not trust the rsync. On the laptop,
`darsay list` shows the skeleton — `archiving: 52% (…, 27.8 GB handed off)` —
and an `archive` run there stops cleanly with `assemble to register` once
everything left is either verified locally or already handed off. A skeleton
with every file handed off is removed, leaving exactly what a plain `mv`
would have. Full design: [Incremental transfer](../docs/INCREMENTAL.md#across-disks-assemble---handoff-and-skeletons).

---

## Move a bundle to another vault

A registered bundle changes disks with `mv`. The destination is a vault
root that already exists (an unmounted disk must not become a folder on
this one). Same filesystem: a rename. Different filesystem: copy, re-hash
the copy at the destination, then remove the source.

```bash
darsay mv qwen--qwen3-0.6b /Volumes/big
```

```
Moving qwen--qwen3-0.6b@c1899de289a0
  from:     ~/darsay/qwen--qwen3-0.6b/c1899de289a0
  to:       /Volumes/big/qwen--qwen3-0.6b/c1899de289a0  (new)
  how:      copy 11 files (1.5 GiB), re-hash the 7 payload files at the destination, then remove the source
  disk:     needs 1.5 GiB, free 400.2 GiB (2.0 GiB floor) at /Volumes/big — OK
  leaves:   hydration.json — vault-local; `darsay hydrate` again at the destination
Transferring 11 files (1.5 GiB) ...
Verifying the copy at the destination ...
Re-hashing 7 files in /Volumes/big/qwen--qwen3-0.6b/.mv-c1899de289a0/c1899de289a0/model ...
Verification: PASS (7 files; 0 modified, 0 missing, 0 extra)
Moved qwen--qwen3-0.6b@c1899de289a0 → /Volumes/big/qwen--qwen3-0.6b/c1899de289a0  (7 payload files verified at the destination before the source was removed)
```

rsync is the same move without the verb, and always will be:

```bash
rsync -aP --exclude=hydration.json --exclude=transfer.lock --exclude=.DS_Store \
  ~/darsay/qwen--qwen3-0.6b/<rev>/ /Volumes/big/qwen--qwen3-0.6b/<rev>/
darsay --vault /Volumes/big verify qwen--qwen3-0.6b     # on the host that owns the disk
darsay rm qwen--qwen3-0.6b --yes
```

Or rsync and then `mv`. A destination that already holds the bundle is
landed on, not refused: every payload file already there at its recorded
size is hashed in place and kept when it matches, only what is missing or
differs is copied, and the bundle's own record replaces the copy's — even
when the copy's record is older, from an rsync made before a `darsay
migrate`. The copy is read once and nothing that matched crosses again.

```
$ darsay mv qwen--qwen3-0.6b /Volumes/big
Moving qwen--qwen3-0.6b@c1899de289a0
  from:     ~/darsay/qwen--qwen3-0.6b/c1899de289a0
  to:       /Volumes/big/qwen--qwen3-0.6b/c1899de289a0  (exists)
  how:      6 of 7 payload files are already there at the recorded size — hash them in place, copy the other 1 (1.4 GiB), then remove the source
  disk:     needs 1.4 GiB, free 400.2 GiB (2.0 GiB floor) at /Volumes/big/qwen--qwen3-0.6b/c1899de289a0 — OK
Landing on /Volumes/big/qwen--qwen3-0.6b/c1899de289a0: hashing 6 payload files in place, copying 1 (1.4 GiB) ...
  model/model.safetensors  (1.4 GiB)  copying
Verification: PASS (7 files; 0 modified, 0 missing, 0 extra)
Moved qwen--qwen3-0.6b@c1899de289a0 → /Volumes/big/qwen--qwen3-0.6b/c1899de289a0  (7 payload files verified at the destination — 6 already there, 1 copied — before the source was removed)
```

`archive.moves` records `method: adopt` with those counts. A file that
was there at the right size but hashed wrong is copied over and listed
under `replaced`. The one landing `mv` refuses is onto payload files this
record does not list — another pin of the same revision — and it names
them and `darsay rm` for either side. When the destination's `curation.md`
differs from the bundle's, the plan says so before anything moves; the
bundle's lands.

Over SMB or NFS, name the machine that owns the disk once and `mv`
hashes there instead of reading the mount back:

```bash
darsay --vault /Volumes/big config host.ssh=root@nas host.path=/volume1/big
darsay mv qwen--qwen3-0.6b /Volumes/big        # plan says: hash: on root@nas
```

That machine needs `sh` and `sha256sum` (or `shasum`, `sha256`,
`openssl`), not darsay or Python — one ssh call carries a short POSIX
script, and the hashes come back. Without it, `mv` verifies by reading
the destination back, which over the wire is a trip of the whole
payload, and it says so before it starts — with the lines to paste, paths
filled in: the rsync above, `darsay verify` on the host that owns the
disk, `darsay rm` here, and the `config` line above. With the bytes
already there, the rsync carries only the record. rsync `--delete` is
never part of it ([FAQ](../docs/FAQ.md#should-the-rsync-use---delete)).
`mv` refuses a partial — that is
[`assemble --handoff`](#archive-in-halves-across-two-disks) or the recipe
below. Which verb when: [FAQ](../docs/FAQ.md#moving-bundles).

---

## Copy a bundle to a backup disk

`cp` is `mv` without the removal. The copy is verified where it landed,
the source is kept, and both manifests record the other as a replica.

```bash
darsay cp qwen--qwen3-0.6b /Volumes/backup
```

```
Copying qwen--qwen3-0.6b@c1899de289a0
  from:     ~/darsay/qwen--qwen3-0.6b/c1899de289a0
  to:       /Volumes/backup/qwen--qwen3-0.6b/c1899de289a0  (new)
  how:      copy 11 files (1.5 GiB), re-hash the 7 payload files at the destination, and keep the source; both manifests record the replica
  disk:     needs 1.5 GiB, free 1.8 TiB (2.0 GiB floor) at /Volumes/backup — OK
Transferring 11 files (1.5 GiB) ...
Verifying the copy at the destination ...
Re-hashing 7 files in /Volumes/backup/qwen--qwen3-0.6b/.cp-c1899de289a0/c1899de289a0/model ...
Verification: PASS (7 files; 0 modified, 0 missing, 0 extra)
Copied qwen--qwen3-0.6b@c1899de289a0 → /Volumes/backup/qwen--qwen3-0.6b/c1899de289a0  (7 payload files verified at the destination; source kept, replica recorded in both manifests)
```

Afterwards `darsay info` on either side shows `Replicas: 1`, and the
manifest's `archive.replicas` names the other location. Refresh the backup
later by running the same `cp` again: the backup is hashed in place, only
what is missing or differs is copied, and the replica entry — keyed by
location — updates its timestamp instead of the disk being listed twice.
On the same APFS volume
the files arrive as copy-on-write clones and cost no space until they
diverge. rsync into the same layout, then `darsay verify` there, is the
same copy without the bookkeeping — and always will be.

---

## Relocate a partial bundle

Partial bytes are portable. Copy the entire
`<repo-slug>/<revision12>/` directory — including any payload `.cache`
— under a different vault and rerun the same `archive` command.

```bash
cp -a ~/darsay/qwen--qwen3.8-27b /mnt/other/vault/
darsay --vault /mnt/other/vault archive Qwen/Qwen3.8-27B
```

The pin is unchanged. Completed files are adopted. The longest Range
partial continues. The ledger holds no source-machine absolute paths, so
this works across laptops.

---

## rsync then darsay

rsync is a first-class copy. The next darsay command trusts dest files
the ledger already marks verified (size match), fetches only the
remainder, and rewrites metadata. It does not re-download the majority,
and it does not pull dest back over SMB to re-hash a copy. Put the
bundle at `<vault>/<slug>/<rev>/`, not at the vault root.

```bash
# resume a partial on the other disk
rsync -aP --exclude=hydration.json --exclude=transfer.lock --exclude=.DS_Store \
  ~/darsay/qwen--qwen3.8-27b/<rev>/ /Volumes/big/qwen--qwen3.8-27b/<rev>/
darsay --vault /Volumes/big archive Qwen/Qwen3.8-27B

# or free the laptop after the copy (trust dest ledger + size, delete source payload)
darsay --vault /Volumes/big assemble ~/darsay/qwen--qwen3.8-27b/<rev> --handoff
```

Hash dest where it is a local disk — `assemble --rehash` or `darsay
verify` on the NAS, not from a laptop over SMB (that reads the whole
payload back over the wire). `assemble` warns before it hashes a dest
that sits on a network mount — unless the vault's `config.toml` names
the NAS in `[host]`, in which case the hashing runs there over ssh and
the plan says `hashed on root@nas`
([FAQ](../docs/FAQ.md#how-do-i-stop-mv-and-verify-reading-a-nas-back-over-the-wire)).
A registered payload is frozen: rsync it, verify dest on the dest host,
then `darsay rm` the source — or [`darsay mv`](#move-a-bundle-to-another-vault),
which is those three steps with the refusals built in, and after an rsync
lands on the copy instead of making another. `--handoff` is the verb for
partials.

---

## Verify a bundle with nothing but coreutils

Every bundle carries `SHA256SUMS`, the payload's hash list in the format
`sha256sum` reads. No darsay, no Python:

```bash
cd ~/darsay/qwen--qwen3-0.6b/c1899de289a0
sha256sum -c SHA256SUMS          # shasum -a 256 -c SHA256SUMS on a Mac
sha256sum SHA256SUMS             # the bundle hash: inventory.bundle_hash.value in manifest.json
```

```
model/LICENSE: OK
model/config.json: OK
model/model.safetensors: OK
…
4b0c2f…  SHA256SUMS
```

The first command checks the bytes; the second binds the list to the
record, because darsay's bundle hash is the SHA-256 of exactly that
text. Why it works: [FAQ](../docs/FAQ.md#can-i-verify-a-bundle-without-darsay-or-without-python).

---

## Take in a bundle from an older darsay

A submission arrives — rsync, a USB stick, `darsay cp` onto a shared
disk — and every verb but `list` refuses it:

```
error: manifest schema 1.8.0 predates this darsay (reads 2.x)
  hint: darsay migrate zai-org--glm-4.5v/ed47433b3711   re-reads the record under the 2.x model; payload untouched (-n to preview)
```

Read what will change, do it, then hash the payload where it landed:

```bash
darsay migrate zai-org--glm-4.5v/ed47433b3711 -n
darsay migrate zai-org--glm-4.5v/ed47433b3711
darsay verify  zai-org--glm-4.5v/ed47433b3711
```

Each line of the plan names a section of the record, what it now says,
and where that came from:

```
Would migrate zai-org--glm-4.5v@ed47433b3711  (schema 1.8.0 → 2.1.0)
  identity:   GLM 4.5 · V — re-read from the name; drops version and family 'glm4v' (read from config.json; still model_metadata.model_type)
  precision:  BF16 · 2.00 bytes/param — re-derived from config.json and 47 safetensors headers (was 'bfloat16', a dtype)
  lineage:    parents: finetune huggingface:zai-org/GLM-4.5-Air (card); descendants as of 2026-08-30 kept (12 quantized, 3 finetunes) — translated from relationships and the recorded upstream tags
  carried:    source, licensing, inventory (47 files, 22.1 GiB), runtime, validation, archive, security, curation — as recorded
  payload:    47 files present at the recorded sizes; bytes untouched, not re-hashed
```

Nothing under `model/` is hashed or written. For a vault that takes many
submissions:

```bash
darsay list                # rows marked `migrate`, and a count in the header
darsay migrate --all -n
darsay migrate --all       # the last lines are the verify commands, one per bundle
```

An older `.mvb.tar` needs no extra step: `darsay import` verifies its
payload against the embedded record and migrates the record as it lands.
More in [FAQ → Older records](../docs/FAQ.md#older-records).

---

## Write curator notes

`curation.md` is the only hand-edited file. The generated `README.md`
is a view — never the other way around.

```bash
# after archive:
$EDITOR ~/darsay/qwen--qwen3-0.6b/<rev>/curation.md

darsay regen qwen--qwen3-0.6b
# rebuilds README.md from manifest + curation.md
```

`regen` will not create a new `curation.md` over an existing one.
Historical significance, capabilities, limitations — that is curator
territory; the tool will not invent it.

---

## Verify on a schedule

```bash
darsay verify qwen--qwen3-0.6b
```

Re-hashes every payload file, diffs against the manifest. Modified,
missing, or extra files flip integrity to `compromised` and the command
exits non-zero.

A 2040 reader without darsay uses the stdlib script shipped in every
export: `python3 darsay-verify.py <bundle-or-tar>`. That check is
read-only — it does not write `verification.json`.

```txt
# nightly, mail on failure
0 3 * * * darsay --vault /srv/vault verify /srv/vault/qwen--qwen3-0.6b/<rev>
```

`darsay list` is the inventory; `darsay info <bundle>` is the index
card.

---

## Dry run anything that writes

Every command that changes the vault takes `-n` / `--dry-run`. It runs the
same checks, prints the same report in the conditional, writes nothing, and
ends with the real command to paste.

```bash
darsay rm qwen--qwen3-0.6b -n
darsay import /Volumes/USB/backups/qwen--qwen3-0.6b@<rev>.mvb.tar -n
darsay --vault /Volumes/big assemble ~/darsay/qwen--qwen3.8-27b/<rev> --handoff -n
darsay mv qwen--qwen3-0.6b /Volumes/big -n         # rename, or copy + verify? and where it lands
darsay cp qwen--qwen3-0.6b /Volumes/backup -n      # what the copy costs and where it lands
darsay run qwen--qwen3-0.6b -n                  # will it install torch first?
darsay envs --prune -n
```

What `rm -n` prints:

```
Would remove:
     1.5 GiB  ~/darsay/qwen--qwen3-0.6b/c1899de289a0
Dry run: nothing removed. To remove:
  darsay rm qwen--qwen3-0.6b
```

And `assemble --handoff -n`, before a laptop hands its half to the big drive:

```
Would assemble into /Volumes/big/qwen--qwen3.8-27b/<rev>  (existing partial)
  from:     ~/darsay/qwen--qwen3.8-27b/<rev>  — copy 4 files, 12.1 GiB  (3 already at dest)
  verify:   4 files against the pin (4 copied)
  disk:     needs 12.1 GiB, free 400.2 GiB (2.0 GiB floor) at /Volumes/big — OK
  after:    7/9 files verified; 2 files (4.4 GiB) still to fetch — continue with `darsay archive`
  --handoff:   ~/darsay/qwen--qwen3.8-27b/<rev>  — release 7 files (26.2 GiB) once verified at dest → skeleton (pin + hashes stay)
Dry run: nothing copied, nothing released. To assemble:
  darsay --vault /Volumes/big assemble ~/darsay/qwen--qwen3.8-27b/<rev> --handoff
```

A refusal is the same refusal: `import -n` of a bundle the vault already
holds exits with `already exists (use --force to replace)`, exactly as the
real command would. `archive --dry-run` is the one preview that records
something — a new source is pinned in `transfer.json`, no payload bytes —
so the plan it shows is the plan the real run continues; `estimate` is the
preflight that writes nothing at all.

---

## Keep a darsay.io board honest

A board is a catalog with a URL, and the URL works wherever a catalog
name does. One command fetches it, re-prices every row with
classification, and pushes the result back — size basis, verdict counts,
GGUF variants, hints, precision, and parameter provenance included. The
board's own quick estimates describe repository or selection inventories;
the CLI adds the archive retention decision:

```bash
darsay estimate https://darsay.io/b/<board-id>
```

To fetch the board's next unfinished row — claiming it first, so a
second machine on the same board picks a different row, and reporting
progress the board renders as a gauge:

```bash
darsay archive --next https://darsay.io/b/<board-id>
```

Unfinished is the board's word for it: a row already checked off as
have — by a client reporting done, or by someone on the board page —
is never picked, even when your own vault does not hold it yet.

To pick the row yourself and still get the claim and the gauge, name
the source and bring the board along with `--board` (this is also the
way to deliberately re-fetch a row the board already has):

```bash
darsay archive MiniMaxAI/MiniMax-H3 --board https://darsay.io/b/<board-id>
```

Claims sign as a stable pseudonym drawn from a hash of the machine
(something like `amber-heron-3f`) — never the raw hostname, which would
tell everyone with the URL exactly which machine holds what. To sign as
a name the group actually knows, set one once:

```toml
# <vault>/config.toml   (or ~/.config/darsay/config.toml)
[board]
client = "jeremy-mbp"
```

The board URL is the capability: anyone who has it can edit the board.
Treat it like a secret.

---

## Share a catalog

A catalog is a list of sources you want, before the bytes exist. The vault
is the same list, realized. Share the file; friends overlay it against
*their* disk.

```bash
darsay catalog new summer --title "Summer 2026" --curator Alex
darsay catalog add summer huggingface:Qwen/Qwen3-0.6B --desire 9
darsay catalog add summer huggingface:Qwen/Qwen3.8-27B --desire 8 --note "the one to finish"
darsay estimate summer                 # cache sizes (optional; add is offline)
darsay list summer                     # want / have / partial, next fetch on top
darsay archive --next summer --max-gb 10
```

Copy `~/darsay/catalogs/summer/` to a USB stick or a git repo. A friend:

```bash
darsay list ./summer                   # every row want, against their vault
darsay list ./summer --want --sort desire
darsay archive --next ./summer --max-gb 10
darsay catalog new reading --curator Sam
darsay catalog adopt reading ./summer  # copy intent; their overlay, their bytes
```

`archive` does not rewrite the catalog. Status is a view. Path-addressed
files are read-only unless `--write`. Spec: [Catalogs](../docs/CATALOGS.md).

---

## Source refs, three ways to write the same thing

```bash
darsay archive huggingface:Qwen/Qwen3-0.6B
darsay archive https://huggingface.co/Qwen/Qwen3-0.6B
darsay archive Qwen/Qwen3-0.6B
```

Datasets:

```bash
darsay archive huggingface:datasets/owner/name
darsay archive datasets/owner/name
```

The canonical form is `huggingface:<locator>`. Unprefixed `owner/name`
is Hugging Face shorthand. A second host is another prefix, not a new
command: [Sources](../docs/SOURCES.md).

---

## Next

- [Concepts](../docs/CONCEPTS.md) — the objects these recipes assume
- [Documentation home](../docs/README.md) — specs, design, testing
- [Incremental transfer](../docs/INCREMENTAL.md) — pin, reconcile, shard, assemble, skeletons

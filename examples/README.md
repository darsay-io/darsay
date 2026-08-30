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
| Move a half-finished archive to another disk | [Relocate a partial](#move-a-partial-bundle) |
| Write curator notes | [Curation](#write-curator-notes) |
| Curate a want-list and share it | [Share a catalog](#share-a-catalog) |
| Prove the bytes have not drifted | [Verify](#verify-on-a-schedule) |

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

## Price one quant from a pack repo

Some GGUF repos are hundreds of gigabytes of named quants. `--include`
prices a glob against Hub metadata, then archives only those files
(plus config/tokenizer/license sidecars). The manifest records the
omitted upstream files.

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

The payload is a Hub snapshot. Loaders that understand Hugging Face
directories understand a bundle.

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
`assemble`, with [`--move`](#archive-in-halves-across-two-disks).)

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
to the big drive with `--move`, and the laptop keeps a **skeleton** — the
pin and the hashes, with the moved bytes deleted — so it can fetch the
*other* half without re-downloading the first.

```bash
# laptop, at the café — the first 30 GB
darsay archive Qwen/Qwen3.8-27B --max-gb 30

# laptop plugged into the big drive — hand the half over, keep the skeleton
darsay --vault /Volumes/big assemble ~/darsay/qwen--qwen3.8-27b/<rev> --move

# laptop, back at the café — the other 30 GB (the moved half is never re-fetched)
darsay archive Qwen/Qwen3.8-27B --max-gb 30

# big drive — the second hand-over completes it and dissolves the skeleton
darsay --vault /Volumes/big assemble ~/darsay/qwen--qwen3.8-27b/<rev> --move
darsay --vault /Volumes/big archive Qwen/Qwen3.8-27B     # registers, zero network
```

`--move` only ever deletes a file the destination has re-hashed against the
pin, so a bad copy never costs the laptop its only copy. On the laptop,
`darsay list` shows the skeleton — `archiving: 52% (…, 27.8 GB moved out)` —
and an `archive` run there stops cleanly with `assemble to register` once
everything left is either verified locally or already moved away. A skeleton
with every file moved out is removed, leaving exactly what a plain `mv`
would have. Full design: [Incremental transfer](../docs/INCREMENTAL.md#across-disks-assemble---move-and-skeletons).

---

## Move a partial bundle

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

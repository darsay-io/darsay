# Classify — masters-first acquisition as the archive default

| | |
|---|---|
| **Author** | Jeremy Norris (phase-1 validation drafted with Claude) |
| **Date** | 2026-08-31 |
| **Status** | Proposed — all open questions ratified 2026-08-31 (see [Open Questions](#open-questions)); awaiting final go for implementation |
| **Audience** | darsay CLI implementers; readers of `docs/QUANTIZATION.md` |
| **Related** | darsay 0.14.3 · manifest schema 1.6.0 → **1.7.0** · catalog schema 1.1.0 → **1.2.0** · `transfer_version` 1 (unchanged) · MVB format (unchanged) |

---

## Overview

`docs/QUANTIZATION.md` already draws the line this proposal mechanizes: a
model repo holds **masters** (weights that cannot be re-derived — the
negative) and **prints** (mechanical transformations of a master — cache,
not archive). Today that line is applied by hand, and `darsay archive`
fetches the shipping box: for a repo that ships a master plus its own
derivatives, the default behavior spends disk on bytes the policy says
are cache.

This proposal makes the policy the default behavior:

```bash
darsay archive OBLITERATUS/Qwen3.8-27B-OBLITERATED
# pins, classifies from bounded header reads, and fetches the artifacts
# that are genuinely hard to regenerate — masters, everything it cannot
# classify, and all support files — skipping only confident prints.
# The skip is printed, recorded in the manifest, and reversible with --full.
```

Classification is mechanical and bounded: small JSON files (`config.json`,
`*.index.json`), GGUF key/value headers, and safetensors headers of
unindexed files, fetched as byte ranges — a few tens of MB against a
multi-hundred-GB repo, with every cap recorded. Verdicts come from a
closed enum — **master**, **print**, **support**, **unknown** — where
`unknown` means *darsay refuses to guess, so the files are fetched*. Only
a confident `print` is ever skipped, the classification is frozen into
the pin (re-runs resume it; rules never reshape an existing bundle), and
the manifest records the selection with the full upstream inventory it
left out, as `source.subset` already does for `--include`.

Three surfaces carry it:

- `darsay archive` — masters-first, full stop; `--full` fetches
  everything for one run; `--include` remains the curator's explicit
  override. There is no compatibility knob: darsay is greenfield, the
  old default was the wrong default, and the change is documented as
  breaking in `CHANGELOG.md` rather than hedged in config.
- `darsay estimate` — prices what archive will actually do, everywhere
  the system quotes a size: the single-source report (both numbers: the
  repo and the to-fetch subset), the catalog refresh, and the stored
  digest the board reads. Plus a free redundancy smell and a new
  catalog hint, **`redundant`**, when weight bytes far exceed one copy
  of the published parameter count.
- `darsay classify` — a new verb for the full evidence: the per-set
  verdict table, rule ids, rationale, per-set globs, and `--json` for
  scripts. This is the audit view of what archive's default did or will
  do.

The layering stays clean: catalogs store the same digest they always
did (one new key and one new hint, schema 1.2.0), the darsay.io board
keeps reading catalog data and inherits honest prices for free, and the
one existing board is re-priced with a single `estimate` run after
release — that is the whole migration.

Nothing is ever deleted by any of this. Already-archived bundles are
untouched; bytes can only be *not fetched*, loudly, reversibly, and on
the record.

---

## Background & Motivation

### The motivating case

`huggingface:OBLITERATUS/Qwen3.8-27B-OBLITERATED` — entry 4 on board
`3b8cb153111534e3c468907ded2a50f7` (catalog `summer-2026-heater`), status
`have`, 239.69 GB on the 2 TB external SSD. It reads as 4.3× the size of
its base `Qwen/Qwen3.8-27B`. It is not a bigger model: both are 27.78 B
parameters at BF16.

| entry | payload | params | dtype | bytes/param |
|---|---|---|---|---|
| `Qwen/Qwen3.8-27B` | 55.59 GB | 27.78 B | BF16 | 2.001 |
| `OBLITERATUS/…-OBLITERATED` | 239.69 GB | 27.78 B | BF16 | **8.628** |

2.0 bytes/param is textbook BF16 — one copy. 8.6 means repeated weights.
Both numbers are already in every estimate; the ratio is a free
redundancy signal darsay currently throws away — and the archive default
this proposal replaces would have spent 239.69 GB where 55.6 GB held
everything irreplaceable-or-undecidable... almost (see below).

A manual investigation (HF API metadata plus a few hundred MB of HTTP
range reads — ~0.1 % of the repo) established, reproducibly:

- 7 GGUF quants + mmproj (128.54 GB) carry **no** `quantize.imatrix.*`
  keys — plain `llama-quantize` output, no calibration corpus baked in.
- `model.safetensors.index.json` selects a 28-shard BF16 set (55.56 GB).
- A second, complete, same-shaped 18-shard BF16 set (55.56 GB) is
  referenced by **no** index and is not loadable as shipped.
- The two sets are different **builds**, not a re-shard: sampled tensors
  are identical below layer ~26 and differ above — two abliteration runs
  over a shared base.
- Provenance disagrees with itself: the repo's abliteration metadata
  names one build; the GGUF headers name another (`s99-merged-fixed`).
  Neither is a published ref. **Nothing in the repo establishes which
  build the GGUFs were made from.**
- All 18 orphan shards differ from upstream `Qwen/Qwen3.8-27B` by LFS
  SHA-256 — not copies of the base.
- Abliteration itself is irreproducible: the refusal-direction prompt
  set is `"builtin"` to an unpublished tool. These weights are masters
  in the QUANTIZATION.md sense.

Every fact came from metadata plus bounded range reads. That is what
makes a mechanical default plausible: the investigation was expensive in
attention, not in bytes. And the investigation's *refusals* matter as
much as its findings — on this repo the honest mechanical outcome is
"keep 111 GB (masters + undecidable), skip nothing confidently" (§ The
rules, below), which is why the default must fetch what it cannot
classify rather than guess.

### What is mechanically decidable — and what is not

Decidable from metadata plus bounded range reads:

| Signal | Cost | Establishes |
|---|---|---|
| `*.index.json` | one small GET | which files constitute the loadable model |
| safetensors header | ~45 KB/file | tensor names, shapes, dtype |
| GGUF KV header | bounded progressive read | `quantize.imatrix.*` presence; `general.*` source claims; quant level |
| `config.json` | one small GET | native dtype; `quantization_config` |
| Hub tags / LFS SHA-256 | already fetched | `base_model:` lineage; byte-identity to a named base |
| bytes/param ratio | free | redundancy smell |

**Not** decidable, and therefore never guessed:

- Whether two full-fidelity weight sets are the same model. Sampling
  proves difference; nothing cheap proves identity.
- Which build a derivative was made from when the recorded provenance
  disagrees with itself — the case-study GGUFs exactly.
- Whether an irreproducible artifact is *historically significant*
  (QUANTIZATION.md §2). Social fact, not file fact.

### The promise this default makes, stated

"No imatrix" buys *functional* regenerability, never *bit* recovery:
`convert_hf_to_gguf.py` and `llama-quantize` drift across llama.cpp
versions. QUANTIZATION.md already concedes this for `hydrate --quantize`
("*a* Q4_K_M under the recorded toolchain, not bit-identical to any
published one"). A default that skips prints therefore changes the
archive's promise for those files from *these bytes* to *an equivalent
artifact can be derived from the master*. This proposal makes that a
**stated, recorded promise**: it is the definition of masters-first in
QUANTIZATION.md, it is printed in archive's preflight and classify's
legend, and the manifest records exactly which files were skipped under
it, with sizes and hashes (`source.subset.full_files`, as `--include`
records today). Wanting the historical bytes anyway — the
community-standard GGUF people actually ran — remains §2 policy and one
flag away (`--full`, or archiving the quant repo as its own satellite
bundle).

---

## Goals & Non-Goals

### Goals

- `darsay archive <model>` acquires, by default, every artifact that is
  hard or impossible to regenerate — masters, everything undecidable,
  all support files — and skips only confident mechanical prints,
  loudly and on the record.
- Mechanize the QUANTIZATION.md master/print line from metadata plus
  bounded range reads — no full-file reads, nothing written by
  estimate/classify, every cap recorded.
- Reproduce the case-study investigation's *conclusions* automatically,
  including its refusals: where evidence ran out for a human, the tool
  fetches rather than guesses.
- Freeze each classification into the pin so a bundle's contents are
  stable across darsay versions and re-runs; feed the existing subset
  machinery (`select_subset`, `source.subset`) rather than invent a
  second subset mechanism.
- One price with one meaning everywhere a size is quoted — `estimate`
  output, catalog rows, stored digests, the board: the cost of the
  default acquisition, with the full repo size as context. Surface the
  redundancy smell for free on catalog rows (`redundant`).
- New capability lands on the `SourceProvider` protocol, implemented for
  Hugging Face and the fake `test:` provider; the hermetic suite stays
  hermetic; graceful degradation everywhere (a failed read means *fetch
  it*, never *skip it* and never a crash).

### Non-goals

- **Deleting anything, ever.** No verdict removes archived bytes.
  Slimming an existing bundle (entry 4 today) is out of scope and listed
  under Open Questions.
- Cross-repo worth judgments ("is this GGUF pack worth keeping given the
  base is archived?"). Classify judges a repo against itself, plus two
  narrow recorded checks (byte-identity to the declared base; base
  presence in the local vault for that one rule). Anything wider is §2
  policy — a human call.
- Byte-sampling comparison between suspected duplicate sets. It proves
  difference, never identity, so it can never justify a skip. Deferred.
- Verifying print claims by regeneration (`hydrate --quantize`,
  QUANTIZATION.md §4, still proposed). This design records the evidence
  a future verifier needs and otherwise stays out of its way.
- Datasets. The policy applies to models; dataset archives are unchanged.
- Any MVB or transfer-ledger change.

---

## Key Decisions

1. **Masters-first is the archive default, expressed as a recorded
   acquisition policy — not a deletion feature.** `darsay archive`
   resolves the pin, classifies, and fetches `master` + `unknown` +
   `support`; only confident `print` sets are skipped. Precedence:
   `--include` (curator's explicit subset, exactly as today) beats
   `--full` (whole repo) beats the default. There is deliberately no
   config knob restoring the old default: one behavior, stated in the
   docs, breaking-noted in the changelog — a machine-wide toggle would
   make the same command mean different things on different machines,
   the opposite of a system other things layer on. The safety
   analysis that made the earlier draft propose-only was about
   *deleting archived bytes*; skipping at acquisition is a different
   asymmetry: while upstream lives, `--full` recovers everything, and
   if upstream vanishes, what was skipped is precisely what the kept
   master can regenerate functionally — the stated promise. What would
   be unrecoverable is a skipped *master*, which is why every
   undecidable case fetches (Decision 3) and why a failed or
   unavailable header read degrades to *fetch*, never to *skip*.

2. **The classification is frozen into the pin.** Classification runs
   once, when a pin is created; the selection is recorded in the ledger
   and manifest, and re-runs resume that pin unchanged — the same
   resume-the-pin behavior `--include` has today ("later reruns without
   `--include` resume that pin rather than expanding it"). This answers
   the version-drift objection to a behavior-bearing default: improved
   rules change only *future* pins, never the meaning of an existing
   bundle, and `--force` re-pins deliberately. It also means the
   expensive-attention step happens exactly once per bundle.

3. **A `print` verdict requires an unambiguous derivation edge — this
   is the fail-safe of the whole default, and it departs from the
   handoff's drafted rule.** The draft said "`.gguf` without imatrix,
   and a full-fidelity master is archivable → print". That rule cannot
   distinguish itself from its own companion rule "no master anywhere →
   master" when the GGUF's source build is not establishable — the
   case study exactly: two differing BF16 builds in-repo, GGUF headers
   naming a third (`s99-merged-fixed`) that may be neither. If those
   GGUFs derive from an unpublished third build, they are its only
   surviving form — masters. Undecidable-between-print-and-master *is*
   `unknown`, and under the default policy `unknown` means fetch. So: a
   GGUF without imatrix is `print` only when the repo holds exactly one
   full-fidelity candidate source and its header does not claim an
   external one; with two or more differing candidates, or an external
   source claim, it is fetched with the rationale printed. The residual
   assumption in the single-candidate case — an author's GGUFs derive
   from the full-fidelity weights they sit next to — is stated in the
   docs as an assumption; it is the shape of the overwhelming majority
   of mixed repos, which is where the real savings live.

4. **In-repo scope, plus two narrow, recorded outside checks.**
   Verdicts are facts about one repo's files relative to that repo's
   other files. Never inferred from a repo *name* — the same line
   `hints_for()` already draws. Two exceptions, both mechanical and
   recorded: (a) a file byte-identical (LFS SHA-256) to a file in the
   repo's declared `base_model` is `print (exact: true)` — byte
   identity is proof, and the case-study investigation ran exactly this
   check; (b) under the archive policy, that exact-duplicate rule may
   *skip* the file only when the identical bytes are verified in the
   local vault (the base bundle is registered) — otherwise the file is
   fetched with the rationale "byte-identical to `<base>`; archive the
   base to make this skippable". Without (b), the default could skip
   bytes whose bit-exact recovery depends on an upstream that may
   vanish. A pure quant-pack repo (all GGUF, no full-fidelity set)
   classifies as all-master and fetches in full: within that repo those
   bytes are the only form, and whether to archive the pack at all
   given the base is §2 policy.

5. **Orphan is a per-directory fact.** An orphan is a full-fidelity
   safetensors file excluded by an index **in its own directory**. A
   directory with no index does not orphan its files — a single
   `model.safetensors`, or a diffusers-style component tree, must not
   come out `unknown` merely for lacking an index. The case-study
   18-shard set sits beside an index that excludes it → orphan →
   `unknown` → fetched, loudly.

6. **The closed verdict enum is `master | print | support | unknown`,
   over weight files; everything else is `support`.** `support` reuses
   estimate's word for non-weight files and is always kept — sidecars,
   licenses, and irreplaceable provenance records like
   `abliteration_metadata.json` (tiny and priceless). There is no
   `duplicate` verdict: byte-identity is `print` with `exact: true`,
   the one print whose promise is bit-exact. Verdicts never enter a
   catalog digest or ledger as free-standing facts; they enter the
   manifest only as the recorded rationale of a selection that actually
   happened (Decision 7).

7. **The manifest records the policy selection — schema 1.6.0 →
   1.7.0.** A policy-subset bundle records `source.subset` exactly as an
   `--include` bundle does (patterns/paths, kept/omitted counts and
   bytes, `full_files` with sizes and hashes), plus `policy:
   "masters"`, the darsay version that classified, and a compact
   per-set verdict record (set, rule id, one-line reason, evidence
   digest). This is not fabrication — it is the recorded provenance of
   a decision the tool actually made, the same way `query_limit`
   records a cap that actually applied. Additive bump; `--include` and
   full-repo bundles are unchanged (`policy` absent).

8. **`include: null` means "the default acquisition" — everywhere.**
   Entry identity stays `(source, revision, include)`. A null-include
   entry is satisfied by a `policy: "masters"` bundle or by a full-repo
   bundle (a superset satisfies the want); an entry with explicit
   include globs matches only its globs, as today. The same reading
   fixes pricing: a null-include row's digest is the policy price
   (Decision 9), so the board's SIZE column, `remaining_bytes`, and
   disk verdicts all quote what having the entry actually costs. The
   original complaint — that darsay priced the shipping box — dies
   here, not just in the single-source report.

9. **`estimate` prices what `archive` will do — including in catalog
   refresh.** For a model source with no explicit `--include`, estimate
   runs the same classification and shows both numbers — the repo and
   the to-fetch subset — with the skip summarized and `--full` noted;
   the disk verdict prices the subset. This widens estimate's network
   contract from "metadata only" to "metadata plus bounded header
   reads, nothing written, receipts printed" — accepted, because an
   estimate that prices 239.69 GB for an archive that will fetch
   55.6 GB is not a preflight, it is a misdirection. `estimate CATALOG`
   classifies model rows the same way — a per-run total read budget,
   recorded like `query_limit`, keeps a 50-row refresh bounded, and a
   row that exceeds it prices full and says so. The stored digest's
   `payload_bytes` is therefore always *the priced acquisition* — the
   meaning it already has for `--include` rows ("the priced payload is
   the subset when `--include` applied") — and the digest gains a
   `policy` key recording when the masters policy did the pricing. One
   number, one meaning, from `estimate` output to `catalog.json` to the
   board. `darsay classify` remains the full-evidence view (verdict
   table, rule ids, rationale, per-set globs, `--json`); estimate shows
   the one-line summary.

10. **One new provider primitive: bounded remote reads.**
    `SourceProvider.read_bytes(source, revision, relative, start,
    length) -> bytes` — exactly the requested range (short only at
    EOF), raising the `SourceError` family on failure; base-class
    default raises (capability missing). The Hugging Face
    implementation issues Range requests through the Hub client's
    session (auth, retries, CDN redirects for free; a server ignoring
    Range is guarded by reading at most `length`). The fake `test:`
    provider slices its in-memory bytes — the extensibility check that
    keeps every rule testable without a network. `estimate`, `classify`,
    and the classification step of `archive` never import
    `huggingface_hub`. Any read failure — gated without a token,
    capability missing, truncated, malformed, over cap — yields
    `unknown` (= fetch) with a recorded `skipped: <why>`.

11. **Read budget: tiers, caps, and a printed receipt.** Tier 0: pin
    metadata darsay already holds (inventory, sizes, hashes, tags,
    `info.safetensors`). Tier 1: small JSON GETs — `config.json`, every
    `*.index.json` (10 MB sanity cap). Tier 2: headers — GGUF KV tables
    read progressively (`llama-quantize` appends the imatrix keys
    *after* the embedded tokenizer arrays, so the whole KV table is
    traversed; bulk numeric arrays are skipped by offset arithmetic
    without fetching, string arrays must stream; 64 MB per-file fetch
    cap), and safetensors headers **only for files no index accounts
    for** — indexed shards need no header read, because index +
    `config.json` establish membership and dtype. That answers
    "46 shards = 46 requests": zero indexed-shard reads; the case study
    costs roughly config + index + 8 GGUF headers + 18 orphan headers —
    tens of requests, tens of MB, against 239.69 GB. All caps recorded
    in JSON and printed: `read: 61 requests, 96.4 MB fetched (caps: 64
    MB/GGUF header, 64 header reads) — nothing written.` A capped or
    failed read is `unknown`, never a crash.

12. **One new hint, `redundant` — and only that one.** Computable
    inside the existing hint contract from a live estimate alone:
    weight bytes ≥ 1.75 × Σ(params_by_dtype × dtype width); dtype or
    params unknown ⇒ no hint; no reads, no guessing. `HINTS` becomes
    `("gated", "large", "quant", "redundant", "subset")`; catalog
    schema 1.1.0 → 1.2.0 (additive — 1.1 readers drop unknown names by
    design). Like the GGUF half of `quant`, it is computed by
    `hints_for()` on live estimates and carried in stored hints;
    `derive_hints` does **not** retro-derive it from old digests (a
    digest loses `by_dtype` and mixes support bytes into
    `payload_bytes` — deriving there risks fabricated positives). The
    proposed `derived` / `orphan` hints are rejected: they need range
    reads, cannot be recomputed from a digest, and are per-file facts
    with rationale — the wrong shape for a flat string list; their home
    is classify's output and the manifest's selection record. The
    website board does not mirror hints at all yet; that pending mirror
    picks `redundant` up with the existing four — the CLI table and
    generated catalog README show it on day one regardless.

13. **Selections feed `select_subset`, as verified globs or exact
    paths.** Compact include patterns are synthesized per set (shard
    numbers → `*`), then verified by running `subset.select_subset`
    against the full inventory and asserting the kept payload equals
    the intended selection exactly; when no compact pattern survives,
    the pin records explicit paths (fnmatch matches exact paths — ugly
    beats wrong). One subset mechanism, one manifest shape, one
    resume behavior — the classifier feeds the machinery the handoff
    named, and is literally validated through it.

14. **The design leaves the door open for falsification.** A print
    claim is falsifiable by regeneration. Classify's evidence records
    what a future `hydrate --quantize` verifier needs —
    `general.file_type`, `general.quantization_version`,
    `general.source.*` claims, candidate-source sets — so `print` can
    graduate from heuristic to tested property without reshaping this
    output. Not built now; deliberately not precluded.

---

## Proposed Design

### The set model

Classification operates on **file sets**, not bare files — the set is
the unit a human reasons about, and a 70-row table hides the 4-row
story:

1. Non-weight files (suffix not in `WEIGHT_SUFFIXES`) → one `support`
   set. Kept always. (`.onnx` is not a weight suffix and lands here —
   the safe direction.)
2. Per directory containing safetensors: each `*.index.json`'s
   `weight_map` defines an **indexed set** (the index file itself rides
   as a sidecar of that set); remaining safetensors in that directory
   are **orphans** if an index exists there, else a **standalone set**.
3. Each `.gguf` is its own set (self-contained), grouped for display by
   quant level.
4. Legacy `.bin` / `.pt` / `.pth` weight files form a set per
   directory, using `pytorch_model.bin.index.json` when present.

### The rules

Evaluated per set; every verdict carries its rule id and a one-line
reason into the output and the manifest's selection record.

| # | Condition | Verdict → default action |
|---|---|---|
| R1 | non-weight file | `support` → fetch always |
| R2 | file byte-identical (LFS SHA-256) to a file in the declared `base_model` | `print` (`exact: true`) → skip **only if** the base bundle is verified in this vault, else fetch |
| R3 | safetensors set selected by its directory's index, full-fidelity dtype, no quantization config | `master` → fetch |
| R4 | standalone safetensors set (no index in its directory), full-fidelity | `master` → fetch |
| R5 | safetensors carrying a quantization config (or non-full-fidelity dominant dtype) | `master` → fetch — calibration or a curated layer map may be baked in; rationale records the method |
| R6 | full-fidelity safetensors excluded by an index in its own directory | `unknown` (orphan) → fetch |
| R7 | GGUF with any `quantize.imatrix.*` key | `master` → fetch — the importance matrix is a baked-in hidden input |
| R8 | GGUF, no imatrix, header `general.source.*` names a repo other than this one | `unknown` → fetch |
| R9 | GGUF, no imatrix, exactly one in-repo full-fidelity set | `print` → **skip** — mechanical quant of the sole candidate; functional-equivalence promise, stated |
| R10 | GGUF, no imatrix, zero in-repo full-fidelity sets | `master` → fetch (only surviving form in scope) |
| R11 | GGUF, no imatrix, two or more in-repo full-fidelity sets | `unknown` → fetch (source build not establishable; Decision 3) |
| R12 | legacy `.bin`/`.pt` set beside a full-fidelity safetensors set | `unknown` → fetch (pickle equivalence not cheaply verifiable) |
| R13 | legacy set with no safetensors sibling | `master` → fetch |
| R14 | header unreadable — truncated, malformed, over cap, gated without a token, provider lacks `read_bytes` | `unknown` → fetch, reason recorded |

On the case study: 28-shard indexed set → `master` (R3); 18-shard set →
`unknown` (R6); all 8 GGUFs → `unknown` (R11 — two differing BF16 sets,
headers naming `s99-merged-fixed`); support → fetch. **Default archive
fetches 111.14 GB of 239.69 GB** — the mechanical skip is the 128.54 GB
of GGUFs *only if* their source were establishable, and it is not, so
they are fetched pending the human call the evidence table tees up. On
the common single-master repo (author BF16 + author GGUFs), R9 fires and
the default fetches the master + support only. On `Qwen/Qwen3.8-27B`
itself: one indexed BF16 set → everything fetches, nothing changes.

### `archive` flow

pin → classify (models; skipped entirely under `--include`, `--full`,
or for datasets) → select via the verified-glob / exact-path subset
record → preflight print → transfer as today.
Classification failure of any kind degrades to `--full` behavior with a
warning. Preflight (also shown by `estimate`):

```
huggingface:owner/name @ main -> 3f2ab9c…
  masters-first: fetching 55.58 GB of 186.14 GB
    master   model-*.safetensors        55.56 GB  selected by index; BF16 [R3]
    print    *.gguf (Q2_K…Q8_0)        130.56 GB  SKIPPED — no imatrix; mechanical quant of the
                                                  master above; regenerable, not bit-identical [R9]
    support  config / tokenizer          0.02 GB  kept [R1]
  --full fetches everything; darsay classify <source> shows the evidence.
  read: 14 requests, 38.1 MB fetched (caps recorded) — nothing written yet.
```

`unknown` sets, when present, appear in the same table with their
rationale and are counted in a closing line ("2 sets, 184.10 GB fetched
because darsay will not guess — darsay classify for the evidence").

### `darsay classify SOURCE`

```
darsay classify SOURCE [--revision REF] [--include GLOB]... [--json]
```

The audit view: full verdict table with rule ids, evidence
(index membership, dtypes, GGUF KVs of interest, SHA-256 matches against
the base, candidate-source lists), the read receipt, per-set globs, and
what `archive` would fetch under the current policy. Exit 0 on
completion — refusal is a finding, not an error; `--json` carries
`unclassified_count` and per-file verdicts for scripts. Read-only in the
strictest sense: bounded ranges, nothing written. Non-model sources get
a one-line explanation. Runs under `_run`; no tracebacks without
`DARSAY_DEBUG=1`.

### New modules

- `src/darsay/gguf_meta.py` — GGUF KV header parser. Stdlib only,
  mirroring `safetensors_meta.py` in posture. Parses against a
  `fetch(start, end) -> bytes` callable so the same code runs over a
  local file, a remote provider, or an in-memory test buffer; skips
  bulk numeric arrays by offset arithmetic; enforces the fetch cap;
  raises typed errors for truncation/malformation that map to R14. A
  rewrite in this style, not a copy of the investigation script.
- `src/darsay/safetensors_meta.py` — grows `read_header_via(fetch)`
  sharing parse/validate logic; the local `read_header(path)` API is
  unchanged.
- `src/darsay/classify.py` — pure functions: (inventory, fetched facts,
  vault facts for R2) → sets, verdicts, rationale, selection. No
  network, no provider imports; testable exactly like `subset.py`.
- `providers/base.py` — `read_bytes` with the degrading default;
  `providers/huggingface.py` — Range implementation; `tests/fakes.py` —
  in-memory implementation.
- `archiver.py` / `cli.py` — policy step in archive, `--full`,
  `cmd_classify`; `catalog.py` — `redundant` hint, digest `policy` key,
  overlay matching rule; `estimate.py` — the policy block, redundancy
  note, and catalog-refresh classification under the read budget;
  `metadata.py` — `source.subset.policy` + selection record.

### Prerequisite fix: index files are sidecars

`SIDECAR_GLOBS` omits `*.index.json` (`subset.py:16`), so any
`--include '*.safetensors'` against a sharded repo yields a bundle whose
weights are all present but which cannot load — no weight map. The
policy selection depends on "keep the indexed set" being expressible
through the subset machinery, so this ships **first, as its own fix with
its own test**: add `*.index.json` and `video_preprocessor_config.json`
(the case-study repo ships one) to `SIDECAR_GLOBS`. Known wart,
accepted: a GGUF-only subset of a mixed repo carries a dangling index
referencing absent shards — harmless to llama.cpp, no worse than the
`config.json` such bundles already carry. Board entry 4's
`include: null` (pricing at 239.69 GB) is a data fix once the human call
on its GGUFs is made — recorded here so it is not forgotten; no code.

---

## Interface Changes

### CLI

| Surface | Change |
|---|---|
| `darsay archive` | **Default is masters-first** for models: classify at pin time, fetch master/unknown/support, skip confident prints, print the table. New flag `--full` (whole repo). `--include` unchanged and takes precedence. Re-runs resume the pinned selection; `--force` re-pins. |
| `darsay classify SOURCE` | **New verb.** Full evidence table, read receipt, per-set globs, `--revision` / `--include` / `--json`. Exit 0. |
| `darsay estimate` | Prices the policy subset for models (both numbers, skip summary, `--full` note); redundancy note + `redundant` hint. Bounded header reads added to its contract, receipts printed. Catalog refresh classifies model rows under a recorded total read budget. |

### Provider protocol

Additive: `SourceProvider.read_bytes(source, revision, relative, start,
length) -> bytes`, default raises `SourceError`. Implemented for
`huggingface` and the test provider. Existing methods unchanged.

### Schemas

| Schema | Change |
|---|---|
| manifest | 1.6.0 → **1.7.0** (additive): `source.subset.policy` (`"masters"`, absent otherwise), `source.subset.classification` — classifier version + per-set `{set, rule, verdict, reason}` for policy bundles. `docs/MANIFEST.md` updated. |
| catalog | 1.1.0 → **1.2.0** (additive): `HINTS` gains `redundant`; the digest gains `policy` (`"masters"` when the stored price is the policy subset, absent otherwise). Overlay matching (null include ⇔ policy or full bundle) is logic, not schema. `docs/CATALOGS.md` updated. |
| MVB, `transfer_version` | Unchanged. |

### Docs

- `docs/QUANTIZATION.md` — §4 gains "Implemented: masters-first archive
  (`darsay archive`) and `darsay classify`", including the
  functional-equivalence promise and the R9 assumption, stated plainly;
  the decision-guide table's first row becomes the default behavior.
- `docs/MANIFEST.md`, `docs/CATALOGS.md`, `docs/CONCEPTS.md` (pin
  section: the selection is part of the pin), `docs/README.md` reading
  map, `examples/README.md` cookbook ("archive just the master";
  "fetch everything: --full"), `CHANGELOG.md` — with the behavior
  changes under an explicit **Breaking** heading: the archive default
  (was: whole repo; now: masters-first; recover with `--full`), the
  meaning of estimate's priced payload and the stored digest (was: the
  shipping box; now: the default acquisition), and the board prices
  that follow from it. Greenfield means fix forward, not silently:
  each breaking entry names the old behavior, the new one, and the
  one-line recovery. Also Fixed: index sidecars; Added: classify,
  `redundant` hint, `read_bytes`.
- Nothing documented before it ships.

---

## Alternatives Considered

### 1. Advisory-only: `classify` prints globs, the human pastes (the earlier phase-1 draft)

Rejected as the end state by the project owner's direction, and on
reflection the safety argument for it conflated two different acts. Not
*deleting* archived bytes on a heuristic remains non-negotiable — and
survives in this design (nothing deletes). Not *fetching* regenerable
prints at acquisition time is a different, recoverable act: `--full`
while upstream lives, regeneration-from-master after. The advisory flow
also fails the person it was meant to protect: the default path (no
flags) would keep spending 4× disk on shipping boxes, and the safety
would live in a verb most users never run. The evidence table survives
intact — in archive's preflight and the `classify` verb.

### 2. `estimate --classify` instead of a verb; or no verb at all

A verb-less design (archive preflight + estimate summary only) was
considered once archive integration landed: rejected because the
evidence table with rule ids and per-set globs is the audit trail for a
default that now has behavioral consequences — it needs a first-class,
scriptable home (`--json`). Folding it into `estimate` as a flag was
rejected for output-shape and contract reasons: estimate summarizes,
classify explains.

### 3. Classify on every archive run (no pin freezing)

Rejected: rule improvements would silently change what an existing
bundle means, and resumed transfers could flip selections mid-flight.
Pin-freezing gives byte-stable bundles, cheap re-runs, and a deliberate
re-pin gesture (`--force`) — the exact shape `--include` pins already
have.

### 4. `derived` / `orphan` / `redundant` all as catalog hints

Only `redundant` survives (Decision 12). The other two need range
reads (a catalog refresh must never issue dozens of Range requests per
row), cannot be re-derived from a digest, and are per-file facts with
rationale — not flat repo-level strings. Their durable home is the
manifest's selection record.

### 5. The handoff's rule 2 as drafted (no-imatrix + master ⇒ print)

Rejected in the multi-candidate case — Decision 3. Under an
auto-skipping default this matters more, not less: the drafted rule
would have skipped 128.54 GB of case-study GGUFs whose source build
nothing establishes. Kept in the single-candidate case with the
assumption stated.

### 6. Skipping R2 exact duplicates unconditionally

Rejected — Decision 4(b). Byte-identity to the *base repo* is proof of
duplication but not of local recoverability; the skip is allowed only
when the identical bytes are verified in this vault.

### 7. Vault-aware classification generally ("you archived the base, so its quants are prints")

Deferred beyond the narrow R2 check. It changes verdicts from
repo-facts to situation-facts and deserves its own flag and proposal
once the repo-scoped core has earned trust.

### 8. Byte-comparison `--deep` tier for suspected duplicate sets

Deferred. Sampling proves difference, never identity, so it can never
justify a skip — only enrich an `unknown`'s rationale (as it did, by
hand, in the case study). `classify.py`'s pure-function shape accepts
extra evidence without restructuring if this is ever wanted.

### 9. A config escape hatch (`archive.policy = full`)

An earlier cut of this proposal shipped one. Dropped: darsay is
greenfield with one board to port, and a machine-wide toggle would make
`darsay archive X` mean different things on different machines forever,
to spare a migration that amounts to one CHANGELOG entry and one
re-priced board. `--full` covers the per-run need honestly (visible in
the command, in the manifest, in shell history); if a real
mirror-everything operator ever appears, `--full` in their script is
still the right shape — silent divergence in config is not.

---

## Security & Safety

- **No deletion path exists.** Nothing here removes archived bytes;
  the policy only chooses what to fetch, and records the choice with
  the full omitted inventory and hashes.
- **Refusal fails safe — toward fetching.** Every undecidable,
  unreadable, capped, gated, capability-missing, or
  classification-crashed case resolves to fetch. The only skips are R9
  (single-candidate mechanical quant, assumption documented) and R2
  gated on vault-verified bytes.
- **Nothing is skipped silently.** The preflight table names every
  skipped set, its size, its rule, and the recovery path (`--full`,
  regeneration); the manifest records the same durably; `estimate`
  shows it before any archive begins.
- **Selections are verified before they bind.** Synthesized globs must
  reproduce the intended set exactly through `select_subset` or the pin
  records explicit paths; a glob that would orphan an unknown cannot
  survive verification.
- **Stable under time.** Pin-freezing means rule evolution never
  reshapes an existing bundle; `--force` is the only re-pin.
- **Gated repos are not bypassed.** Range reads ride the provider's
  normal auth; a denied read is a recorded reason and a fetch, never a
  retry storm or an alternate route.
- **Bounded and visible.** Every read capped, every cap recorded,
  totals printed. No full-file payload reads during classification.
- **No tracebacks** without `DARSAY_DEBUG=1`; `SourceError` text stays
  CLI-ready.

---

## Rollout

Order matters; each step lands green (`ruff check`, `ruff format
--check`, `pytest`) before the next.

0. **Sidecar fix** (independent bug): `*.index.json` +
   `video_preprocessor_config.json` in `SIDECAR_GLOBS`; integration
   test that `--include '*.safetensors'` on a sharded fake repo keeps
   the index and passes completeness.
1. **Provider primitive**: `read_bytes` on base (degrading default), HF
   (Range via the Hub session), test provider (in-memory slice); unit
   tests incl. short-read-at-EOF and the default's `SourceError`.
2. **`gguf_meta.py`**: fetch-callable parser; unit tests over crafted
   tiny GGUFs — imatrix keys present/absent, numeric-array leapfrog,
   string-array streaming, truncated header, bad magic, cap hit.
3. **`safetensors_meta.py` remote path**: `read_header_via(fetch)`;
   local API and tests untouched.
4. **`classify.py`**: pure rules R1–R14 (a unit test per rule), the set
   model (per-directory indexes, orphans, standalone), glob synthesis +
   `select_subset` verification incl. the exact-path fallback, the R2
   vault gate.
5. **`darsay classify`**: cmd, table/JSON, read receipt; integration
   tests via the fake provider — single-master+GGUF repo (R9), the
   case-study shape (two BF16 sets + GGUFs → R3/R6/R11), quantized
   config, no-index repo, gated degrade, capability-missing degrade.
6. **Archive integration**: policy step, `--full`, preflight table,
   pin-freeze + resume, manifest 1.7.0 selection record,
   degrade-to-full on classification failure;
   integration tests: default skips R9 prints and the provider never
   sees a request for them; resume does not re-classify; `--force`
   re-pins under changed rules; `--include` and `--full` bypass; a
   fake-provider read fault mid-classification degrades to full fetch.
7. **Estimate + catalogs + hint**: policy block in estimate; catalog
   refresh classification under the recorded total read budget (tests:
   a row over budget prices full and says so); digest `policy` key;
   overlay null-include ⇔ policy-or-full matching (tests: board shows
   `have`); `redundant` in `hints_for`/`HINTS`, schema 1.2.0, threshold
   edges, unknown-dtype suppression, no retro-derivation from digests.
8. **Docs & changelog** as listed under Interface Changes.
9. **Verification**: opt-in e2e — `estimate`/`classify`/`archive` of
   `sshleifer/tiny-gpt2` (single master; asserts the default changes
   nothing for a plain repo); one manual live run against the
   case-study repo checking verdicts against the handoff's evidence
   (28-shard master, 18-shard orphan-unknown, GGUFs
   unknown-with-provenance-rationale, 111.14 GB fetch plan).

### Website & board coordination

The darsay.io board layers on catalog data and needs no code to
benefit from honest prices. Two follow-ups ride the next site release:
mirror `hints` (still unmirrored for the existing four names;
`redundant` arrives with them) and pass the digest's `policy` key
through so the board can label a price "masters". The one existing
board (`summer-2026-heater`, `3b8cb153111534e3c468907ded2a50f7`) is
ported by running `darsay estimate summer-2026-heater` once on the new
release — entry 4 then prices at its true acquisition cost instead of
239.69 GB, and its GGUF decision, once made, is recorded as an include
glob or a note like any other curation. That single re-estimate is the
entire migration surface of the breaking changes.

### Acceptance

Acceptance is the handoff's §8 list, with three amendments ratification
should bless: (a) the surface is `archive`-default + `classify` verb,
not `estimate --classify`; (b) on the case-study repo the correct
mechanical outcome is fetching the GGUFs as `unknown` — the refusal
matches the evidence of §2.3, and the drafted print rule would have
guessed; (c) the changes ship as breaking defaults with CHANGELOG
**Breaking** entries — no compatibility knobs, no transition release.

Future work, explicitly out of this cut: slimming existing bundles,
vault-aware verdicts beyond R2, byte-comparison evidence, `hydrate
--quantize` print verification.

---

## Open Questions

All six were put to the project owner and ratified on 2026-08-31; they
are recorded here as decisions, with the fork that was weighed.

1. **The R9 assumption — RATIFIED: accept, documented.** A no-imatrix
   GGUF beside exactly one full-fidelity set is skipped on the
   assumption it derives from that set; the residual risk (author
   quantized a build they never uploaded) is invisible by construction.
   The alternative — demanding a positive `general.source.*` match —
   would zero the savings on most well-behaved repos, since most GGUFs
   do not record a source. The preflight names every such skip; the
   worst-case loss is the exact bytes of a quant whose source build was
   never published, while the kept master regenerates an equivalent.
2. **Slimming an existing bundle — RATIFIED: defer; manual path.**
   (Entry 4 holds 239.69 GB today.) Archive the policy subset as a new
   pin — sibling-blob reuse should satisfy it from the existing bundle
   with zero network — then `darsay rm` the full bundle. Cheap at
   current scale; a dedicated verb would be the one operation deleting
   bytes downstream of a verdict, and earns a proposal of its own if
   slimming ever becomes a chore.
3. **Refresh read budget — RATIFIED: generous but recorded.** On the
   order of 256 requests / 512 MB per `estimate CATALOG` run, tuned
   after the first real refresh of the existing board.
4. **`redundant` threshold — RATIFIED: 1.75×.** Catches an exact
   second copy (2.0×) with margin; a lower ~1.3× (also flagging
   master-plus-one-quant repos) was weighed and passed over for its
   false-positive risk on repos whose published parameter metadata
   undercounts. One constant; revisit with board data.
5. **Verb name — RATIFIED: `classify`.** `appraise` was weighed
   (museum register) and passed over for discoverability.
6. **Dangling index wart — RATIFIED: acceptable.** A GGUF-only subset
   of a mixed repo carries an index referencing absent shards; harmless
   to llama.cpp. Set-aware sidecar selection was weighed and passed
   over as real complexity for a cosmetic problem.

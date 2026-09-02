<p align="center">
  <a href="GETTING-STARTED.md">Start here</a> ·
  <a href="CONCEPTS.md">Concepts</a> ·
  <a href="../examples/README.md">Examples</a> ·
  <a href="README.md">All docs</a> ·
  <a href="../README.md">README</a>
</p>

# Negatives, prints, and precision — what gets archived, what gets derived

> **In one sentence.** Archive the negatives. Keep the prints people
> actually ran as satellites when they matter. Derive the rest at run time.

How darsay treats a model that exists in many precisions: which bytes are
irreplaceable, which are casts of them, how a size is explained, and how
"I want to run it at 4 bits" is served without archiving anything extra.

**This policy is mechanized.** `darsay archive` applies it by default
([§4](#4-mechanics)); `darsay classify` shows the per-file evidence;
`estimate` names the precision and the bytes per parameter; `--include`
pins an explicit subset; `--full` fetches everything. Case study:
Qwen/Qwen3.8-27B and the abliterated repo built on it.

## 1. Negatives and prints

Photography had the words first. A **negative** is the original exposure;
a **print** is anything you can make from it again. darsay keeps the same
line through a model repo, and there is no third word.

**Negatives** are weights nothing can regenerate:

- the full-fidelity release — the BF16 or F16 safetensors the model was
  trained in;
- a **native low-precision release** — DeepSeek's FP8, Kimi K3's MXFP4,
  an official INT4 — when no higher-fidelity public copy exists. That
  release *is* the negative; there is nothing above it to prefer;
- a published quant that cannot be rebuilt: AWQ and GPTQ need calibration
  data and a GPU run; an imatrix GGUF bakes in an importance matrix
  computed on a private corpus; an official FP8 is often a curated layer
  map (Qwen's keeps 3.08B parameters in BF16 and casts 24.7B);
- an alignment edit whose inputs were never published — an abliteration
  whose refusal-direction prompts are `"builtin"` to an unpublished tool;
- the only weight format a repo ships.

**Prints** are mechanical transformations of a negative the repo also
holds: a plain `llama-quantize` GGUF with no importance matrix sitting
beside the full-fidelity set it was cast from, or a weight set
byte-identical to one the bundle already keeps. Given the negative and a
recorded recipe they are regenerable, so they are **cache, not archive**.

Whether bytes are a negative or a print is decided from headers, indexes,
and hashes — never from a name ([§4](#4-mechanics)).

## 2. Precision, and the one number that explains a size

Every archive size is **parameters × bytes per parameter**. The second
factor is the release precision:

| Release precision | bytes / parameter | 27.78B weighs | 2.45T weighs |
|---|---|---|---|
| F32 | 4 | 103 GiB | 8.9 TiB |
| BF16 · F16 | 2 | 51.7 GiB | 4.4 TiB |
| FP8 · INT8 | 1 | 25.9 GiB | 2.2 TiB |
| MXFP4 · NVFP4 · INT4 (packed two per byte, plus scales) | ≈ 0.5–0.6 | 13–16 GiB | 1.2–1.4 TiB |

So `Qwen/Qwen3.8-2.4T-A95B` — 2.45T parameters, BF16 — is 4.4 TiB and
one negative set, nothing to skip; and `moonshotai/Kimi-K3` — 2.78T
parameters, MXFP4 with a scale byte per 32 weights — is 1.4 TiB. The
second has *more* parameters and costs a third of the disk, because it
was exposed at a quarter of the precision. Neither is "bigger"; both are
negatives.

`darsay estimate` says this on every model:

```
  parameters:   2.78T (2.72T U8 (packed), 57.18B BF16, 11.1M F32)  [upstream safetensors metadata]
  precision:    MXFP4 (compressed-tensors · mxfp4-pack-quantized · 4-bit · group 32) — 0.56 B/param: about half a byte per weight — a 4-bit release
                a native low-precision release is the negative — no higher-fidelity public copy exists to prefer
```

The label comes from `config.json`'s `quantization_config` (wherever a
multimodal config nests it), else the dominant safetensors dtype, else —
when GGUF is all a repo ships — the quant level in the file name
(`Q4_K_M`). Bytes per parameter is *measured*: the priced weight bytes
over the published parameter count. Far above two is the smell of a
repo shipping several weight sets (the `redundant` hint); near one is an
8-bit release; near a half is 4-bit. The same three facts — parameters,
precision, bytes per parameter — travel into the catalog digest and onto a
darsay.io board row, so a size explains itself wherever it is quoted.

## 3. Policy

1. **Negatives by default.** `darsay archive <model>` classifies a fresh
   pin and fetches every negative, everything it cannot classify, and all
   support files. Only confident prints are skipped — loudly, on the
   record, and reversibly with `--full`.
2. **Published prints that mattered are satellite bundles.** The official
   FP8 people served, the community-standard `bartowski` or `unsloth`
   GGUF people ran: ordinary repos, archived as ordinary bundles, linked
   to the negative through `lineage` (the manifest) and the family view
   (a catalog or board). `--include '*Q4_K_M*'` prices and archives one
   file of a pack.
3. **Everything else is hydration-time derivation.** Wanting to *run* the
   model at 4 bits is not a reason to archive a 4-bit repo. Derived
   weights are runtime state: outside bundles, recorded with provenance,
   deletable and rebuildable at will.

Why not "archive the quant, skip the 55 GB negative"? The negative is the
only artifact every downstream form can be derived from, the only one
eval-comparable across engines, and the only one the formats of 2032 can
be built from. Prints answer "what did people run in 2026"; the negative
answers everything else. When disk forces a choice, keep the negative.

## 4. Mechanics

### Implemented: negatives by default (`archive`) and `darsay classify`

A fresh `darsay archive <model>` classifies the repo's weight files from
metadata plus **bounded header reads** — `config.json`, every
`*.index.json` (up to 64 MiB; a frontier MoE weight map runs to 60 MB),
GGUF key/value tables, safetensors headers of files no index accounts
for; a few tens of MB against a multi-hundred-GB repo, every cap
recorded — and fetches the artifacts that are genuinely hard to
regenerate. Verdicts come from a closed enum:

- **negative** — irreplaceable: the index-selected or standalone
  full-fidelity weights, quantized safetensors (calibration or a curated
  layer map may be baked in — a native MXFP4 release lands here), GGUFs
  carrying `quantize.imatrix.*`, the only weight format in a repo.
- **print** — mechanically regenerable and **skipped by default**: a
  GGUF with no imatrix sitting beside exactly one full-fidelity weight
  set; a file byte-identical (LFS SHA-256) to the declared base repo
  *when that base is archived in this vault*; a weight set byte-identical,
  file for file, to another kept set in the same repo — multi-pipeline
  repos ship the same 60 GB text encoder three times, and the skipped
  twins are bit-recoverable from the copy the bundle keeps.
- **support** — config, tokenizer (including tiktoken vocabularies and
  the custom Python a `custom_code` model needs), licenses, provenance
  records: always fetched.
- **unknown** — darsay will not guess, so the files are **fetched**:
  orphaned weight sets no index selects, GGUFs whose source build cannot
  be established (several candidate sets, or a header naming an external
  source), legacy `.bin`/`.pt` beside safetensors, anything unreadable,
  capped, or gated.

Every skip is named in the preflight with its rule and reason, recorded
in the manifest (`source.subset.policy: "negatives"` plus the per-set
classification, with the full upstream inventory and hashes), and frozen
into the pin — re-runs resume the selection without re-reading a header;
`--force` re-pins under current rules. `--full` fetches the whole repo;
`--include` remains the curator's explicit override. Any classification
failure degrades to the full fetch, never the other way.

**The stated promise.** A skipped print is *functionally* regenerable
from the kept negative under a recorded toolchain — not bit-identical to
the published bytes (`convert_hf_to_gguf.py` and `llama-quantize` drift
across versions). Wanting the historical bytes people actually ran is §3
policy and one `--full` away. The single-candidate print rule rests on
one stated assumption: an author's GGUFs derive from the full-fidelity
weights they sit next to. When anything contradicts that — two differing
weight sets, an external source claim, an unreadable header — the verdict
is `unknown` and the bytes are fetched.

`darsay classify <source>` is the audit view: the per-set verdict table
with rule ids, evidence, the read receipt, and the exact selection —
`--json` for scripts. The rules, R1–R15, are recorded in
[the classify proposal](proposals/classify.md).

### Implemented: preflight sizing (`darsay estimate`)

No payload downloads, nothing written; a fresh model source is
classified the same way `archive` classifies it, so the price quoted is
the price paid:

    darsay estimate Qwen/Qwen3.8-27B                 # the negatives, priced
    darsay estimate Qwen/Qwen3.8-27B --full          # the whole repo, priced
    darsay estimate Qwen/Qwen3.8-27B --variants      # + quantized ecosystem, sized
    darsay estimate unsloth/Qwen3.8-27B-GGUF --include '*Q4_K_M*'   # one quant of a pack

The `negatives:` line says what the price is made of — `1 negative set
(29 files, 51.7 GiB) + 9 sets darsay will not guess about (171.5 GiB),
fetched` — or that prints were skipped and by how much. The `precision:`
line names the release precision and the measured bytes per parameter
([§2](#2-precision-and-the-one-number-that-explains-a-size)); `family:`
and `lineage:` place the work ([Concepts → lineage](CONCEPTS.md#lineage)).
Exact numbers (file sizes, parameter counts by dtype) come from upstream
metadata; derived numbers (RAM x1.2, download scratch) are labeled
estimates. Every cap is recorded (`query_limit`, the read receipt).

### Implemented: subset archiving (`archive --include`)

Pack repos (one repo, 27 quant files) make "bundle = whole repo" the wrong
unit: archiving one 16 GB Q4_K_M shouldn't cost 430 GB.

```bash
darsay estimate unsloth/Qwen3.8-27B-GGUF --include '*Q4_K_M*'
darsay archive  unsloth/Qwen3.8-27B-GGUF --include '*Q4_K_M*'
```

`--include` is a glob, repeatable; a leading `/` anchors it to the repo
root and disables the filename fallback. Matching files are kept, plus
sidecar files (config, tokenizer, license, card, custom code) so a single
GGUF still loads. The pin is the subset: later reruns without `--include`
resume that pin rather than expanding it (`--force` re-pins). The manifest
records the subset honestly — `source.subset` holds the include patterns
**and the full upstream file list with sizes/hashes**, so the bundle
states exactly what it deliberately does not contain.

### Proposed: hydration-time quantization (`hydrate --quantize`, `run --quantize`)

Recipes are registry entries under each engine in `ENGINES`
(`hydrate.py`), never special cases elsewhere:

- **Load-time recipes** (no files produced): transformers +
  bitsandbytes/torchao — `--quantize int8`, `--quantize nf4`. The recipe
  only adds packages to the env and flags to the runner.
- **Derive-time recipes** (files produced): llama.cpp — convert the
  archived safetensors to GGUF, then `llama-quantize` to the requested
  level; MLX similarly. Derived weights go in
  `<vault>/.runtime/derived/<bundle_hash12>/<recipe>/`, content-keyed like
  shared envs. `model/` is never written to; deleting `.runtime/` loses
  nothing (payload immutability + hydration-is-disposable both hold).

`hydration.json` records the recipe with full provenance: recipe name and
parameters, tool versions, input bundle hash, output file hashes, wall-time.
Recorded facts, not claims — a derived Q4_K_M is *a* Q4_K_M under the
recorded toolchain, not bit-identical to any published one. Runs against
derived weights stay offline (`HF_HUB_OFFLINE=1`); derivation itself needs
no network either, since the input is the archived payload.

### Decision guide

| You want | Do |
|---|---|
| Preserve the model | `darsay archive <repo>` — the negatives are the default (55.6 GB, not the 239.7 GB shipping box) |
| Preserve every published byte of the repo | `darsay archive <repo> --full` |
| Preserve what people actually ran | Also archive that published print as a satellite bundle (FP8: +30.9 GB; one GGUF via `archive --include`: +16 GB) |
| Understand a size before deciding | `darsay estimate <repo>` — parameters, precision, bytes per parameter |
| See what is irreplaceable before deciding | `darsay classify <repo>` |
| Just run it smaller/faster locally | Proposed `hydrate --quantize <recipe>` — disposable, regenerable, zero archival cost |

## See also

- [North star](NORTH-STAR.md) — why negatives, precision, and lineage are
  the model of the models.
- [Concepts](CONCEPTS.md#negatives-and-prints) — the vocabulary in one
  page; [lineage](CONCEPTS.md#lineage) for families and generations.
- [HYDRATION.md](HYDRATION.md) — the env/runner machinery that `--quantize`
  recipes would extend (`ENGINES` registry, `hydration.json`, offline runs).
- [MANIFEST.md](MANIFEST.md) — `lineage` (parents and descendants at
  archive time), `model_metadata.precision`, `source.subset` for the
  recorded selection.
- The project [README](../README.md) three-verb loop and
  [examples: the negatives by default](../examples/README.md#archive-the-negatives-the-default).

---

[Documentation index](README.md)

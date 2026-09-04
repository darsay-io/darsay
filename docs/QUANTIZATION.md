<p align="center">
  <a href="GETTING-STARTED.md">Start here</a> ·
  <a href="CONCEPTS.md">Concepts</a> ·
  <a href="../examples/README.md">Examples</a> ·
  <a href="README.md">All docs</a> ·
  <a href="../README.md">README</a>
</p>

# Collection scope, preservation evidence, and precision

> **In one sentence.** Choose the release or variants you mean to collect,
> retain their published bytes conservatively, and require evidence for
> every automatic omission.

How darsay treats a model that exists in many precisions: what a size
includes, what lineage establishes, and what justifies retaining or
omitting an artifact. A usable derivative and the release it descends
from are both legitimate collection targets.

**This policy is mechanized.** `darsay archive` applies it by default
([§4](#4-mechanics)); `darsay classify` shows the per-set evidence;
`estimate` names the size basis and lists GGUF variants; `--include`
pins an explicit subset; `--full` fetches everything.

An interactive fresh GGUF-pack archive first offers the
[collection room](COLLECTIONS.md). Its starting points are editable scope
choices, separate from classification. Explicit includes, full publications,
board/catalog jobs, and unattended runs keep their specified archive policy.

## 1. Negatives and prints

The photography analogy is useful for talking about ancestry, but it is
not sufficient for deciding what to delete from an archive. A derivative
can be a distinct historical artifact, and a published low-precision
release can be the publisher's release of record.

The classifier uses **negative** for a preservation candidate retained by
a conservative rule, **print** for an established duplicate or derivative
relationship, and **unknown** when its evidence does not settle the
classification. These labels do not prove originality, irrecoverability,
or safe omission. The recorded **action** answers whether a set is fetched.

Headers, indexes, hashes, and upstream declarations establish different
facts. BF16 says how weights are encoded, not what precision they were
trained in or whether they contain more information than an FP8 source.
An importance matrix does not prove a private calibration corpus; no
importance-matrix header does not prove regeneration is possible. A
declared parent is not a recovery recipe.

Start with four questions: **what scope am I collecting; what are its
artifacts and lineage; what recovery evidence exists; what will be
retained?** An explicit subset is a curator's choice to collect less,
not a finding that everything outside it can be recreated. The
[North star](NORTH-STAR.md) applies this distinction across the product.

## 2. Precision and size scope

For one complete model weight variant, **parameters × bytes per
parameter** explains the weight size. The precision determines the
approximate storage per weight, with extra bytes for scales and metadata:

| Release precision | bytes / parameter | 27.78B weighs | 2.45T weighs |
|---|---|---|---|
| F32 | 4 | 103 GiB | 8.9 TiB |
| BF16 · F16 | 2 | 51.7 GiB | 4.4 TiB |
| FP8 · INT8 | 1 | 25.9 GiB | 2.2 TiB |
| MXFP4 · NVFP4 · INT4 (packed two per byte, plus scales) | ≈ 0.5–0.6 | 13–16 GiB | 1.2–1.4 TiB |

A repository total can include many complete copies at different
precisions, projectors, and other support files. An archive can retain
several of those variants. Neither total is necessarily one model's
weight size.

`darsay estimate` reports precision separately from preservation:

```
  parameters:   2.78T (2.72T U8 (packed), 57.18B BF16, 11.1M F32)  [upstream safetensors metadata]
  precision:    MXFP4 (compressed-tensors · mxfp4-pack-quantized · 4-bit · group 32) — 0.56 B/param: about half a byte per weight — a 4-bit release
```

The label comes from `config.json`'s `quantization_config` (wherever a
multimodal config nests it), else the dominant safetensors dtype, else —
when GGUF is all a repo ships — the quant level in the file name
(`Q4_K_M`). Bytes per parameter is *measured*: one complete variant's
model weight bytes over the published total parameter count. Mixture of
experts models use total parameters, including inactive experts, because
all their weights occupy storage. GGUF estimates use the Hub's
`gguf.total` when safetensors metadata is absent, and record that source.
Whole GGUF packs, incomplete shard selections, and projector-only
selections have no single-model bytes-per-parameter or RAM estimate.

Every price carries a `size_basis`: `repository` for an upstream
inventory, `selection` for a chosen subset, or `archive` for the files
retained by classification. The classification summary separately counts
negative, print, and unknown sets. Unknown sizes make a known-byte sum a
lower bound. A retained archive can still be a large multi-variant pack;
classification does not choose one preferred precision for the collector.

### A GGUF pack in practice

At revision `2975ab414d30340466d8c51533c6e91f0cca64c1`,
[`unsloth/GLM-5.3-Flash-GGUF`](https://huggingface.co/unsloth/GLM-5.3-Flash-GGUF/tree/2975ab414d30340466d8c51533c6e91f0cca64c1)
contains **2,545,636,747,545 bytes (2.315 TiB)** across twelve model
variants and supporting files. The model has 320,759,404,382 total
parameters. Its BF16 variant totals **641,641,064,192 bytes (597.6 GiB)**
across fourteen shards; UD-Q4_K_XL totals **199,707,321,347 bytes
(186.0 GiB)** across six. The repository total is correct, but calling it
one model's size or a quantity of confirmed negatives is not. The default
archive retains all **87 files** at this pin: **70 weight files** and
**17 support files**, with no automatic omissions. Bounded classification
leaves three weight sets unresolved; those bytes are included, not zeroed.

The publisher's own
[`zai-org/GLM-5.3-Flash`](https://huggingface.co/zai-org/GLM-5.3-Flash/tree/690b705278a3a58e538fcb37c2ca8b5f9511213c)
release at revision `690b705278a3a58e538fcb37c2ca8b5f9511213c` uses FP8
quantization. It contains **328,366,172,624 bytes (305.8 GiB)** across
72 files: 62 weight shards and ten support files. Its upstream parameter
count is 321,323,031,390. It is a different collection target from the
Unsloth pack, not a smaller subset of that repository.

| Collection target | Archive bytes at these pins |
|---|---:|
| Entire Unsloth pack, including support and projectors | 2,545,636,747,545 B · 2.315 TiB |
| Z.ai's published FP8 release, including support | 328,366,172,624 B · 305.8 GiB |
| Unsloth UD-Q4_K_XL selection, six shards plus README | 199,707,329,724 B · 186.0 GiB |

Choose the publisher release to collect that release, the pack to
collect all those published variants, or a subset for one intended
variant. There is no universal “negative size.” The larger BF16 encoding
does not by itself establish a more original or more faithful source.

GGUF variant sizes sum every shard. The first shard can be a small
metadata file and does not represent the complete variant. `mmproj`
projectors are companion files with separate storage needs; they are not
additional variants or part of the model parameter denominator.

## 3. Policy

1. **Choose the collection target.** A publisher release, a community
   derivative, and a multi-variant pack answer different collection
   goals. Prefer provenance over a “largest precision wins” rule. The
   board must not silently replace one source with another.
2. **Retain conservatively by default.** `darsay archive <model>` fetches
   negatives, unknowns, all support, and prints without a retained
   same-bundle twin. Only non-GGUF weight sets whose every file has a
   hash-identical twin in a retained set are automatically omitted. Their content remains;
   their duplicate paths do not. `--full` keeps every published file and
   layout. Classification chooses no preferred precision.
3. **Make a smaller scope explicit.** `--include` selects variants and
   documented sidecars; include required companions too. It records what
   was omitted without claiming a complete model collection. Published
   derivatives are ordinary bundles with declared lineage, not disposable
   runtime caches merely because another model is their ancestor.
4. **Distinguish exact preservation from functional recreation.** A
   runnable quantization is not necessarily the published quantization.
   Reconstruction-based omission would require retained inputs and a
   verified recovery receipt. Hydration-time quantization is proposed
   below; it is not an implemented recovery guarantee.

## 4. Mechanics

### Implemented: conservative retention (`archive`) and `darsay classify`

A fresh `darsay archive <model>` classifies the repo's weight files from
metadata plus **bounded header reads** — `config.json`, every
`*.index.json` (up to 64 MiB; a frontier MoE weight map runs to 60 MB),
GGUF key/value tables, safetensors headers of files no index accounts
for, with every cap recorded — and retains every artifact except
established same-bundle duplicates. Verdicts come from a closed enum:

- **negative** — a retained preservation candidate: the index-selected or standalone
  floating-point weights, quantized safetensors (calibration or a curated
  layer map may be baked in — a native MXFP4 release lands here), GGUFs
  carrying `quantize.imatrix.*`, the only weight format in a repo.
- **print** — an established duplicate relationship. A weight set matching
  the declared base's upstream SHA-256 inventory is **retained**: the
  remote match does not establish recoverable local bytes, even when
  another revision of that base is in the vault. A weight set whose entire
  file-hash multiset matches a retained twin in this same bundle is skipped
  under R15; this does not prove a complete model is present upstream.
- **support** — config, tokenizer (including tiktoken vocabularies and
  the custom Python a `custom_code` model needs), licenses, provenance
  records: always fetched.
- **unknown** — darsay will not guess, so the files are **fetched**:
  orphaned weight sets no index selects, GGUFs whose source build cannot
  be established (even a sole colocated candidate, or a header naming an external
  source), legacy `.bin`/`.pt` beside safetensors, anything unreadable,
  capped, or gated.

Every skip is named in the preflight with its rule and reason, recorded
in the manifest (`source.subset.policy: "negatives"` plus the per-set
classification, with the full upstream inventory and hashes), and frozen
into the pin — re-runs resume the selection without re-reading a header;
`--force` re-pins under current rules. `--full` fetches the whole repo;
`--include` remains the curator's explicit override. Any classification
failure degrades to the full fetch, never the other way.

**The stated promise.** Every automatically skipped weight file has
hash-identical content in a retained same-bundle weight set. This saves
duplicate content, not every upstream pathname or runnable pipeline
layout. The full inventory records the omitted paths and hashes; use
`--full` to preserve all layouts. No automatic skip relies on an assumed
GGUF conversion, a missing imatrix key, or another base revision's presence.
The derived selection is checked against **all** retained paths, including
support files outside the ordinary sidecar list.

`darsay classify <source>` is the audit view: the per-set verdict table
with rule ids, evidence, the read receipt, and the exact selection —
`--json` for scripts. The rules, R1–R15, are recorded in
[the classify proposal](proposals/classify.md).

### Implemented: preflight sizing (`darsay estimate`)

No payload downloads, nothing written; a fresh model source is
classified the same way `archive` classifies it, so the price quoted is
the price paid:

    darsay estimate Qwen/Qwen3.8-27B                 # the archive selection, priced
    darsay estimate Qwen/Qwen3.8-27B --full          # the whole repo, priced
    darsay estimate Qwen/Qwen3.8-27B --variants      # + quantized ecosystem, sized
    darsay estimate unsloth/Qwen3.8-27B-GGUF --include '*Q4_K_M*'   # one quant of a pack

The `archive:` line says what the price is made of — `1 negative set
(29 files, 51.7 GiB) + 9 sets darsay will not guess about (171.5 GiB),
fetched` — or that prints were skipped and by how much. The `precision:`
line names the release precision and, for one complete variant, measured
bytes per parameter ([§2](#2-precision-and-size-scope)); `family:`
and `lineage:` place the work ([Concepts → lineage](CONCEPTS.md#lineage)).
Exact numbers (file sizes, parameter counts by dtype) come from upstream
metadata; derived numbers (RAM x1.2, download scratch) are labeled
estimates. The `payload:` line names its size basis; GGUF variant totals
include all shards and flag incomplete sets. Every cap is recorded
(`query_limit`, the read receipt).

### Implemented: subset archiving (`archive --include`)

Pack repos (one repo, 27 quant files) make "bundle = whole repo" the wrong
unit: archiving one 16 GB Q4_K_M shouldn't cost 430 GB.

```bash
darsay estimate unsloth/GLM-5.3-Flash-GGUF --include '/UD-Q4_K_XL/GLM-5.3-Flash-UD-Q4_K_XL-*-of-00006.gguf'
darsay archive  unsloth/GLM-5.3-Flash-GGUF --include '/UD-Q4_K_XL/GLM-5.3-Flash-UD-Q4_K_XL-*-of-00006.gguf'
```

`--include` is a glob, repeatable; a leading `/` anchors it to the repo
root and disables the filename fallback. Matching files are kept, plus
sidecar files (config, tokenizer, license, card, custom code) so a single
GGUF has its supporting metadata. Select any needed model companions,
such as a multimodal projector, explicitly as well. GGUF variant
inventories provide exact selectors for all shards of each variant and
remain visible even when pricing a selection. The pin is the subset:
later reruns without `--include` resume that pin rather than expanding it
(`--force` re-pins). The manifest
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
This is a proposed record, not a shipped feature. A derived Q4_K_M is
*a* Q4_K_M under the
recorded toolchain, not bit-identical to any published one. Runs against
derived weights stay offline (`HF_HUB_OFFLINE=1`); derivation itself needs
no network either, since the input is the archived payload.

This functional runtime recipe would not authorize dropping a published
artifact. That requires a separate byte-exact recovery check: pinned input
hashes, retained calibration and toolchain inputs, the recipe, and verified
output hashes, with the dependency closure portable through export.

### Decision guide

| You want | Do |
|---|---|
| Collect a release conservatively | Choose its repository, then `darsay archive <repo>`; a pack can retain many variants |
| Preserve every published byte of the repo | `darsay archive <repo> --full` |
| Preserve what people actually ran | Archive that published derivative; use `--include` for chosen variants and required companions |
| Understand a size before deciding | `darsay estimate <repo>` — parameters, precision, bytes per parameter |
| Inspect preservation evidence before deciding | `darsay classify <repo>` — verdicts, reasons, and separate actions |
| Run one published smaller variant | Price and archive an explicit `--include` selection; verify runtime support and required companions |

## See also

- [North star](NORTH-STAR.md) — collection scope, artifact identity,
  recovery evidence, and retention decisions.
- [Concepts](CONCEPTS.md#negatives-and-prints) — the vocabulary in one
  page; [lineage](CONCEPTS.md#lineage) for families and generations.
- [HYDRATION.md](HYDRATION.md) — the env/runner machinery that `--quantize`
  recipes would extend (`ENGINES` registry, `hydration.json`, offline runs).
- [MANIFEST.md](MANIFEST.md) — `lineage` (parents and descendants at
  archive time), `model_metadata.precision`, `source.subset` for the
  recorded selection.
- The project [README](../README.md) three-verb loop and
  [examples: conservative retention](../examples/README.md#archive-conservatively-the-default).

---

[Documentation index](README.md)

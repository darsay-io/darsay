<p align="center">
  <a href="GETTING-STARTED.md">Start here</a> ·
  <a href="CONCEPTS.md">Concepts</a> ·
  <a href="../examples/README.md">Examples</a> ·
  <a href="README.md">All docs</a> ·
  <a href="../README.md">README</a>
</p>

# Quantization policy — what gets archived, what gets derived

> **In one sentence.** Archive the highest-fidelity release. Archive
> published quants that matter historically. Derive the rest at run time.

How darsay treats a model that ships in many precisions: which artifact
is the canonical bundle, when a quantized repo deserves archiving in its own
right, and how "I want to run it at 4 bits" is served without archiving
anything extra.

`estimate --include` and `archive --include` pin a subset. Hydration-time
`--quantize` is proposed. Case study: Qwen/Qwen3.8-27B.

## 1. The two kinds of quantized artifact

A quantized model is one of two very different things, and the design hinges
on not conflating them:

**Published quants** are upstream repos in their own right —
`Qwen/Qwen3.8-27B-FP8`, `unsloth/Qwen3.8-27B-GGUF`, `bartowski/…-GGUF`,
`mlx-community/…-4bit`. Most are **not reproducible from the master
weights**: AWQ/GPTQ need calibration data and a GPU quantization run;
imatrix GGUFs bake in an importance matrix computed on a private corpus
(unsloth's repo ships `imatrix_unsloth.gguf` alongside its quants); Qwen's
official FP8 is mixed-precision (3.08B params kept BF16, 24.7B in F8_E4M3 —
a curated layer map, not a mechanical cast). These are historical artifacts:
if it matters what people actually ran, the bytes must be archived, because
no future toolchain will regenerate them bit-exact.

**Derived quants** are mechanical transformations of archived weights —
`convert_hf_to_gguf.py` + `llama-quantize`, `mlx_lm.convert --quantize`,
bitsandbytes/torchao load-time quantization. Given the payload plus a
recorded recipe (tool, version, parameters) they are regenerable, so they
are **cache, not archive**.

## 2. Policy

1. **Fidelity first.** The canonical bundle for a model is the
   highest-fidelity upstream release (the BF16/FP16 safetensors repo),
   archived byte-exact. Never quantize *before* archiving; the master is the
   negative, quants are prints.
2. **Published quants worth keeping are satellite bundles.** They are
   ordinary HF repos, so the existing pipeline already handles them: archive
   the repo, and `relationships.base_model` / the master bundle's
   `relationships.quantized_versions` snapshot link the two. No special
   casing. Archive a published quant when it is historically significant
   (the official FP8, the community-standard bartowski/unsloth GGUF) or when
   it's the only form that runs on hardware you care about.
3. **Everything else is hydration-time derivation.** Wanting to *run* the
   model at 4 bits is not a reason to archive a 4-bit repo. Derived weights
   are runtime state: they live outside bundles, are recorded with full
   provenance, and can be deleted and rebuilt at will.

## 3. Why not "archive the quant, skip the 55 GB master"?

The master is the only artifact every downstream form can be derived from,
the only one eval-comparable across engines, and the only one future
formats (the GGUF/NVFP4 of 2032) can be built from. The satellite quants
answer "what did people run in 2026"; the master answers everything else.
When disk forces a choice, keep the master.

Numbers for Qwen3.8-27B (27.78B params): master BF16 55.6 GB; official FP8
30.9 GB; unsloth GGUF pack ~430 GB across ~27 quant levels (6.2 GB IQ1_S to
55.6 GB BF16 — a wholesale archive of a pack repo is usually the wrong
unit; see `--include` below).

## 4. Mechanics

### Implemented: preflight sizing (`darsay estimate`)

Read-only against the Hub API — no download, nothing written:

    darsay estimate Qwen/Qwen3.8-27B                 # size/params/disk for the master
    darsay estimate Qwen/Qwen3.8-27B --variants      # + quantized ecosystem, sized
    darsay estimate unsloth/Qwen3.8-27B-GGUF --include '*Q4_K_M*'   # one quant of a pack

Exact numbers (file sizes, param counts by dtype) come from upstream
metadata; derived numbers (RAM x1.2, download scratch) are labeled
estimates. Variant listing caps are recorded (`query_limit`,
`detail_limit`), matching the manifest's record-don't-fabricate rule.

### Implemented: subset archiving (`archive --include`)

Pack repos (one repo, 27 quant files) make "bundle = whole repo" the wrong
unit: archiving one 16 GB Q4_K_M shouldn't cost 430 GB.

```bash
darsay estimate unsloth/Qwen3.8-27B-GGUF --include '*Q4_K_M*'
darsay archive  unsloth/Qwen3.8-27B-GGUF --include '*Q4_K_M*'
```

`--include` is a glob, repeatable. Matching files are kept, plus sidecar
files (config, tokenizer, license, card) so a single GGUF still loads.
The pin is the subset: later reruns without `--include` resume that pin
rather than expanding it (`--force` re-pins). Completeness rules already
pass for a single GGUF (`model/*.gguf` satisfies config/weights/tokenizer).

The manifest records the subset honestly — `source.subset` holds the
include patterns **and the full upstream file list with sizes/hashes**,
so the bundle states exactly what it deliberately does not contain.

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
| Preserve the model | Archive the highest-fidelity repo (55.6 GB) |
| Preserve what people actually ran | Also archive that published quant as a satellite bundle (FP8: +30.9 GB; one GGUF via `archive --include`: +16 GB) |
| Just run it smaller/faster locally | Proposed `hydrate --quantize <recipe>` — disposable, regenerable, zero archival cost |

## See also

- [HYDRATION.md](HYDRATION.md) — the env/runner machinery that `--quantize`
  recipes would extend (`ENGINES` registry, `hydration.json`, offline runs).
- [MANIFEST.md](MANIFEST.md) — `relationships.quantized_versions` /
  `gguf_repos`, the archive-time ecosystem snapshot that links master and
  satellite bundles; `source.subset` for `archive --include`.
- The project [README](../README.md) three-verb loop and
  [examples: price one quant](../examples/README.md#price-one-quant-from-a-pack-repo).

---

[Documentation index](README.md)

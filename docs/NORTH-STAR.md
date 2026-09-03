<p align="center">
  <a href="GETTING-STARTED.md">Start here</a> ·
  <a href="CONCEPTS.md">Concepts</a> ·
  <a href="../examples/README.md">Examples</a> ·
  <a href="README.md">All docs</a> ·
  <a href="../README.md">README</a>
</p>

# North star

> **In one sentence.** darsay lets a person collect models that are
> museum-grade *and* still plug into an inference system tomorrow — and
> makes that person smarter about AI every time they use it.

This page is the measure every other page, feature, and field is held
against. When two designs both work, the one that serves this page wins.

## The mission

Models are the artifacts of this decade. They are published as living
websites: renamed, gated, rewritten, deleted. A collector needs three
things at once, and the tools built for one of them fail the others:

1. **Museum grade.** Byte-exact, pinned, hashed, licensed, documented well
   enough that a reader in 2040 can open the record without the tool.
2. **A functional brick.** The archived payload is a pristine snapshot any
   loader already understands. It hydrates into a runtime, plugs into an
   agent, answers a prompt — offline, as proof.
3. **Understanding.** The collector should come away knowing what a model
   *is*: what it weighs and why, what it descends from, what nothing can
   regenerate, what is only a cast of something else.

The third is not decoration. A collection you do not understand is a pile
of bytes. darsay's job is to turn the act of collecting into the act of
learning — every number explains itself, every label teaches its meaning,
and the model of the models is clear enough to browse.

## The model of the models

Five words carry the whole system. Every surface — the CLI, the manifest,
a catalog, a darsay.io board — uses the same five with the same meaning.

**Work.** A model as a thing in the world: `Qwen3.8-2.4T-A95B`. A catalog
row wants a work; a bundle holds one pinned release of it; a work with no
downloadable release yet (an API-only model) still has a place, as a
**closed** work with a home page and no bytes.

**Negative and print.** Photography had the words first. A *negative* is
the original exposure — the weights nothing can regenerate: the
full-fidelity release, a quant with calibration baked in, the only form a
repo ships. A *print* is a mechanical transformation of a negative — a
plain `llama-quantize` GGUF, a byte-identical twin. darsay archives the
negatives by default and leaves prints as cache, loudly and on the record.
There is no third word.

**Precision.** The release precision of a work's weights — `BF16`, `FP8`,
`MXFP4`, `Q4_K_M` — and with it **bytes per parameter**, the one number
that explains every size. A 2.4T-parameter BF16 release weighs 4.4 TiB
because sixteen bits is two bytes; a 2.8T-parameter native MXFP4 release
weighs 1.4 TiB because four bits plus a scale is about half a byte. Neither
is "bigger"; they are exposed at different precisions, and a native low
precision release is still the negative — there is no higher-fidelity copy
to prefer.

**Lineage.** Works come in **families** (`Qwen`, `Kimi`, `DeepSeek`),
families in **generations** (`3`, `3.5`, `3.8`; `K2`, `K2.5`, `K3`), and
generations have **members** (`27B`, `Flash-Next`, `2.4T-A95B`, `Max`).
Around each member hang its **variants** (`Base`, `Instruct`,
`Abliterated`) and its **prints** (`GGUF`, `FP8`). Family, generation, and
member are read from the publisher's name for the work by a documented
grammar and labeled as such; parent edges (finetune, adapter, merge,
quantized) are recorded as upstream declares them; successors are the
next generation in the same family. The tree is browsable, and a closed
work sits in it beside its open siblings.

**Vault, bundle, pin, catalog.** The mechanics — named in
[Concepts](CONCEPTS.md) — and unchanged by any of the above.

## Principles

**Record, don't fabricate.** A fact comes from upstream or from the
payload, or it is `null`. Anything derived — bytes per parameter, a family
read from a name, a RAM estimate — says what it was derived from.

**One price, one meaning.** A size is what `archive` will fetch: the
negative set. The shipping box is context, shown beside it, never quoted
as the price.

**Every label is a doorway.** A chip on a board row, a column in `list`, a
line in `estimate` — each names a concept from the model above and opens
onto its explanation applied to that row. Nothing is shown that cannot be
explained in one card.

**Names are evidence, not verdicts.** Family and variant are read from the
repo name and say so. Whether bytes are a negative or a print is decided
from headers and hashes, never from a name.

**The formats outlive the tool.** `manifest.json`, `catalog.json`, and
`.mvb.tar` are plain, documented, and sufficient without darsay — and
`SHA256SUMS` lets `sha256sum` alone check a payload against its record.

**Fix forward.** darsay is greenfield. When the model of the models
improves, the schema follows it — no compatibility knobs, no fields kept
to describe how things used to be. A record written under the old model
is not read around; `darsay migrate` re-reads its facts under the new
one and writes them in the new shape, and the payload it describes never
changes. Breaking changes are named in the changelog with their recovery.

## What "smarter" looks like

A collector who adds `Qwen/Qwen3.8-2.4T-A95B` to a board should see, on
the row, that it is 2.45T parameters at BF16, two bytes each, 4.4 TiB —
one negative set, nothing to skip — and that it is the flagship member of
Qwen generation 3.8, beside the 27B and Flash-Next members they already
listed, three generations after the Qwen3 8B base they keep as an
experimental organism. When they add `moonshotai/Kimi-K3`, they should
see 2.78T parameters at MXFP4, half a byte each, 1.4 TiB — and understand
in one glance why a model with *more* parameters costs a third of the
disk. When Qwen publishes the weights of `Qwen3.8-Max`, the closed row
they kept for it should become an open one without losing its place in
the tree.

That is the bar. A feature that moves toward it belongs in darsay.

---

[Documentation index](README.md)

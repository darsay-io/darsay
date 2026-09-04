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
2. **A functional brick.** Retained files stay in their published formats,
   with the paths and companions the intended loader needs. A supported
   selection hydrates into a runtime, plugs into an agent, and answers a
   prompt — offline, as proof. Exact hashes establish preservation; an
   offline run establishes usability. Neither test substitutes for the other.
3. **Understanding.** The collector should come away knowing what a model
   *is*: what the collection includes, what it weighs and why, what it
   descends from, and what evidence supports keeping or omitting a file.

The third is not decoration. A collection you do not understand is a pile
of bytes. darsay's job is to turn the act of collecting into the act of
learning — every number explains itself, every label teaches its meaning,
and the model of the models is clear enough to browse.

## The model of the models

Every surface — the CLI, the manifest, a catalog, a darsay.io board —
uses the same vocabulary. Four questions organize the preservation decision:

| Question | What answers it |
|---|---|
| What do I intend to collect? | A named publisher release, repository, or explicit selection at a pinned revision. |
| What are these artifacts? | File identities, complete weight variants, precision, and declared lineage. |
| What can I recover without storing another copy? | Evidence about retained content and reconstruction, not format or ancestry alone. |
| What will this archive actually contain? | Per-set retain/omit decisions, the selected inventory, and its byte total. |

A smaller collection is a valid choice, not proof that its omitted
artifacts were disposable. Having one variant is not preserving every
release of a work.

**Work.** A model as a thing in the world: `Qwen3.8-2.4T-A95B`. A catalog
row wants a work; a bundle holds one pinned release of it; a work with no
downloadable release yet (an API-only model) still has a place, as a
**closed** work with a home page and no bytes.

**Negative and print.** These are teaching shorthand, not a complete
theory of preservation. The classifier's *negative* means a preservation
candidate retained by a conservative rule, not proven original or
impossible to reproduce. A *print* records an established duplicate or
derivative relationship; that relationship alone does not justify
omission. A derived release can be historically important in its own
right. Unresolved evidence is *unknown* and retained. Automatic omission
requires the same complete content to remain in this bundle; a base
repository's presence elsewhere in a vault is not enough.

**Precision.** The release precision of a work's weights — `BF16`, `FP8`,
`MXFP4`, `Q4_K_M` — and with it **bytes per parameter** for one complete
model weight variant. A BF16 weight takes two bytes; a packed four-bit
weight takes about half a byte plus scales. A repository may contain a
dozen variants of the same model, with several shards per variant. Its
total describes storage across those variants, not the size or RAM needs
of one model. Precision alone cannot establish whether a release is an
original or a derivative.

**Lineage.** Works come in **families** (`Qwen`, `Kimi`, `DeepSeek`),
families in **generations** (`3`, `3.5`, `3.8`; `K2`, `K2.5`, `K3`), and
generations have **members** (`27B`, `Flash-Next`, `2.4T-A95B`, `Max`).
Around each member hang its **variants** (`Base`, `Instruct`,
`Abliterated`) and its **published formats** (`GGUF`, `FP8`). These labels
do not establish a preservation verdict. Family, generation, and
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

**Every size names its scope.** A repository size totals the upstream
inventory; a selection size totals the selected files and support; an
archive size totals what the preservation decision retains. Archive bytes
can include retained prints, unknowns, and several model variants. Negative, print, and
unknown counts explain the decision separately. Missing sizes make a
total a lower bound, never an exact price.

**Every label is a doorway.** A chip on a board row, a column in `list`, a
line in `estimate` — each names a concept from the model above and opens
onto its explanation applied to that row. Nothing is shown that cannot be
explained in one card.

**Guidance is an invitation, not a verdict.** The collection room offers
explicit starting points and lets the collector change them. It names the
rule behind each starting point, shows the resulting pinned scope, and
separates measured storage from unmeasured quality, hardware fit, and
recreation cost. The board and terminal teach the same distinctions. No
payload moves merely because a person opened a chooser. See
[Choose your collection](COLLECTIONS.md).

**Names are evidence, not verdicts.** Family and variant are read from the
repo name and say so. Whether bytes are a negative or a print is decided
from recorded evidence, never from a name. A BF16 encoding need not be
the publisher's original release or contain more information than an FP8
source. Calibration metadata does not establish that its inputs were
private; absent calibration metadata does not establish reproducibility.

**Recovery has a standard of proof.** Byte-exact reconstruction and a
functionally similar model are different outcomes. A recipe alone proves
neither. Today automatic omission is limited to hash-identical weight
sets with a retained twin in the same bundle. Duplicate paths are absent
from that selection; use `--full` when every published layout matters.

Future reconstruction-based omission must carry verifiable recovery
receipts: pinned input hashes, retained calibration and toolchain inputs,
the recipe, and checked output hashes. Its dependencies must survive
export and recovery without the original vault. Until that exists,
published derivatives stay in scope unless the collector chooses a
narrower selection. Hydration-time quantization is proposed, not an
implemented substitute for collecting a published release.

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

Adding a GGUF pack should be equally clear: the row names the repository
total, lists each complete variant with all its shards, and lets the
collector choose the intended variant. A 2.3 TiB repository containing
twelve variants of a 321B-parameter model must not look like one 2.3 TiB
model, or imply that all those bytes are confirmed negatives.

For GLM-5.3-Flash, the pinned Unsloth pack is **2.315 TiB**, Z.ai's own
FP8 release is **305.8 GiB**, and one Unsloth UD-Q4_K_XL selection is
**186.0 GiB** before an optional projector. These are different collection
targets, not three answers to “how big is the negative?” The
[pinned case study](QUANTIZATION.md#a-gguf-pack-in-practice) gives the
inventories and exact totals. A board should explain the choices without
silently replacing the source the collector chose.

That is the bar. A feature that moves toward it belongs in darsay.

---

[Documentation index](README.md)

# Lineage and precision — the model of the models

| | |
|---|---|
| **Author** | Jeremy Norris (drafted with Claude) |
| **Date** | 2026-09-02 |
| **Status** | Implemented — manifest schema **2.1.0**, catalog schema **3.0.0**. Shared by the CLI, darsay.io board, and worker. Sets the vocabulary in [NORTH-STAR.md](../NORTH-STAR.md). |
| **Audience** | darsay CLI and darsay.io implementers; readers of `docs/CONCEPTS.md` |
| **Related** | [classify.md](classify.md) (preservation evidence and archive selection) · [darsay-io.md](darsay-io.md) (boards) |

---

## The question that started it

A board row for `huggingface:Qwen/Qwen3.8-2.4T-A95B` read 4.4 TiB, and a
row for `moonshotai/Kimi-K3`, a model with a similar parameter count, read
1.4 TiB. Two questions followed: *is 4.4 TiB really the negative set?* and
*why the difference?* Both answers were established from upstream in
minutes, and neither was visible anywhere in darsay:

| | parameters | release precision | bytes / parameter | size |
|---|---|---|---|---|
| Qwen3.8-2.4T-A95B | 2.45T | BF16 | 2.00 | 4.4 TiB — one indexed weight set, nothing to skip |
| Kimi-K3 | 2.78T | MXFP4, group 32 (compressed-tensors) | 0.56 | 1.4 TiB — a published 4-bit weight set |

These single-set examples do not generalize to repository totals.
A GGUF pack may contain several precisions and shard groups, so its
inventory size, a selected variant's size, and a classified archive's
size must remain distinct. A negative/print verdict answers a separate
preservation question.

## Decisions

1. **Scope and evidence precede the metaphor.** Preservation verdicts are
   `negative | print | support | unknown`; the manifest names the default
   acquisition policy `negatives`. Catalog sizes instead carry
   `size_basis` (`repository | selection | archive`) and a separate
   classification summary. An archive can retain unknowns, support,
   multiple variants, and prints. Negative means retained by a conservative
   rule, not proven original or irrecoverable. Print describes a relationship,
   not permission to omit. Only weight sets whose every file has a
   hash-identical retained same-bundle twin are omitted automatically. Collection scope,
   artifact identity and lineage, recovery evidence, and the resulting
   retention decision are separate questions.

2. **Precision and size scope are recorded separately.** `precision.py`
   names the release precision from
   `config.json` (`quantization_config`, wherever a multimodal config
   nests it), the dominant safetensors dtype, or — only when GGUF is all a
   repo ships — the quant level in the file name. Bytes per parameter is
   measured for one complete model weight variant against the published
   total parameter count, including inactive experts. Whole GGUF packs,
   incomplete shard selections, and projector-only selections have no
   single-model bytes-per-parameter or RAM estimate. Precision alone
   cannot establish whether a release is original. These facts travel:
   `estimate` prints them, the catalog digest stores them (`precision`,
   `bytes_per_param`), the board shows them on the row, the manifest's
   `model_metadata` records them for the archived payload.

3. **Lineage is a model, read from names and declarations, never
   guessed.** `lineage.py` reads *family · generation · member · variants
   · formats · size* from a work's name by a documented grammar shared
   with the site (`website/src/lib/lineage.ts`, one fixture table on both
   sides), and labels the result `read_from: "name"`. Parent edges come
   from upstream declarations (card `base_model`, `base_model:<relation>`
   tags, card `datasets` as `trained_on`) with their provenance. The
   manifest gains `identity.{family, generation, member, variants,
   formats, size}` and a `lineage` section (`parents`, `descendants`,
   curator `successors` / `related`); the provider protocol exposes
   `lineage()`. Declared ancestry is not a reconstruction guarantee.

4. **Closed works have a place.** A catalog entry's `source` may be a home
   URL on a host with no provider — an API-only model, an announced
   release. It is a `closed` row: no price, nothing to fetch, its place in
   the family held by the name grammar (`qwen3.8-max-0902` sits in Qwen
   3.8 beside `Qwen3.8-2.4T-A95B`). When weights ship, the address becomes
   a source ref.

5. **Boards browse the tree.** Family lenses (one chip per family on the
   board), a lineage view (families → generations → members, derivatives
   nested under the parent they declare, closed works dashed), and three
   field-guide cards: collection scope and preservation evidence, precision
   and bytes per parameter, families and generations, closed weights.

6. **Fix forward.** Every reader uses the current schema: manifest 2.1.0
   and catalog 3.0.0. Catalog digests explicitly name their size basis,
   classification, whole-repository bytes, parameter source, and GGUF
   variant inventory. The record carries these facts rather than asking
   readers to infer what a size meant.

7. **Kimi K3 reads correctly.** The JSON read cap rises to 64 MiB (the
   GGUF header cap), tiktoken vocabularies satisfy the tokenizer rule, and
   custom-code Python rides along as a sidecar of any subset.

## Surfaces

| Surface | Change |
|---|---|
| `darsay estimate` | `archive:` explains retention; `payload:` names the size basis; `precision:` names the label and, for one complete variant, bytes/param; GGUF choices sum every shard; `family:` / `architecture:` / `lineage:` place the work; parameter counts name their source. |
| `darsay classify` | Per-set negative/print/unknown/support verdicts with evidence and separate fetch/skip actions; complete GGUF shard groups are judged together. |
| `darsay list` | FAMILY and PRECISION columns; `--sort family` reads the tree; `closed` status. |
| `darsay catalog add` | Accepts a home URL as a closed work; refuses `--revision` / `--include` on one. |
| Catalog README | Precision column; a **Families** section. |
| Manifest 2.1.0 | `identity` name-derived fields; `lineage` section; `model_metadata.precision` label, `precision_detail`, `bytes_per_param`. |
| Catalog 3.0.0 | Digest `size_basis`, `classification`, `repository_bytes`, `gguf_variants`, `parameters_source`, `precision`, `bytes_per_param`, `architecture`, `parents`; home URLs; `closed`. |
| darsay.io | Worker stores home rows and the new digest keys, reads `config.json` and `base_model` tags on add; board shows precision and bytes/param chips, family lenses, a lineage view, closed rows; field guide gains three cards. |

## Not done, deliberately

- Curator-authored lineage edges (successor-of, corresponds-to) beyond
  the manifest's `successors` / `related` fields. Generation order inside
  a family is mechanical; cross-family edges (a distillation) wait for a
  real need.
- A curator override of the name grammar. A name like `Meta-Llama-3.1`
  parses to family `Meta-Llama`; the board says "read from the name" and
  groups by the string. An override field is a small addition when it is
  wanted.
- Re-deriving parameter counts for packed dtypes. The Hub already counts
  packed 4-bit weights as parameters (verified: Kimi-K3's U8 count is
  exactly twice its packed bytes); darsay records the upstream count and
  marks the dtype packed.

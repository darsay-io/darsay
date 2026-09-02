# Lineage and precision — the model of the models

| | |
|---|---|
| **Author** | Jeremy Norris (drafted with Claude) |
| **Date** | 2026-09-02 |
| **Status** | Implemented (unreleased) — manifest schema 1.8.0 → **2.0.0**, catalog schema 1.2.0 → **2.0.0**, darsay.io board and worker updated in step. Sets the vocabulary in [NORTH-STAR.md](../NORTH-STAR.md). |
| **Audience** | darsay CLI and darsay.io implementers; readers of `docs/CONCEPTS.md` |
| **Related** | [classify.md](classify.md) (the negatives-by-default mechanism, formerly "masters-first") · [darsay-io.md](darsay-io.md) (boards) |

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
| Kimi-K3 | 2.78T | MXFP4, group 32 (compressed-tensors) | 0.56 | 1.4 TiB — a native 4-bit release; the negative *is* 4-bit |

The tooling also got Kimi K3 wrong twice: its 60 MB weight index exceeded
a 10 MiB read cap, so its one negative set classified as `unknown`, and
its tiktoken tokenizer made the bundle read as `incomplete`. And the
vocabulary was split: the CLI said *master/print*, the site's field guide
said *negatives and prints*, and the docs said both in one sentence.

## Decisions

1. **One vocabulary: negative and print.** `master` is gone from every
   surface — verdicts (`negative | print | support | unknown`), the
   archive policy (`negatives`), manifest and catalog `policy` values,
   the board's chip and lens, every document. Photography's pair is one
   metaphor; *master* was half of another and collides with git branches
   and mixed-precision "master weights".

2. **Precision is a recorded fact, and bytes per parameter is the number
   beside every size.** `precision.py` names the release precision from
   `config.json` (`quantization_config`, wherever a multimodal config
   nests it), the dominant safetensors dtype, or — only when GGUF is all a
   repo ships — the quant level in the file name. Bytes per parameter is
   measured (priced weight bytes over the published count). Both travel:
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
   curator `successors` / `related`) replacing `relationships`; the
   provider protocol's `relationships()` becomes `lineage()`.

4. **Closed works have a place.** A catalog entry's `source` may be a home
   URL on a host with no provider — an API-only model, an announced
   release. It is a `closed` row: no price, nothing to fetch, its place in
   the family held by the name grammar (`qwen3.8-max-0902` sits in Qwen
   3.8 beside `Qwen3.8-2.4T-A95B`). `closed` replaces the old `unknown`
   status. When weights ship, the address becomes a source ref.

5. **Boards browse the tree.** Family lenses (one chip per family on the
   board), a lineage view (families → generations → members, derivatives
   nested under the parent they declare, closed works dashed), and three
   new field-guide cards: negatives and prints (retitled), precision and
   bytes per parameter, families and generations, closed weights.

6. **Fix forward.** Manifest and catalog majors bump to 2. A 1.x manifest
   is refused with a re-archive hint; a 1.x catalog is refused with a
   re-add hint; a 1.x export does not import. No reader carries the old
   shape. The one live board is ported by one `darsay estimate
   <board-url>` after the site deploys.

7. **Kimi K3 reads correctly.** The JSON read cap rises to 64 MiB (the
   GGUF header cap), tiktoken vocabularies satisfy the tokenizer rule, and
   custom-code Python rides along as a sidecar of any subset.

## Surfaces

| Surface | Change |
|---|---|
| `darsay estimate` | `negatives:` says what the price is made of; `precision:` names the label and bytes/param; `family:` / `architecture:` / `lineage:` place the work; `parameters` marks packed dtypes; counts print in T. |
| `darsay classify` | Verdict `negative`; footer "Nothing here is a print — the negatives are the whole repo." |
| `darsay list` | FAMILY and PRECISION columns; `--sort family` reads the tree; `closed` status. |
| `darsay catalog add` | Accepts a home URL as a closed work; refuses `--revision` / `--include` on one. |
| Catalog README | Precision column; a **Families** section. |
| Manifest 2.0.0 | `identity` name-derived fields; `lineage` section; `model_metadata.precision` label, `precision_detail`, `bytes_per_param`. |
| Catalog 2.0.0 | Digest `precision`, `bytes_per_param`, `architecture`, `parents`; `policy: "negatives"`; home URLs; `closed`. |
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

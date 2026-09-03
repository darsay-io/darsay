<p align="center">
  <a href="GETTING-STARTED.md">Start here</a> ·
  <a href="CONCEPTS.md">Concepts</a> ·
  <a href="../examples/README.md">Examples</a> ·
  <a href="README.md">All docs</a> ·
  <a href="../README.md">README</a>
</p>

# catalog.json — schema reference (v2.0.0)

> **In one sentence.** A catalog is a curated list of works. The vault is
> that list, realized. Possession is a view, not a rewrite of this file.

`catalog.json` is the portable want-list. Overlay it against a vault with
`darsay list CATALOG`. Share the file (or the directory with its generated
`README.md`). A 2040 reader needs this document and the JSON; they do not
need darsay.

Conventions match the [manifest](MANIFEST.md):

- Timestamps are ISO 8601 UTC with second precision.
- `null` means unknown — the tool never fabricates sizes.
- No source-machine absolute paths. Cached estimates are a projection of
  `darsay estimate`, not the live dict (disk paths and bundle dirs are
  stripped).
- Major schema version is breaking. Additive minor/patch: readers ignore
  unknown fields; this tool preserves unknown *top-level* keys on
  round-trip. A file of another major is a hard error — a catalog is not
  migrated (a bundle's *record* is: `darsay migrate`); re-add its
  entries to a new one. The tool writes the version it conforms to.

The catalog schema is independent of bundle `schema_version`. Catalogs
are not inside `.mvb.tar`.

## Where it lives

```
vault/catalogs/<slug>/
├── catalog.json     # source of truth
├── README.md        # generated (`darsay catalog regen`)
└── curation.md      # curator; never overwritten once it exists
```

`catalogs/` is a reserved first-level vault name (with `.runtime/`). It is
not a bundle. `darsay du` still counts bundles and `.runtime` only.

A catalog may also be a lone `catalog.json` (USB, git clone, gist). Path-
addressed catalogs require `./`, `~/`, or an absolute path (same as bundle
addressing) and are read-only unless `--write`. A bare slug (`summer`)
resolves only under `vault/catalogs/<slug>/`. Vault-named catalogs are
writable.

## Top level

| Field | Meaning |
|---|---|
| `catalog_schema_version` | `"2.0.0"`. Major = breaking. |
| `kind` | Always `"darsay.catalog"`. |
| `id` | Slug. Matches the directory name when stored at `catalogs/<id>/`. Lowercase letter, then letters, digits, `.`, `_`, `-` (max 64). |
| `title` | Human title. Defaults to `id`. |
| `curator` | Free text, or `null`. |
| `note` | Catalog-level curator note, or `null`. |
| `created` / `updated` | Tool-written UTC timestamps. `updated` changes on add, drop, estimate-refresh, adopt, regen. |
| `entries` | Array. Insertion order is preserved on disk; sort is a view. |

## Entry

| Field | Meaning |
|---|---|
| `source` | The work's address. A canonical source ref after parse (`huggingface:Qwen/Qwen3-0.6B`, `huggingface:datasets/owner/name`, `test:acme/toy`) — or, for a **closed work**, its home URL on a host with no provider (`https://www.qwencloud.com/models/qwen3.8-max-0902`): an API-only model, an announced release. A closed work has no `revision`, no `include`, and no `estimate`; its family, generation, and member are read from the URL's last segment exactly as from a repo name. |
| `revision` | Intended pin, or `null` = any revision of this source satisfies. A 12-char (or longer) hex prefix is stored as typed. Non-hex refs match `revision_ref` exactly. |
| `include` | `null` (full repo) or a list of globs in argv order, same meaning as `archive --include`. Identity is the *sorted set* of those globs. |
| `desire` | Integer `1–9`, or `null`. 9 = most desired. Curator data. |
| `note` | Short curator note, or `null`. Not a Hub description. |
| `added` | When the entry was inserted. |
| `estimate` | Cached digest, or `null`. See below. |

Uniqueness: `(canonical source, revision or "", sorted include tuple)`. Full-repo and `*Q4_K_M*` of the same repo are different works.

## Estimate digest

A **projection** of live `estimate()`, not a subset and not the live dict.
Refresh is explicit (`darsay estimate CATALOG` or `catalog add --estimate`).
Stale after 7 days (`*` on SIZE in `list`).

| Digest key | Live `estimate()` source |
|---|---|
| `as_of` | `est["as_of"]` |
| `artifact_type` | `est["artifact_type"]` |
| `revision` | `est["source"]["revision"]` |
| `revision_ref` | `est["source"]["revision_ref"]` |
| `payload_bytes` | `est["payload"]["total_size_bytes"]` |
| `file_count` | `est["payload"]["file_count"]` |
| `license` | `est["source"]["license"]` |
| `gated` | `est["source"]["gated"]` |
| `parameters` | `est["parameters"]["total"]` if dict, else `null` |
| `dominant_dtype` | `est["parameters"]["dominant_dtype"]` if dict, else `null` |
| `unknown_size_count` | `est["payload"]["unknown_size_count"]` |
| `hints` | Derived once, by `hints_for(est)` — see [Hints](#hints). Since 1.1.0. |
| `policy` | `"negatives"` when the CLI classified the row and the stored price is its negative set — the default acquisition, whether or not any print was skipped ([Quantization §4](QUANTIZATION.md#4-mechanics)); `null` for `--full` and explicit-include prices, datasets, and rows priced by a board before the CLI's refresh. |
| `precision` | The release precision label — `BF16`, `FP8`, `MXFP4`, `AWQ INT4`, `Q4_K_M` — from `config.json`'s `quantization_config`, the dominant dtype, or (GGUF-only repos) the file name ([Quantization §2](QUANTIZATION.md#2-precision-and-the-one-number-that-explains-a-size)); `null` when nothing establishes it. |
| `bytes_per_param` | Measured: the priced weight bytes over `parameters`. About 2 is one full-fidelity copy, about 1 an 8-bit release, about 0.5 a 4-bit one, far above 2 several weight sets. `null` when either side is unknown. |
| `architecture` | `config.json` `model_type` (`qwen3_5_moe_text`), or `null`. |
| `parents` | Parent edges as upstream declares them: `[{source, relation}]` where `relation` is `finetune` / `adapter` / `merge` / `quantized` / `trained_on` or `null` when unlabeled. From the card's `base_model` and `datasets` and the Hub's `base_model:*` tags. `null` when nothing is declared. |

Never stored: `disk.*`, `bundle.dir`, engines, completeness, variants,
vault status, bundle ids.

### Hints

`hints` is a **sorted list from a closed set**, decided by the CLI at
estimate time so every reader — `list`, the generated `README.md`, a
darsay.io board — agrees on what the words mean without re-deriving them.
Empty (`[]`) means *nothing notable*; a missing digest still means
*unknown*. Nothing is guessed from a repo name.

| Hint | When |
|---|---|
| `gated` | Upstream is gated (`source.gated`). Archiving needs an accepted license and `hf auth login`. |
| `large` | The priced payload is ≥ 20 GiB (`LARGE_PAYLOAD_BYTES`) — more than one sitting, often more than one disk. With `--include`, the priced payload is the subset. An unknown size is never large. |
| `quant` | A published quantized artifact ([Quantization](QUANTIZATION.md)): the weight bytes are mostly GGUF, or the dominant safetensors dtype is not F64 / F32 / F16 / BF16. |
| `redundant` | The priced weight bytes are ≥ 1.75× one copy at the published per-dtype parameter counts — the repo likely ships several weight sets (`darsay classify` shows them). Live estimates only; never re-derived from a stored digest. Since 1.2.0. |
| `subset` | The entry was priced with an explicit `--include`. A negatives-policy price sets the digest's `policy` key instead. |

A digest written by a darsay.io board before the CLI has refreshed it
carries the hints the board could derive. Readers derive `large`,
`gated`, and `subset` from the digest and the entry's `include` on the
fly; `quant` needs the live estimate's weight formats, so a GGUF row
shows it only after `darsay estimate CATALOG` refreshes the digest (a
quantized safetensors dtype is derived either way). Refresh is the only
thing that writes `hints`; `catalog adopt` copies it verbatim.

### Lineage (not in the file)

Family, generation, member, variants, formats, and size are **read from
the name** at view time by the grammar in `lineage.py` — the same grammar
darsay.io runs — so the file stores nothing it could derive. `darsay
list CATALOG --json` rows carry them under `lineage` (with `read_from:
"name"`); `list` shows a FAMILY column; `--sort family` orders rows as
the tree (family, then generation oldest first, then size); the
generated README draws a **Families** section. Parent edges are the one
lineage fact the file stores, in the digest's `parents`, because they
come from upstream declarations, not from the name.

## Overlay (not in the file)

`darsay list CATALOG` matches each entry against this vault’s
`bundle_records` by canonical source address + optional revision + include
set. An entry with `include: null` means *the default acquisition*: it is
satisfied by a negatives-policy bundle (what `archive <source>` produces)
or by a full-repo bundle (a superset). An entry with explicit include
globs matches only its globs.

| Status | Meaning |
|---|---|
| `have` | A complete bundle of this work is in the vault. |
| `partial` | An in-progress pin (ledger, no manifest) matches. |
| `want` | Nothing in this vault matches. |
| `closed` | Nothing to fetch: the address is a home URL, or a scheme with no provider yet. Not unfinished work — a place held. A known provider with a locator that does not parse is a load error, not `closed`. |

`archive --next` and `list --sort next` prefer `partial` over `want`
(finish bytes already on disk), then higher desire. `--sort desire` is
priority-first; `--sort family` is the tree. Closed rows are not
unfinished work: `--next` skips them when anything else remains, and
says so if they are all that is left.

A vault stores one pin per `(source, revision12)`. Catalog rows that
differ only by `--include` are different works in the catalog, but they
cannot both occupy the same bundle directory. `--next` of a full-repo
row will not resume a subset pin of the same source.

`archive` does not write `catalog.json`. Status flips when *this* vault
grows bytes. A friend’s overlay against their empty vault is all `want`.

Remaining GiB is remaining-to-finish: want entries contribute cached
`payload_bytes`; partials contribute `remaining_network`; have is 0.
Unknown bytes print as `+ ?`, never as zero. A partial that is a
*skeleton* — bytes handed to another vault (`assemble --handoff`, see
[Incremental](INCREMENTAL.md#across-disks-assemble---handoff-and-skeletons)) —
counts those handed-off bytes as done, not as remaining: its `remaining_network`
is only what is still to fetch here.

## Boards (darsay.io)

A board on darsay.io is a catalog with a URL, and that URL is a
**catalog address** — the third form after a vault slug and a
filesystem path:

    darsay estimate https://darsay.io/b/<board-id>     # fetch → classify → push back
    darsay list     https://darsay.io/b/<board-id>     # overlay against this vault (read-only)
    darsay archive --next https://darsay.io/b/<board-id>
    darsay catalog add  <board-url> huggingface:owner/name --desire 8
    darsay catalog drop <board-url> huggingface:owner/name --full
    darsay catalog adopt local-name <board-url>        # pull a local copy

`estimate` against a board is the round trip that keeps it honest: the
board's own quick estimates can only price the shipping box, while the
CLI classifies (negatives-first prices, `hints`, the `policy` marker,
precision and bytes per parameter) and pushes the refreshed catalog
back. Closed rows are left as they are — there is nothing to price. Mutating verbs push after saving;
`--dry-run` never pushes. The board URL is the capability — treat it
like a secret. The page address with `.json` (`/b/<board-id>.json`, the
board as a document for programs) names the same board and is accepted
wherever a board URL is.

`archive SOURCE --board <board-url>` claims the row for a source *you*
chose — the board's desire ordering decides nothing, but the claim and
the progress gauge still happen; a source with no matching row archives
unclaimed, with a warning. `archive --next <board-url>` instead lets
the board pick, and **claims** the row it picks, signed
as this machine (`board.client` in config; the default is a stable
pseudonym like `amber-heron-3f`, never the raw hostname — the board URL
travels, and the hostname of who holds it should not). A row
the board already marks `have` — a client reported done, or someone
checked it off — is never picked, even though board status stays out of
`catalog.json` and your vault alone would still want it; name the
source with `--board` to re-fetch one deliberately. A row
another client holds a live claim on is skipped — that is how two
people split one board without colliding. Archive boundaries (start,
clean pause, registration) report progress, which the board renders as
a gauge; reporting done flips the row to `have` and fills an empty
holders field with the client id. Claims, like the board's status and
holders columns, are board-side coordination: they never appear in
`catalog.json`, and a stale claim (24 h without a report) simply
expires.

## CLI

```
darsay catalog new NAME
darsay catalog add CATALOG SOURCE [--desire 1-9] [--estimate]
darsay catalog add CATALOG https://host/path/to/a-closed-work   # holds its place, no price
darsay catalog drop CATALOG SOURCE [--include GLOB | --full]
darsay catalog regen CATALOG
darsay list CATALOG
darsay list CATALOG --want
darsay list CATALOG --next
darsay estimate CATALOG
darsay archive --next CATALOG
darsay catalog adopt MINE ./friend
```

`catalog add` is offline unless `--estimate`. Every `catalog` verb and
`estimate CATALOG` take `-n` / `--dry-run`: the change is printed, the file
is not written. Bare `darsay list` is the
vault as the same table; DESIRE, HINTS, and NOTE hide when every cell is
empty. HINTS is the entry's [hints](#hints) (`large, gated`), the same
words `estimate CATALOG` prints per row.
`list --next` prints a copy-pasteable `darsay archive` line (source +
`--revision` + `--include`). FAMILY and PRECISION show the name-derived
generation and the digest's precision label. Cookbook:
[Share a catalog](../examples/README.md#share-a-catalog).

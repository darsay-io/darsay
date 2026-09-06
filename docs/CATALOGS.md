<p align="center">
  <a href="GETTING-STARTED.md">Start here</a> ·
  <a href="CONCEPTS.md">Concepts</a> ·
  <a href="../examples/README.md">Examples</a> ·
  <a href="README.md">All docs</a> ·
  <a href="../README.md">README</a>
</p>

# catalog.json — schema reference (v3.2.0)

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
| `catalog_schema_version` | `"3.2.0"`. Major = breaking; 3.1.0 added the digest's `attention`; 3.2.0 added `imatrix` to each `gguf_variants` entry. |
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
| `include` | `null` (default acquisition) or a list of globs in argv order, same meaning as `archive --include`. Identity is the *sorted set* of those globs. |
| `desire` | Integer `1–9`, or `null`. 9 = most desired. Curator data. |
| `note` | Short curator note, or `null`. Not a Hub description. |
| `added` | When the entry was inserted. |
| `estimate` | Cached digest, or `null`. See below. |

Uniqueness: `(canonical source, revision or "", sorted include tuple)`. A default acquisition and `*Q4_K_M*` selection of the same repo are different entries.

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
| `payload_bytes` | `est["payload"]["total_size_bytes"]`: known bytes at `size_basis`; a lower bound when `unknown_size_count` is nonzero. |
| `file_count` | `est["payload"]["file_count"]` |
| `license` | `est["source"]["license"]` |
| `gated` | `est["source"]["gated"]` |
| `parameters` | `est["parameters"]["total"]` if dict, else `null`. Total model parameters, including inactive experts; never the active count of a mixture of experts model. |
| `parameters_source` | `"safetensors"`, `"gguf"`, or `null`. GGUF counts come from upstream `gguf.total` when safetensors counts are unavailable. |
| `dominant_dtype` | `est["parameters"]["dominant_dtype"]` if dict, else `null` |
| `unknown_size_count` | `est["payload"]["unknown_size_count"]`: selected files whose sizes are unknown. |
| `hints` | Derived once, by `hints_for(est)` — see [Hints](#hints). |
| `size_basis` | `"repository"` for the upstream inventory, `"selection"` for an explicit or pinned selection, `"archive"` for a classified retention decision. An archive can retain negative, unknown, support, and print files; this field is not a preservation verdict. |
| `repository_bytes` | Exact byte total across all upstream files, or `null` if any size is unknown. Remains the whole repository even when `payload_bytes` describes a selection. |
| `classification` | `null` when unavailable; otherwise `{verdicts, skipped_bytes, unclassified_count}`. `verdicts` maps each present `negative`, `print`, or `unknown` verdict to `{sets, files, bytes}`. `unclassified_count` counts unresolved **sets**, not files. Support bytes are included in the payload separately. Prints matching a remote base remain retained; only sets whose every file has a hash-identical retained same-bundle twin are automatically skipped. |
| `gguf_variants` | Whole upstream inventory of GGUF model variants: `[{name, precision, file_count, size_bytes, complete, include, imatrix}]`. Each entry groups all shards of one variant; `complete` requires every declared shard exactly once. `size_bytes` is the sum of its files, or `null` if a size is unknown. `include` contains exact selection globs. `imatrix` is what the variant's GGUF headers established when classification read them: `true` when any header carries `quantize.imatrix.*` (rule R7), `false` when every header was read and none does, `null` when a header was not read — never a reading of the file name. Projectors such as `mmproj` are companions, not model variants. An empty list means no GGUF model variants were found. |
| `precision` | The release precision label — `BF16`, `FP8`, `MXFP4`, `AWQ INT4`, `Q4_K_M` — from `config.json`'s `quantization_config`, the dominant dtype, or (GGUF-only repos) the file name ([Quantization §2](QUANTIZATION.md#2-precision-and-size-scope)); `null` when nothing establishes it. Precision does not establish original-release status. |
| `bytes_per_param` | Measured model weight bytes over `parameters`. About 2 is a 16-bit release, about 1 an 8-bit release, about 0.5 a 4-bit one. `null` when either side is unknown, and for GGUF packs, incomplete shard selections, or projector-only selections. |
| `architecture` | `config.json` `model_type` (`qwen3_5_moe_text`), falling back to the Hub's GGUF architecture metadata; `null` when neither is available. |
| `attention` | The KV cache's shape from `config.json` — `{kind, full_layers, sliding_layers, sliding_window, recurrent_layers, kv_heads, head_dim, values, kv_bytes_per_token}` — so a reader can price a context length: the cache for N tokens is `kv_heads × head_dim × values × bytes per value × (full_layers × N + sliding_layers × min(N, sliding_window))`, and `kv_bytes_per_token` is that at sixteen bits for one token across every attending layer. `kind` is `mha` / `gqa` / `mqa` / `mla` (`values` 1: one latent per token). Recurrent layers are counted, not priced. `null` for a GGUF-only repository or a config that does not establish the shape. Same field as the manifest's `model_metadata.attention`. |
| `parents` | Parent edges as upstream declares them: `[{source, relation}]` where `relation` is `finetune` / `adapter` / `merge` / `quantized` / `trained_on` or `null` when unlabeled. From the card's `base_model` and `datasets` and the Hub's `base_model:*` tags. `null` when nothing is declared. |

The GGUF inventory remains complete even on a selected row, so a reader
can compare choices without mistaking the selected bytes for the whole
repository. A model variant is not a preservation verdict: both a
negative and a print can occupy one variant row.

Never stored: `disk.*`, `bundle.dir`, engines, overall payload
completeness, descendant-repository estimates, vault status, bundle ids.

### Hints

`hints` is a **sorted list from a closed set**, decided by the CLI at
estimate time so every reader — `list`, the generated `README.md`, a
darsay.io board — agrees on what the words mean without re-deriving them.
Empty (`[]`) means *nothing notable*; a missing digest still means
*unknown*. Nothing is guessed from a repo name.

| Hint | When |
|---|---|
| `gated` | Upstream is gated (`source.gated`). Archiving needs an accepted license and `hf auth login`. |
| `large` | Known payload bytes are ≥ 20 GiB (`LARGE_PAYLOAD_BYTES`) — more than one sitting, often more than one disk. With `--include`, this is the subset. A lower bound can establish that a payload is large even when some file sizes are unknown. |
| `quant` | A published quantized artifact ([Quantization](QUANTIZATION.md)): the weight bytes are mostly GGUF, or the dominant safetensors dtype is not F64 / F32 / F16 / BF16. |
| `redundant` | The priced weight bytes are ≥ 1.75× one copy at the published per-dtype parameter counts — the repo likely ships several weight sets (`darsay classify` shows them). Live estimates only; never re-derived from a stored digest. |
| `subset` | The entry was priced with an explicit `--include`. Classification is represented separately by `size_basis` and `classification`. |

A digest written by a darsay.io board carries the hints its metadata
estimate could establish. Readers can derive `large`, `gated`, and
`subset` from the digest and the entry's `include`; a GGUF inventory also
establishes the published format. Refresh writes the estimate's hints;
`catalog adopt` copies them verbatim. A `quant` hint is a format or dtype
observation, not a negative/print verdict.

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
satisfied by a classified archive bundle (what `archive <source>` produces)
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

`estimate` against a board refreshes the preservation evidence. The
board's quick metadata estimates price the repository or an explicit
selection and inventory its GGUF variants; the CLI classifies the default
acquisition and pushes its archive size, verdict counts, hints, precision,
and parameter provenance back. Every displayed price names its basis;
neither a repository inventory nor an archive total is labeled as a
quantity of negatives. Closed rows are left as they are — there is
nothing to price. Mutating verbs push after saving;
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
people split one board without colliding. While the transfer runs, the
row shows the panel: about once a minute the CLI reports what the
terminal shows — percent, bytes banked of the total, the rate and its
sparkline, the time left, files done, the file in flight, and the
panel's own word for the moment (downloading, verifying, stalled,
offline, retrying) — and the board draws the same rail for everyone
who has the page open. A report goes out when a whole percent has
passed or that word changed, and in any case every five minutes, so a
row that has not heard for longer knows the client is gone.
`board.report_every` in config sets the cadence (`"1m"` by default;
`"0"` keeps to the boundaries — start, a clean pause, registration —
which report regardless). Reporting done flips the row to `have` and
fills an empty holders field with the client id. Claims, like the
board's status and holders columns, are board-side coordination: they
never appear in `catalog.json`, and a stale claim (24 h without a
report) simply expires.

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

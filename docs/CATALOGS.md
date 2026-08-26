<p align="center">
  <a href="GETTING-STARTED.md">Start here</a> ·
  <a href="CONCEPTS.md">Concepts</a> ·
  <a href="../examples/README.md">Examples</a> ·
  <a href="README.md">All docs</a> ·
  <a href="../README.md">README</a>
</p>

# catalog.json — schema reference (v1.0.0)

> **In one sentence.** A catalog is a curated list of sources. The vault is
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
  round-trip. A 2.x file is a hard error for a 1.x tool.

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
| `catalog_schema_version` | `"1.0.0"`. Major = breaking. |
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
| `source` | Canonical source ref after parse (`huggingface:Qwen/Qwen3-0.6B`, `huggingface:datasets/owner/name`, `test:acme/toy`). |
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

Never stored: `disk.*`, `bundle.dir`, engines, completeness, variants,
vault status, bundle ids.

## Overlay (not in the file)

`darsay list CATALOG` matches each entry against this vault’s
`bundle_records` by canonical source address + optional revision + include
set.

| Status | Meaning |
|---|---|
| `have` | A complete bundle of this work is in the vault. |
| `partial` | An in-progress pin (ledger, no manifest) matches. |
| `want` | Nothing in this vault matches. |
| `unknown` | Unregistered source scheme (a future provider). Not unfinished work. A known provider with a locator that does not parse is a load error, not `unknown`. |

`archive --next` and `list --sort next` prefer `partial` over `want`
(finish bytes already on disk), then higher desire. `--sort desire` is
priority-first. Unknown rows are skipped.

`archive` does not write `catalog.json`. Status flips when *this* vault
grows bytes. A friend’s overlay against their empty vault is all `want`.

Remaining GiB is remaining-to-finish: want entries contribute cached
`payload_bytes`; partials contribute `remaining_network`; have is 0.
Unknown bytes print as `+ ?`, never as zero.

## CLI

```
darsay catalog new NAME
darsay catalog add CATALOG SOURCE [--desire 1-9] [--estimate]
darsay list CATALOG
darsay list CATALOG --want
darsay estimate CATALOG
darsay archive --next CATALOG
darsay catalog adopt MINE ./friend
```

`catalog add` is offline unless `--estimate`. Bare `darsay list` is the
vault as the same table; DESIRE and NOTE hide when every cell is empty.
`list --next` prints a copy-pasteable `darsay archive` line (source +
`--revision` + `--include`). Cookbook:
[Share a catalog](../examples/README.md#share-a-catalog).

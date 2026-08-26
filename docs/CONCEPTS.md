<p align="center">
  <a href="GETTING-STARTED.md">Start here</a> ·
  <a href="CONCEPTS.md">Concepts</a> ·
  <a href="../examples/README.md">Examples</a> ·
  <a href="README.md">All docs</a> ·
  <a href="../README.md">README</a>
</p>

# Concepts

darsay is small once the objects are named. This page is the naming.

Have not run it yet? [Start here](GETTING-STARTED.md) first — five minutes,
then come back. The ideas land harder after you have a bundle on disk.

## Vault

A vault is a directory of bundles. Default: `./vault`. Override with
`--vault` or `$DARSAY_HOME`.

```
vault/
├── qwen--qwen3-0.6b/<rev>/          # a model bundle
├── datasets--rotten_tomatoes/<rev>/ # a dataset bundle
└── .runtime/                        # disposable inference envs (not archival)
```

There is no hidden database. `darsay list` is a walk of this tree.
The vault is yours to rsync, restic, or put on a shelf.

## Bundle

A bundle is **one pinned revision of one source**, stored so it is both
a museum record and a working checkout.

```
vault/<name>/<revision12>/
├── model/            # or data/ — the immutable payload
├── manifest.json     # recorded facts (the source of truth)
├── README.md         # generated from the manifest (`darsay regen`)
├── curation.md       # the only file you write by hand
└── LICENSE           # upstream license, surfaced at the root
```

Four parts, always:

| Part | Role | Mutability |
|---|---|---|
| Payload (`model/` or `data/`) | Byte-exact snapshot of upstream | Frozen after archive |
| `manifest.json` | What was established, never guessed | Tool may add facts; never fabricates |
| Generated views (`README.md`, `VERIFICATION.md`) | Human-readable projections | Regenerated |
| `curation.md` | Your notes | Yours; never overwritten once it exists |

The **bundle hash** covers the payload only. Tool-written state at the
bundle root can change without the archive “changing.”

## Pin

`archive` does not mean “download `main`.” It means:

1. Resolve the ref (`main`, a tag, a commit) to an **immutable revision**.
2. Freeze the file set for that revision.
3. Transfer those files, and only those files, until every one verifies.

Rerunning `archive` on the same source continues *that pin*. It does not
chase a moving `main`. To take a new snapshot, `--force` pins again.

That is why resume works without a special subcommand, and why a 50 GB
job can be ten evenings of `--max-gb 10`.

## Payload vs metadata

This distinction is the whole design.

- **Payload** — the files a loader needs. Never rewritten. Copied,
  hashed, verified, exported. For models, `model/` is a pristine Hub
  snapshot: `transformers` loads it as a local directory. For datasets,
  `data/` is the same idea for parquet/jsonl/csv.
- **Metadata** — `manifest.json` and the sibling reports. The tool owns
  these. They record facts (hashes, license text, parameter counts from
  safetensors headers, Hub tags *at archive time*). Unknown is `null`.
  Query caps are recorded, never silently truncated.

If a number is not in the payload and not returned by upstream, it is
not in the manifest. Curators fill gaps in `curation.md`.

## The loop

```mermaid
flowchart LR
  S["Source"] --> E["estimate"]
  E --> A["archive"]
  A --> B["Bundle"]
  B --> V["verify"]
  B --> R["run"]
  B --> X["export"]
  X --> I["import"]
```

| Verb | What it does | What it does not do |
|---|---|---|
| `estimate` | Read-only preflight from source metadata | Download, write, guess |
| `archive` | Pin, transfer, hash, register | Mutate an already-registered payload |
| `verify` | Re-hash payload vs manifest | Repair files |
| `run` | Hydrate an env, infer offline | Touch `model/` |
| `export` | Pack a deterministic `.mvb.tar` | Include machine-local logs |
| `import` | Unpack, re-hash, then register | Trust the tar without checking |

`hydrate` is the explicit form of what `run` does first.
`dehydrate` / `envs --prune` throw away runtimes, never archives.

## Record, don't fabricate

The Hub will lie to a future reader by vanishing. The manifest must not
lie in the other direction by filling blanks.

- Established from upstream or the payload → recorded.
- Not established → `null`.
- A listing that stopped at a query cap → the cap is stored
  (`query_limit`), and the list is marked incomplete.

This is why a bundle remains interpretable without darsay: a 2040 reader
opens `manifest.json` and [MANIFEST.md](MANIFEST.md), and knows which
fields are facts.

## Hydration is disposable

Inference needs torch, or llama-cpp, or whatever the payload's format
implies. Those packages are large, versioned, and not archival.

So they do not live in the bundle. They live under
`<vault>/.runtime/envs/`, content-keyed, shared across bundles with the
same needs. `hydration.json` at the bundle root is a pointer plus a run
log. Delete it. The next `run` rebuilds. The payload is still there.

A passing `run` is evidence: it executed with `HF_HUB_OFFLINE=1`. The
archived bytes were sufficient. Details: [Hydration](HYDRATION.md).

## Formats outlive the tool

Museum-grade does not mean “hope this CLI still runs.” Longevity sits in
two boring formats:

- **`manifest.json`** — plain JSON, field-by-field spec in
  [MANIFEST.md](MANIFEST.md).
- **`.mvb.tar`** — uncompressed tar, marker first, deterministic
  metadata, unpackable with stock `tar`. Spec and manual recovery:
  [MVB-FORMAT.md](MVB-FORMAT.md).

The payload is already a format the world knows: a Hugging Face repo
layout. The tool is glue. The bundles are the product.

## Two artifact types, one shape

A bundle is type-agnostic: immutable payload + recorded facts + derived
views + one curator file. The type only changes the payload root and
what “complete” means.

| | Model | Dataset |
|---|---|---|
| Address | `huggingface:owner/name` | `huggingface:datasets/owner/name` |
| Shorthand | `owner/name` | `datasets/owner/name` |
| Payload | `model/` | `data/` |
| Engines | transformers, llama-cpp, … | none — open the files |
| Extra manifest | `model_metadata`, `runtime` | `dataset_metadata` |

No new verbs. `verify` / `export` / `info` dispatch on
`manifest.artifact_type`. [Datasets](DATASETS.md).

## Sources are plugins

`estimate` and `archive` take a **source ref**, not “a Hugging Face
repo.” Hugging Face is the first provider:

```
huggingface:Qwen/Qwen3-0.6B
hf:Qwen/Qwen3-0.6B
https://huggingface.co/Qwen/Qwen3-0.6B
Qwen/Qwen3-0.6B                          # shorthand
```

A second host is another `SourceProvider`, not a new CLI flag.
[Sources](SOURCES.md).

## What is archival, what is cache

The canonical bundle is the **highest-fidelity upstream release**,
byte-exact. Published quants that the world actually ran (an official
FP8, a community GGUF) are ordinary **satellite bundles** — they cannot
be regenerated bit-exact from the master.

Running the model smaller on your machine is hydration-time derivation,
never archival. [Quantization](QUANTIZATION.md).

## If you remember four sentences

1. A vault is a folder of bundles.
2. A bundle is a pinned snapshot whose payload never changes.
3. The manifest records facts and leaves blanks as blanks.
4. You load `model/` (or `data/`) the way you would load the original repo.

That is darsay. The rest is resume, proof, and packing.

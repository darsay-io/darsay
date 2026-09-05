<p align="center">
  <a href="GETTING-STARTED.md">Start here</a> ·
  <a href="CONCEPTS.md">Concepts</a> ·
  <a href="../examples/README.md">Examples</a> ·
  <a href="README.md">All docs</a> ·
  <a href="../README.md">README</a>
</p>

# Code bundles — the vault's third artifact type

> **In one sentence.** A repository is addressed as `github:owner/repo`
> and its payload lives in `code/`; everything else is identical.
> Copy-paste: [archive a repository](../examples/README.md#archive-a-repository).

```bash
darsay estimate github:MiaAI-Lab/Qwen3.8-Flash-Next-Single-DGX-Spark
darsay archive  github:MiaAI-Lab/Qwen3.8-Flash-Next-Single-DGX-Spark
```

A code bundle is a source tree at one pinned commit, kept the way a model
bundle keeps a snapshot: immutable payload, recorded facts, derived views,
one curator file. Case study: the recipe above.

## 1. Why

A model is bytes; running it is knowledge, and the knowledge lives in
repositories. The launcher that serves a 99 GB checkpoint on one machine,
the patches that make an inference engine accept it, the training script,
the evaluation harness — none of it is on the model's Hub page, and all of
it is the most perishable part of the ecosystem: rewritten daily,
force-pushed, renamed, deleted.

The [north star](NORTH-STAR.md) asks for a functional brick, proven by a
run. darsay's own engines prove that for models that fit `transformers`,
`llama.cpp`, or MLX on the collector's machine. For a 180B-parameter
release that needs a patched vLLM on one specific GPU, the proof *is* the
repository that runs it. Keeping the checkpoint and losing the recipe
keeps the sculpture and discards the instructions for standing it up.

The case study makes the shape concrete. `MiaAI-Lab/Qwen3.8-Flash-Next-Single-DGX-Spark`
is 23 files and 270 KiB: a download script, a launcher, a watchdog, and
five generators that patch vLLM. It serves `Mia-AiLab/Qwen3.8-Flash-Next-NVFP4`,
a 98.7 GiB checkpoint on the Hub, inside the container image
`vllm/vllm-openai:qwen38-flash-next`. Three artifacts on three hosts. The
vault now holds the first two as bundles; the third is the subject of the
[workbench proposal](proposals/workbench.md).

## 2. The conceptual contract — what a bundle is, type-free

Every invariant is artifact-agnostic — payload immutability, export
determinism, record-don't-fabricate, verify-before-register,
generated-vs-hand-edited, registry extensibility, disposable hydration —
and none mentions "model". The one sentence a user needs:

> *A repository is addressed as `github:owner/repo` (or its github.com
> URL) and its payload lives in `code/`; everything else is identical.*

That sentence is the design's budget. What the type does **not** say is
what the tree is *for*. A repository can be a serving harness, training
code, a paper's source, or a model with weights in Git LFS. darsay
records what the tree carries and lets the curator say what it means.

## 3. The whole delta — five registry-scoped properties

| Property | `model` | `dataset` | `code` |
|---|---|---|---|
| payload root | `model/` | `data/` | `code/` |
| completeness rules | config / weights / tokenizer | data files | at least one file; README and license recommended |
| metadata extractor | `model_metadata` | `dataset_metadata` | `code_metadata`: what upstream said, and which standard build/run files the tree carries |
| lineage snapshot | quantized / finetunes / adapters of it | models trained on it | the fork's parent as upstream declares it; fork count |
| engines | transformers, llama-cpp, mlx | none | none — `hydrate` / `run` refuse by name |

## 4. Addressing: one grammar, a second provider

```
github:owner/repo
gh:owner/repo
https://github.com/owner/repo          # paste from the browser; .git, a trailing slash, an issue link all resolve
```

GitHub is the second [source provider](SOURCES.md), and a repository is a
code bundle on every provider that serves one. Three things about the
address:

- **`HEAD` is the default revision** — the repository's default branch,
  whatever it is called. darsay never assumes `main`.
  `--revision` takes a branch, a tag, or a commit.
- **A URL that buries a revision is refused with the flag.**
  `…/tree/v1.2`, `…/blob/main/README.md`, `…/commit/abc…`, and
  `…/releases/tag/v1` all stop with
  `darsay archive github:owner/repo --revision <ref>` printed, so the
  revision the collector was looking at is the one that gets pinned.
- **A token comes from the environment.** `GITHUB_TOKEN` (or `GH_TOKEN`)
  reads private repositories and lifts the unauthenticated API allowance.
  An exhausted allowance is a refusal naming the reset time; a private
  repository without a token is a refusal naming the variable.

`--include` narrows a tree the way it narrows a repository: matching
files plus the standard sidecars, recorded as a subset.

## 5. Naming and layout

    vault/github--miaai-lab--qwen3.8-flash-next-single-dgx-spark/09d4424be2b7/
    ├── code/            immutable payload — the tree at that commit, no .git
    ├── manifest.json    artifact_type: "code"
    ├── SHA256SUMS       sha256sum -c works here with no darsay
    └── ... identical bundle anatomy ...

- **Directory and bundle id** carry the provider: `github--owner--repo`,
  lower-cased, so a GitHub `owner/repo` can never collide with a Hub one.
- **The revision is the commit SHA.** Re-archiving that revision
  reproduces the payload bit for bit, which is the same promise a Hub
  bundle makes.
- **Every file has an upstream expectation.** A git blob carries the
  SHA-1 the tree names it by (`upstream_git_sha1`); a Git LFS object
  carries the SHA-256 and true size from its pointer
  (`upstream_lfs_sha256`). `verify` compares against both, and the
  archive holds the object, never the pointer.

## 6. The code manifest

Universal sections unchanged. Code bundles carry `code_metadata` instead
of `model_metadata` / `runtime` ([field by field](MANIFEST.md#code_metadata--code-bundles-only)):

- **What upstream said**, recorded at pin time: `description`, `homepage`,
  `topics`, `languages` (bytes per language), `default_branch`,
  `archived_upstream` (the repository was already frozen where it lives),
  `submodules` the tree names — recorded, not fetched — and `symlinks`.
- **What the tree carries**, read from the inventory:
  `runtime_declarations`. A closed vocabulary of the standard files that
  say how a tree is built or run, each with the paths that matched:

  | Label | Files |
  |---|---|
  | `container` | `Dockerfile`, `Dockerfile.*`, `*.Dockerfile`, `Containerfile` — anywhere |
  | `compose` | `compose.yaml` / `.yml`, `docker-compose*.yaml` / `.yml` — anywhere |
  | `python` | `pyproject.toml`, `setup.py`, `setup.cfg`, `requirements*.txt`, `requirements/*.txt`, `environment.yml`, `uv.lock`, `poetry.lock` |
  | `node` / `rust` / `go` | `package.json` / `Cargo.toml` / `go.mod` |
  | `nix` | `flake.nix`, `shell.nix`, `default.nix` |
  | `make` | `Makefile`, `justfile` — tree root only |
  | `env_template` | `.env.sample`, `.env.example`, `.env.template` — tree root only |
  | `shell` | `*.sh` — tree root only |

  Lists are capped at 20 paths with the true count beside them, and the
  section says `read_from: "inventory"`. It is evidence of what the tree
  can do — the vocabulary a harness dispatches on — never a verdict on
  what it is for.

- **Lineage.** `parents` holds a fork's upstream when GitHub declares one
  (`relation: fork`, `declared_by: api`); `descendants.forks_count` is
  the count at archive time. Nothing is listed, so `query_limit` is null.
- **`source.upstream_stats_at_archive.likes`** is the star count.
  `source.access.gated` reads `"private"` for a private repository; the
  token that read it is not part of the record.

## 7. What the tree references

A serving recipe names the checkpoint it serves and the image it runs
in; training code names its datasets; a README credits the repository an
idea came from. What a program will actually load at run time is
undecidable in general — the model id can be computed, passed as an
argument, or fetched from somewhere else — and darsay does not guess at
it. What it can prove is that *these strings are in these files*, so that
is what `code_metadata.references` records, with provenance in three
tiers, strongest first:

| Tier | Where it was found | Examples |
|---|---|---|
| `declared` | A standard file whose job is to say it | a dotenv template's `IMAGE=…`, a compose file's `image:`, a Dockerfile's `FROM`, a Spaces card's `models:` list |
| `evidence` | A literal in code — a quoted string, or the default of a shell `${VAR:-…}` expansion read as text, never evaluated | `MODEL_ID="${TP1_MODEL_ID:-Mia-AiLab/Qwen3.8-Flash-Next-NVFP4}"` |
| `mentioned` | Prose: READMEs, issue templates, comments and docstrings, a URL in any of them | a credit, a benchmark tool, an example in a usage comment |

Each reference is a `model`, `dataset`, `image`, or `code` (another
repository) with its canonical address — `huggingface:owner/name`,
`oci:image:tag`, `github:owner/repo` — where it was found, how many
times, and what upstream said when asked. **Resolution** is the one
network step: declared and evidence references are looked up on their
provider (at most 20, the cap recorded); mentions are not, because a
mention is not a claim; images have no provider yet.

One rule turns evidence into a lineage edge. Exactly one *model*
reference at the evidence tier or better that resolves upstream is the
tree's **primary model**, recorded in `lineage.parents` as
`relation: references` with the provenance that named it. Two resolving
candidates are recorded as candidates and the curator chooses; none is
none. The relation is `references`, not `serves`: a name is evidence, a
purpose is a verdict.

The case study, scanned live:

```
  references:   image   oci:vllm/vllm-openai:qwen38-flash-next  [.env.sample: env template (+4 more places); a tag, not a digest]
                model   huggingface:Mia-AiLab/Qwen3.8-Flash-Next-NVFP4  [download.sh: shell default (+5 more places); resolves; 98.7 GiB upstream, not in this vault]
                model   huggingface:RadixArk/Qwen3.8-Flash-Next-NVFP4  [files/patch_modelopt_mxfp8.py: literal; mentioned only]
                code    github:MiaAI-Lab/sparkDash  [README.md: URL (+2 more places); mentioned only]
                code    github:lancelind/qwen3.8-Flash-DGX  [README.md: URL; mentioned only]
```

The author declared exactly one thing in a standard file — the image.
The model is a shell default in two launchers, a Python literal, and
prose. A second checkpoint with the same name under another publisher
appears once, in a docstring, and stays a mention. That is the honest
shape of a recipe, and it is why the tiers exist.

Two facts the scan makes explicit are the reason to archive the pair
together. **The recipe pins no revision of the model** — its download
script takes whatever `main` is today — so every model reference carries
`revision: null`, and the bundle of that model in a vault is the pinned
instance the recipe was archived beside. **The image is a tag, not a
digest**; `digest` stays null until an image provider records what the
tag pointed at on the day.

A resolving reference is a source ref, so it becomes first-class by
the machinery that already exists: `estimate` prices it upstream and
says whether this vault holds it; `darsay catalog add CATALOG
huggingface:Mia-AiLab/Qwen3.8-Flash-Next-NVFP4` makes it a want beside
the recipe, `list` shows have or want for both, and `archive --next`
fetches it. No new verbs.

## 8. Per-command behavior

`estimate` prices a repository like anything else — exact sizes from the
tree, completeness, the disk verdict — and, where a model shows
parameters and precision, prints what upstream said and what the tree
declares:

```
  about:        Qwen3.8-Flash-Next on ONE DGX Spark (TP=1)  [upstream]
  languages:    Python 66%, Shell 34%  [upstream]
  declares:     env_template (1), shell (3)  [read from the inventory]
  references:   image   oci:vllm/vllm-openai:qwen38-flash-next  [.env.sample: env template (+4 more places); a tag, not a digest]
                model   huggingface:Mia-AiLab/Qwen3.8-Flash-Next-NVFP4  [download.sh: shell default (+5 more places); resolves; 98.7 GiB upstream, not in this vault]
  formats:      py 102.6 KiB in 7, md 71.9 KiB in 6, sh 52.5 KiB in 4, …
  family:       Qwen · generation 3.8 · member Flash-Next-Single-DGX-Spark  [read from the name]
  payload:      23 files, 270.0 KiB (repository)
  engines:      none (code bundle — hydrate/run not applicable)
```

The reference scan at `estimate` time is bounded range reads —
declarations and the README first, then root-level code, then the rest,
within a read budget the record confesses when it binds; `archive` scans
the whole tree from disk. The family line is the same name grammar every
bundle gets, labeled the same way; it is what lets a recipe named after
its model sit beside that model on a board.

`archive` pins the commit, fetches every blob, cross-checks git SHA-1 and
LFS SHA-256, resolves what the tree references, and writes the same
reports. `verify`, `export`, `import`, `mv`, `cp`, `doctor`, and
`SHA256SUMS` dispatch on the payload root and need nothing new. `hydrate`
and `run` refuse a code bundle by name and say what to do instead;
`smoke` records nothing rather than pretending a tokenizer test applies.
`info` shows the description, languages, declarations, and the primary
model; `list` shows the type. A catalog row can hold a repository like
any other source ref.

## 9. What is recorded, and what is not

- **Submodules are named, not fetched.** Each is its own repository at
  the recorded commit; archive it as its own bundle.
- **Symbolic links are stored as git stores them** — a file holding the
  link target — and listed in `code_metadata.symlinks`.
- **No `.git` directory.** The bundle is the tree at one commit, not the
  history; the commit SHA and the upstream URL are how to reach the
  history while it exists.
- **A tree GitHub cannot list in one call is refused**, not pinned
  partially. Very large repositories wait for a paged tree walk.
- **Release assets and the LFS batch API are not read yet.** A repository
  that publishes weights as release attachments is a later step; one that
  keeps them in LFS is archived in full today, by way of the media host.
- **GitHub only, for now.** A second git host is another provider, and a
  repository on it is a code bundle by the same sentence.
- **A reference is a string, never a claim about run time.** Comments and
  docstrings are prose wherever they sit; a tree's link to itself is not
  a reference; a name the Hub does not know that the tree also links as a
  GitHub repository is that repository. The general problem stays
  unsolved on purpose.

## 10. Where this is going

The code type is the first stone of a layer that presents archived
bundles to a runtime on a named machine — a workbench — on existing
standards rather than new ones: the Hugging Face cache layout to present
a model, a compose file to declare a harness, an OCI digest to pin the
image, an OpenAI-compatible endpoint as proof of life. The binding "this
tree serves that checkpoint in that image" is a composition, not a fact
about any one artifact's bytes, so it lives outside the manifests — the
`references` edge says the tree *names* the model; the workbench says it
*ran* it.

A recipe like the case study is five kinds of glue in one launcher, and
most of it generalizes: fetching, presenting the model, choosing an
image, budgeting memory, probing an endpoint. One layer does not — the
patches that close the gap between what the engine supported on the day
and what this model on this hardware needed — and there will always be
one, because new models ship ahead of engines, new hardware ahead of
kernels. The workbench runs the stock case itself and runs a recipe's
harness for the rest; the archive is where the snowflake earns its keep.
The layer table, the harness contract, and the phases are in
[the workbench proposal](proposals/workbench.md).

## Case study: MiaAI-Lab/Qwen3.8-Flash-Next-Single-DGX-Spark

Pinned 2026-09-05 at `09d4424be2b7`: 23 files, 270 KiB, AGPL-3.0. Python
66%, Shell 34%. Declares an env template and three root launchers. No
fork parent; 14 forks at archive time. `verify` passes, `sha256sum -c
SHA256SUMS` passes, two exports are byte-identical. The scan reads 22
text files and finds one declared image, one model named in code that
resolves — the primary, recorded as a `references` edge — and four
mentions. Beside it in a vault sits
`huggingface:Mia-AiLab/Qwen3.8-Flash-Next-NVFP4` — the 98.7 GiB
checkpoint the recipe's `download.sh` fetches at whatever `main` is that
day, itself declared a quantization of `Qwen/Qwen3.8-Flash-Next`. Two of
the three things a future reader needs to stand this model up on one DGX
Spark; the third, the container image, is a tag in the record and an OCI
digest in `curation.md` until the workbench gives it a home.

---

[Documentation index](README.md)

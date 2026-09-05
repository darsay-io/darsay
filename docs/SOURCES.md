<p align="center">
  <a href="GETTING-STARTED.md">Start here</a> ·
  <a href="CONCEPTS.md">Concepts</a> ·
  <a href="../examples/README.md">Examples</a> ·
  <a href="README.md">All docs</a> ·
  <a href="../README.md">README</a>
</p>

# Source providers

> **In one sentence.** `estimate` and `archive` take a source ref.
> Hugging Face is the first provider and GitHub the second; neither is the product.

Acquisition is a plugin. The archive format, the vault, and the CLI verbs do
not belong to Hugging Face. Hugging Face is the first **source provider**:
it pins a revision, lists files, and fetches bytes. A second provider is a
new class plus a registry line; it is not a new `archive` flag.

## Public address grammar

`estimate` and `archive` take one argument, a **source ref**:

```
<provider>:<locator>
<provider>://<locator>
https://<provider-host>/...
```

Examples that all resolve to the same Hugging Face model:

```
huggingface:Qwen/Qwen3-0.6B
hf:Qwen/Qwen3-0.6B
https://huggingface.co/Qwen/Qwen3-0.6B
Qwen/Qwen3-0.6B                          # Hugging Face shorthand
```

Datasets on that provider:

```
huggingface:datasets/owner/name
datasets/owner/name                      # Hugging Face shorthand
https://huggingface.co/datasets/owner/name
```

GitHub repositories, archived as **code** bundles ([Code bundles](CODE.md)):

```
github:owner/repo
gh:owner/repo
https://github.com/owner/repo
```

The default revision is `HEAD` — the repository's default branch,
whatever it is called — and `--revision` takes a branch, tag, or commit.
A URL that buries the revision (`…/tree/v1.2`, `…/commit/abc…`,
`…/releases/tag/v1`) is refused with the `--revision` command that says
the same thing unambiguously; a deep link to an issue or a pull request
resolves to the repository. `GITHUB_TOKEN` (or `GH_TOKEN`) in the
environment reads private repositories and lifts the unauthenticated API
allowance.

Unprefixed `owner/name` and `datasets/owner/name` stay as Hugging Face
shorthand so existing commands keep working. They are convenience, not the
canonical form. Canonical addresses are always `huggingface:<locator>`
(with the `datasets/` prefix when the artifact is a dataset). Pin and
estimate expand an unprefixed `owner/name` that exists only as a dataset
to that canonical; if both namespaces have a repo, unprefixed stays a
model.

A source string whose scheme is not a registered provider is an error, not
a silent Hugging Face parse. Adding ModelScope later is
`modelscope:qwen/Qwen-7B` (or that host's URL) with no CLI change.

## Home URLs: closed works

A catalog may also hold a work darsay cannot fetch — an API-only model,
an announced release, a host with no provider yet — by its **home URL**
(`https://www.qwencloud.com/models/qwen3.8-max-0902`). `estimate` and
`archive` refuse such an address (there is nothing to fetch); `catalog
add` accepts it as a **closed** work that holds its place in its family
([Catalogs](CATALOGS.md#entry), [Concepts → closed works](CONCEPTS.md#closed-works)).
The last path segment is read as the work's name, so the closed row
lands in the same family and generation as its open siblings. When the
weights are published, the address becomes a source ref.

## What a provider owns

The interface is `SourceProvider` in `src/darsay/providers/base.py`.
Each backend implements:

| Method | Role |
|---|---|
| `parse` / `parse_url` | Locator → `SourceRef` (canonical address, URL, vault directory name, artifact type) |
| `pin` | Moving ref → immutable revision + file inventory + JSON-safe metadata |
| `download_file` | One payload file into the bundle, including the provider's auth/Range/retry |
| `transient_network_error` | Classify a `download_file` failure worth waiting out (`"DNS lookup failed"`, `"connection reset"`) versus a real error; the base class reads the OS-level cause chain, a plugin adds its transport library's types |
| `transfer_session` | Optional wrap around a transfer run (resume semantics, caches); receives the operator's `max_rate` so the transport can pace smoothly |
| `variants` | `estimate --variants` (or `None`) |
| `declared_parents` | The parents a pin's metadata already declares — what `estimate` shows without another query |
| `exists` | One cheap lookup: does this locator exist upstream — true, false, or "cannot say"; how a code bundle's references are resolved, never a listing |
| `lineage` | Parents as upstream declares them, and a best-effort snapshot of descendants at register time |
| `access_record` | Gate / authorization notes for the manifest |

Transfer bookkeeping — pin, reconcile, budgets, sibling-blob reuse, assemble —
stays in `transfer.py` and does not import a hosting-service client.
Hydration, verify, export, and the payload layout never see the provider.

## Hugging Face specifics that stay in the plugin

Hub address grammar, `huggingface_hub`, LFS vs git-blob digests, gated-repo
auth, `base_model:*` / `dataset:` listings, Xet-disable + Range resume,
httpx error classification, chunk sizing under a rate cap, and the payload
`.cache/huggingface/` partials all live in
`src/darsay/providers/huggingface.py`. Reconnect scheduling, the token
bucket, and the panel's outage states are provider-neutral and live in
`transfer.py` / `progress.py`.

## GitHub specifics that stay in the plugin

The REST pin (repository, commit, recursive tree, languages), raw-content
and LFS-media URLs, `.gitattributes` parsing and pointer resolution, Range
resume with a restart when the host answers `200`, the bearer token from
`GITHUB_TOKEN` / `GH_TOKEN`, the rate-limit refusal that names the reset
time, and `urllib` error classification all live in
`src/darsay/providers/github.py`. It needs only the standard library: no
new dependency came with the second provider.

Hugging Face bundle directory names are **unchanged**
(`owner--name`, `datasets--owner--name`) so existing vaults resume. A later
provider includes its id in `SourceRef.bundle_name` so locators cannot
collide across hosts — GitHub bundles are `github--owner--repo`.

## Adding a provider

1. Subclass `SourceProvider` in `src/darsay/providers/<name>.py`.
2. Register it in `sources._ensure_providers`.
3. Declare `url_hosts` if the provider has a web URL people will paste.
4. Do not add a `--provider` flag or a new archive subcommand.

The core dependency on `huggingface_hub` belongs to the Hugging Face
plugin, not to the archive format; the GitHub plugin needs only the
standard library. Optional extras are unchanged.

## Manifest

The record stores `source.provider` and `source.address` alongside
`origin` / `repo_id` / `upstream_url`. `origin` is the hosting service
id (`"huggingface"` for Hub archives, `"github"` for repositories). See
[MANIFEST.md](MANIFEST.md).

---

[Documentation index](README.md)

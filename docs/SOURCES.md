> [Documentation](README.md) · [Project README](../README.md)

# Source providers

Acquisition is a plugin. The archive format, the vault, and the CLI verbs do
not belong to Hugging Face. Hugging Face is the first **source provider**:
it pins a revision, lists files, and fetches bytes. A second provider is a
new class plus a registry line; it is not a new `archive` flag.

Implemented in v0.6.0, manifest schema 1.5.0 (additive).

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

Unprefixed `owner/name` and `datasets/owner/name` stay as Hugging Face
shorthand so existing commands keep working. They are convenience, not the
canonical form. Canonical addresses are always `huggingface:<locator>`
(with the `datasets/` prefix when the artifact is a dataset).

A source string whose scheme is not a registered provider is an error, not
a silent Hugging Face parse. Adding ModelScope later is
`modelscope:qwen/Qwen-7B` (or that host's URL) with no CLI change.

## What a provider owns

The interface is `SourceProvider` in `src/modelvault/providers/base.py`.
Each backend implements:

| Method | Role |
|---|---|
| `parse` / `parse_url` | Locator → `SourceRef` (canonical address, URL, vault directory name, artifact type) |
| `pin` | Moving ref → immutable revision + file inventory + JSON-safe metadata |
| `download_file` | One payload file into the bundle, including the provider's auth/Range/retry |
| `transfer_session` | Optional wrap around a transfer run (resume semantics, caches) |
| `variants` | `estimate --variants` (or `None`) |
| `relationships` | Best-effort ecosystem snapshot at register time |
| `access_record` | Gate / authorization notes for the manifest |

Transfer bookkeeping — pin, reconcile, budgets, sibling-blob reuse, assemble —
stays in `transfer.py` and does not import a hosting-service client.
Hydration, verify, export, and the payload layout never see the provider.

## Hugging Face specifics that stay in the plugin

Hub address grammar, `huggingface_hub`, LFS vs git-blob digests, gated-repo
auth, `base_model:*` / `dataset:` listings, Xet-disable + Range resume, and
the payload `.cache/huggingface/` partials all live in
`src/modelvault/providers/huggingface.py`.

Hugging Face bundle directory names are **unchanged**
(`owner--name`, `datasets--owner--name`) so existing vaults resume. A later
provider includes its id in `SourceRef.bundle_name` so locators cannot
collide across hosts.

## Adding a provider

1. Subclass `SourceProvider` in `src/modelvault/providers/<name>.py`.
2. Register it in `sources._ensure_providers`.
3. Declare `url_hosts` if the provider has a web URL people will paste.
4. Do not add a `--provider` flag or a new archive subcommand.

The core dependency on `huggingface_hub` remains until a second provider
ships; it is a dependency of the Hugging Face plugin, not of the archive
format. Optional extras are unchanged.

## Manifest

Schema 1.5.0 records `source.provider` and `source.address` alongside the
existing `origin` / `repo_id` / `upstream_url`. `origin` is the provider
id (still `"huggingface"` for Hub archives). Older manifests without the
new fields remain valid; readers fall back to `origin` + `repo_id`.
See [MANIFEST.md](MANIFEST.md).

---

[Documentation index](README.md)

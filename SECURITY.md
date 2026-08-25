# Security

modelvault copies untrusted upstream files (weights, tokenizers, datasets,
and whatever else a Hub repo contains) onto local disk and, during
`hydrate` / `run`, may load them into inference libraries. Treat payload
bytes as untrusted. A passing `verify` means the bytes match the recorded
hashes, not that they are safe to execute.

## Reporting a vulnerability in the tool

Please use GitHub's private vulnerability reporting on this repository
(Security → Advisories → New advisory). Do not open a public issue for a
tool vulnerability until it is fixed or we ask you to.

## What is in scope

- Integrity bugs: `verify` / `import` accepting a payload that does not
  match the manifest or marker hash.
- Path traversal or untrusted-tar extraction on `import`.
- Ledger or transfer bugs that could mix bytes from the wrong pin into a
  registered bundle.
- Supply-chain issues in the release artifacts (wheel / sdist).

## What is out of scope

- Harmful or gated content that an upstream repo already publishes.
- Model-weight "malware" that only manifests when loaded by torch /
  transformers / llama.cpp — report that upstream.
- Missing features (signing, provenance attestations) unless an existing
  claim is false.

# Design rationale — implementation choices

Why modelvault is written in Python, and why the archive's longevity does
not depend on that choice. Decision reviewed 2026-08 at v0.3.0 (~2,600
lines, one runtime dependency); revisit criteria at the end.

## Why Python

**Hydration is married to the Python ecosystem.** The runner scripts
(`runners/`) execute inside hydrated envs against torch, transformers, and
llama-cpp-python — Python APIs with no serious equivalent elsewhere. A
rewrite in Go/Rust would move only the orchestrator: the runners stay
Python, putting a language boundary through the cleanest abstraction in the
tool (the `ENGINES` registry → runner contract). Planned work deepens the
dependency — quantize recipes mean bitsandbytes load flags,
`mlx_lm.convert`, `convert_hf_to_gguf.py`, all Python-side (see
[QUANTIZATION.md](QUANTIZATION.md)).

**`huggingface_hub` is the reference client.** The riskiest code an
archival tool could own is a hand-rolled reimplementation of snapshot
semantics — revision pinning, LFS vs git-blob hashing, gated-repo auth,
sibling metadata — because a silent bug there corrupts the core claim
("byte-exact copy of upstream"). Python gets the canonical, maintained
implementation, and the metadata surface `estimate` and `archive` lean on
(`files_metadata`, `safetensors.parameters`, `base_model:quantized`
relations) comes with it for free.

**Performance is not the bottleneck.** The workload is network download and
disk IO, then hashing. `hashlib` releases the GIL and runs OpenSSL's
hardware-accelerated SHA-256; the optional `blake3` package is the same
Rust core a Rust rewrite would link. Archiving a 55 GB model spends tens of
minutes downloading; a compiled language saves seconds of that. If hashing
ever matters, parallelizing shard hashing with a thread pool (the GIL is
released during hashing; 3.14 free-threading exists) is a small change, not
a rewrite.

**The code's shape is Python's sweet spot.** The tool is glue over network,
filesystem, subprocess, and JSON; the stdlib supplies tar, hashing, and
venv creation. Features land small — `estimate` was one module and thirty
lines of CLI.

## Accepted costs

- The tool needs a Python to run, and hydration needs an interpreter to
  build env interpreters (`$MODELVAULT_PYTHON` / `--python`; `uv`, when
  present, can fetch interpreters itself).
- Distribution is `pip install`, not a single static binary. See below for
  why that does not threaten the archive.

## Longevity: formats outlive tools

"Museum-grade" must not mean "hope the tool still runs in 2040." Longevity
is deliberately carried by the artifacts, not the software:

- the manifest is plain JSON, documented field-by-field
  ([MANIFEST.md](MANIFEST.md));
- the export is a plain uncompressed tar with a documented layout and a
  manual-recovery procedure that needs no modelvault
  ([MVB-FORMAT.md](MVB-FORMAT.md));
- the payload is a pristine upstream snapshot any HF-compatible loader uses
  directly.

The tool is replaceable; the bundles are not — so the engineering
investment goes into keeping the formats self-describing, not into making
the binary immortal.

### Proposed: standalone verifier

A single-file, stdlib-only script (no `huggingface_hub`, no `blake3`) that
re-hashes a payload against its manifest — runnable anywhere any Python
exists, decades on. It could ship inside exports, with two constraints:
adding it to the tar is an MVB format minor bump, and the script itself
must be byte-stable across releases or export determinism breaks.

## When to revisit

Only when a component appears whose profile is genuinely different — a
long-running vault server, or a mass-distributed verifier where a static
binary is the point. Write *that component* in Go/Rust; do not port the
tool.

<p align="center">
  <a href="GETTING-STARTED.md">Start here</a> ·
  <a href="CONCEPTS.md">Concepts</a> ·
  <a href="../examples/README.md">Examples</a> ·
  <a href="README.md">All docs</a> ·
  <a href="../README.md">README</a>
</p>

# Design rationale — implementation choices

> **In one sentence.** Python because hydration and Hub snapshot semantics
> live there. Longevity lives in the formats, not the binary.

Why darsay is written in Python, and why the archive's longevity does
not depend on that choice. Revisit criteria at the end.

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

**Acquisition is a provider plugin; Hugging Face is the first one.**
The riskiest code an archival tool could own is a hand-rolled
reimplementation of snapshot semantics — revision pinning, LFS vs git-blob
hashing, gated-repo auth, sibling metadata — because a silent bug there
corrupts the core claim ("byte-exact copy of upstream"). The Hugging Face
provider uses `huggingface_hub` as the reference client for that host, and
the metadata surface `estimate` and `archive` lean on
(`files_metadata`, `safetensors.parameters`, `base_model:quantized`
relations) comes with it for free. The public API is a source ref
(`huggingface:Qwen/Qwen3-0.6B`); a second host is another `SourceProvider`,
not a new CLI. See [SOURCES.md](SOURCES.md).

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
  build env interpreters (`$DARSAY_PYTHON` / `--python`; `uv`, when
  present, can fetch interpreters itself).
- Distribution is a pure-Python wheel (`pipx` / `uvx` / `pip`), not a
  single static binary. Frozen executables are possible but a poor
  primary path for this CLI — see [DISTRIBUTION.md](DISTRIBUTION.md).
  That choice does not threaten the archive.

## Longevity: formats outlive tools

"Museum-grade" must not mean "hope the tool still runs in 2040." Longevity
is deliberately carried by the artifacts, not the software:

- the manifest is plain JSON, documented field-by-field
  ([MANIFEST.md](MANIFEST.md));
- the export is a plain uncompressed tar with a documented layout and a
  manual-recovery procedure that needs no darsay
  ([MVB-FORMAT.md](MVB-FORMAT.md));
- the payload is a pristine upstream snapshot any HF-compatible loader uses
  directly.

The tool is replaceable; the bundles are not — so the engineering
investment goes into keeping the formats self-describing, not into making
the binary immortal.

### Standalone verifier

A single-file, stdlib-only script (`src/darsay/standalone_verify.py`: no
`huggingface_hub`, no `blake3`, no darsay imports) re-hashes a payload
against its manifest — runnable anywhere any Python exists, decades on.

Every export copies that file verbatim into the tar as `darsay-verify.py`
(MVB format **1.2**). After unpack:

```bash
python3 darsay-verify.py .                  # bundle directory
python3 darsay-verify.py bundle.mvb.tar     # the tar itself, no unpack
```

It is read-only: it never writes `manifest.json` or `verification.json`.
`darsay verify` remains the tool that records a check in the bundle.

The script is part of the export format. Changing it is an MVB minor bump;
the same bundle state under the same format version still exports the
same bytes.

## When to revisit

Only when a component appears whose profile is genuinely different — a
long-running vault server, or a mass-distributed verifier where a static
binary is the point. Write *that component* in Go/Rust; do not port the
tool.

---

[Documentation index](README.md)

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
hardware-accelerated SHA-256 (3.3 GB/s per thread measured; four threads
scale to 12.8 GB/s); the optional `blake3` package is the same Rust core a
Rust rewrite would link. Archiving a 55 GB model spends tens of minutes
downloading; a compiled language saves seconds of that. Digests overlap
downloads and `--jobs` streams files in parallel — with threads, not a
rewrite. See [Transfer concurrency](#transfer-concurrency) for the
measurements and the Xet question.

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

## Transfer concurrency

Why the fetch → hash → commit pipeline in `transfer.py` is Python threads
and not a Go/Rust core. The question was asked in earnest when a 703 GiB
archive read `stalled` for minutes while it digested a 5 GiB shard; the
answer rests on measurements, not on taste.

### What was measured

18-core Apple Silicon, Python 3.14, OpenSSL-backed `hashlib`, August 2026:

| | |
|---|---|
| `hashlib.sha256`, one thread, 1 MiB updates | 3.3 GB/s |
| four threads hashing at once | 12.8 GB/s aggregate — the GIL is released inside OpenSSL |
| `hash_file` as `archive` calls it (SHA-256 + git blob SHA-1, per-MiB panel callbacks) | 1.45 GB/s — a 5 GiB shard in about 4 s |
| uncached read from the vault SSD, 1 MiB reads | 4.1 GB/s |

The minutes in that report came from a slower volume, not from Python: the
panel showed at most 140 MB/s during the digest, which is a disk-read
figure. Socket reads and file writes release the GIL too. So a Python
thread that hashes while another downloads gets the whole benefit a
compiled core would, for this workload.

### What is parallel, and what is not

- **Overlap, not per-file parallel hashing.** SHA-256 is sequential per
  stream in every language; no core can split one file's digest across
  cores. (BLAKE3 can, being a tree hash — the optional `blake3` package is
  the same Rust core, with `max_threads`.) What the pipeline buys is
  overlap: the network never idles for a digest, and one hash thread at
  1.5–3 GB/s outruns any link this tool will meet.
- **Per-file streams, not per-file range requests.** `--jobs` runs N files
  as N single HTTP streams. Splitting one file into parallel ranges is what
  `hf_xet` does, in Rust; darsay does not reimplement it (see below).
- **The ledger is single-writer.** Workers fetch and hash; only the main
  thread commits, so `transfer.json` never sees two writers and the
  invariants in [INCREMENTAL.md](INCREMENTAL.md) hold without new locking.
- **Backpressure.** If the disk cannot keep up with the network (a network
  volume, a USB drive), fetching pauses at `--jobs` + 1 files awaiting
  digest. Otherwise a fast link could run gigabytes ahead of the ledger,
  and a Ctrl-C would leave all of it to re-hash.

What a compiled core would add: nothing on the hash (OpenSSL is the
ceiling), nothing on I/O (the syscalls already release the GIL), and only
in-file range parallelism on the network — which `hf_xet` provides
already. What it would cost: a second toolchain, platform wheels instead of
a pure-Python one ([DISTRIBUTION.md](DISTRIBUTION.md)), and an FFI seam
through the most invariant-dense module in the tool.

### Xet

Xet is Hugging Face's replacement for Git LFS storage. LFS keeps a file as
one blob, fetched with one HTTP stream; Xet splits files into
content-defined chunks (about 64 KB), deduplicates them, packs them into
~64 MB xorbs in a content-addressed store, and describes a file as a list
of chunk hashes. A download fetches that reconstruction plan, then pulls
the byte ranges it needs in parallel, verifying each chunk. `hf_xet` is
the PyPI package that carries the Rust client; `huggingface_hub` uses it on
its own when it is installed and the repo is Xet-backed, which most are
since 2025. It gives in-file parallelism, chunk-level dedup across
revisions and repos, and integrity checks during transfer.

darsay switches it off for the transfer session (`HF_HUB_DISABLE_XET=1`
in the provider's `transfer_session`) because, when this was validated at
v0.5.0, an aborted Xet download — a byte budget, a Ctrl-C — discarded its
in-flight reconstruction: nothing durable to resume from. HTTP Range
leaves a byte-exact `.incomplete` that survives restarts and an `rsync` to
another vault. "Partial bytes are authoritative and portable" outranked
throughput.

An opt-in (`--xet`, or a config key) would leave Xet on for people on a
fast, reliable link who run without budgets. Before shipping one, verify
against the installed `hf_xet`:

1. An aborted download leaves reusable state in the chunk cache (darsay
   already points `HF_XET_CACHE` inside the payload's `.cache/`), so a
   restart skips the chunks it already has.
2. `partial_bytes` and the transfer plan can bank Xet's bytes — today they
   read the `.incomplete` size, which Xet does not grow monotonically.
3. The meter's Xet handling (`_darsay_xet`; `update_transfer` reporting
   network bytes apart from reconstruction) still matches the installed
   `huggingface_hub`.
4. The byte budget stays honest across parallel range gets.

The case is weaker than it was: `--jobs` streams already fill a link for
any multi-shard model. Xet's in-file parallelism matters for a model that
is one giant file, and its chunk-level dedup is finer than darsay's
file-level sibling-blob reuse — real, but narrow.

## Longevity: formats outlive tools

"Museum-grade" must not mean "hope the tool still runs in 2040." Longevity
is deliberately carried by the artifacts, not the software:

- the manifest is plain JSON, documented field-by-field
  ([MANIFEST.md](MANIFEST.md));
- the export is a plain uncompressed tar with a documented layout and a
  manual-recovery procedure that needs no darsay
  ([MVB-FORMAT.md](MVB-FORMAT.md));
- the payload preserves selected upstream files byte-for-byte; compatible
  loaders use it when their required files and paths are present
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

### Records move forward; readers do not move back

Every reader reads one schema major. That is what keeps the readers
simple and the manifest honest: no field describes how things used to
be, and no code path quietly reinterprets an old shape. When the model
of the models changes, the record changes shape — and a record written
under the old shape is a complete set of facts about a payload that has
not changed, so the recovery is to re-read those facts under the new
model, not to download the payload again.

`darsay migrate` is that re-reading, and it is deliberately not a chain
of patches. It carries every fact whose meaning did not change exactly
as recorded, re-derives from the payload the facts whose meaning did
(the precision label, bytes per parameter), re-reads the name for the
fields the name grammar now supplies, and translates the sections the
model renamed. The test that holds it is not "the patch applied" but
"the migrated record equals a fresh archive of the same source" — with
archive-time facts (timestamps, the downloader, the descendants
snapshot) kept as the older record had them, because those are facts
about *that* archive. The reading of an older major lives in one
module, `migrate.py`; the day 3.0 comes, that module gains a 2.x
reading and a 1.x record takes both steps. `archive.migrations` records
that it happened, and `verify` remains the verb that reads the bytes.

## When to revisit

Only when a component appears whose profile is genuinely different — a
long-running vault server, or a mass-distributed verifier where a static
binary is the point. Write *that component* in Go/Rust; do not port the
tool.

Xet: when the four checks under [Transfer concurrency](#xet) pass against
the installed `hf_xet` — and then as an opt-in, never the default for a
budgeted or interruptible transfer.

---

[Documentation index](README.md)

<h1 align="center">
  <img src="docs/darsay-logo.png" alt="darsay" width="880">
</h1>

<p align="center">
  <strong>Keep a model forever. Run it tomorrow.</strong>
</p>

```bash
pipx install darsay

darsay archive Qwen/Qwen3-0.6B
darsay run     qwen--qwen3-0.6b "Say hello"
```

<p align="center">
  A pinned snapshot of the Hub — hashed, licensed, and still loadable as-is.<br>
  The Hub is a living website. A darsay bundle is a museum piece that still runs.<br>
  <sub>Python 3.10+ · macOS and Linux · Apache 2.0</sub>
</p>

<p align="center">
  <a href="https://pypi.org/project/darsay/"><img src="https://img.shields.io/pypi/v/darsay?style=flat-square&color=22d3ee" alt="PyPI"></a>
  <a href="https://github.com/jeremynorris/darsay/actions/workflows/ci.yml"><img src="https://img.shields.io/github/actions/workflow/status/jeremynorris/darsay/ci.yml?style=flat-square&label=CI" alt="CI"></a>
  <a href="https://github.com/jeremynorris/darsay/blob/main/LICENSE"><img src="https://img.shields.io/badge/license-Apache%202.0-38bdf8?style=flat-square" alt="Apache 2.0"></a>
  <img src="https://img.shields.io/badge/python-3.10%2B-00b4ff?style=flat-square&logo=python&logoColor=white" alt="Python 3.10+">
  <img src="https://img.shields.io/badge/schema-v1.6.0-0ea5e9?style=flat-square" alt="Manifest schema 1.6.0">
</p>

<p align="center">
  <a href="docs/GETTING-STARTED.md">Start here</a> ·
  <a href="docs/CONCEPTS.md">Concepts</a> ·
  <a href="examples/README.md">Examples</a> ·
  <a href="docs/README.md">All docs</a> ·
  <a href="CONTRIBUTING.md">Contributing</a>
</p>

---

## The idea in four lines

You have a **vault**. It holds **bundles**.

Each bundle is one **pinned revision** of one source: an immutable payload
(`model/` or `data/`), a `manifest.json` of recorded facts, and one file
you write by hand (`curation.md`).

`archive` puts the snapshot in the vault. `run` speaks to it **offline**.
`export` packs it into a single `.mvb.tar` that any `tar` can open in 2040.

The payload never changes. The Hub can.

```
  Hugging Face                         your disk
  ────────────                         ─────────
  Qwen/Qwen3-0.6B                      vault/
       │                                 │
       │  darsay archive                 │
       └──────────────►──────────────────┤
                                         │
                          qwen--qwen3-0.6b/<rev>/
                          ├── model/           frozen snapshot
                          ├── manifest.json    recorded facts
                          └── curation.md      your notes
                                         │
                          darsay run ────┘──► tokens, offline
```

| On the Hub | In a bundle |
|---|---|
| A living website | A pinned git revision |
| Can be gated, rewritten, deleted | Payload is immutable after archive |
| “latest” is a moving target | The manifest records what was true |
| You hope it still loads | `darsay run` is offline proof |

New here? Take the [guided first bundle](docs/GETTING-STARTED.md)
(~five minutes with a tiny model). Want the mental model in full?
[Concepts](docs/CONCEPTS.md). Want copy-paste recipes?
[Examples](examples/README.md).

## Install

Requires Python 3.10+. Isolated CLI tools are the intended way to run a
release; see [distribution](docs/DISTRIBUTION.md).

```bash
pipx install darsay
# or, no install at all:
uvx darsay --help
# or:
uv tool install darsay
```

<details>
<summary><strong>Homebrew tap</strong> — personal tap, not homebrew/core</summary>

Unqualified `brew install darsay` will not find it.

```bash
brew install jeremynorris/darsay/darsay
```

</details>

<details>
<summary><strong>Development checkout</strong></summary>

```bash
python3 -m venv .venv
.venv/bin/pip install -e .                      # core: huggingface_hub only
.venv/bin/pip install -e ".[fast-hash,smoke]"   # + blake3, tokenizers
.venv/bin/pip install -e ".[inference]"         # + transformers/torch for in-process smoke
.venv/bin/pip install -e ".[datasets]"          # + pyarrow for measured dataset row counts
.venv/bin/pip install -e ".[dev]"               # pytest
.venv/bin/pytest                                # unit + integration; see docs/TESTING.md
```

The extras only serve in-process smoke tests. `darsay run` needs none of
them — hydration builds its own isolated env per engine.

</details>

The vault root defaults to `./vault` (override with `--vault` or
`$DARSAY_HOME`). Bundles are gitignored — they live on disk or in your
backup tier, not in this repo.

## The three verbs

**1. Estimate** — price the source from Hub metadata. Nothing downloaded.

```bash
darsay estimate Qwen/Qwen3.8-27B
```

```
Qwen/Qwen3.8-27B @ main -> 1d4bf0f2ff60
  image-text-to-text | license apache-2.0
  parameters:   27.78B BF16  [upstream safetensors metadata]
  payload:      32 files, 51.8 GiB
  engines:      transformers
  completeness: complete
  bundle:       vault/qwen--qwen3.8-27b/1d4bf0f2ff60  (new)
  disk:         needs ~55.5 GiB, free 1022.6 GiB — OK

To archive: darsay archive Qwen/Qwen3.8-27B
```

Exits non-zero when free space is insufficient, so it doubles as a guard
in scripts. `--variants` lists the quantized ecosystem.
`--include '*Q4_K_M*'` prices one file inside a huge GGUF pack.

**2. Archive** — pin a revision, fetch bytes, hash them, write the manifest.

```bash
darsay archive Qwen/Qwen3-0.6B
```

Ctrl-C is fine. Budgets are fine. The same command resumes:

```bash
darsay archive Qwen/Qwen3.8-27B --max-gb 10     # tonight
darsay archive Qwen/Qwen3.8-27B --max-gb 10     # tomorrow
darsay archive Qwen/Qwen3.8-27B                 # finish, verify, register
```

**3. Run** — isolated env, fully offline, payload untouched.

```bash
darsay run qwen--qwen3-0.6b "Say hello"
```

Or skip the tool and point any Hugging Face-compatible loader at the
payload:

```python
from transformers import AutoModelForCausalLM, AutoTokenizer

path = "vault/qwen--qwen3-0.6b/<rev>/model"
tok = AutoTokenizer.from_pretrained(path)
model = AutoModelForCausalLM.from_pretrained(path)
```

No unpacking. No conversion. The archived files *are* the model.

## A bundle

```
vault/qwen--qwen3-0.6b/<revision12>/
├── model/              # immutable payload: pristine snapshot of the upstream repo
├── manifest.json       # machine-readable record — the source of truth
├── README.md           # human-readable summary, generated from the manifest
├── VERIFICATION.md     # latest verification report
├── verification.json   # verification history (last 50 runs)
├── curation.md         # curator's notes — the only hand-edited file
├── exports.json        # log of single-file exports (after first export)
├── hydration.json      # runnable-env record + run history (after first hydrate)
├── transfer.json       # disposable resumable-transfer ledger
├── transfer.lock       # transient writer lock (only during archive/assemble)
└── LICENSE             # upstream license text, surfaced at the root
```

The payload under `model/` (or `data/` for datasets) is **immutable after
archiving**; the bundle hash covers it alone.

<details>
<summary><strong>What the manifest records</strong></summary>

| Section | Contents |
|---|---|
| `identity` | name, family, publisher, version, release date, bundle id |
| `source` | provider, address, pinned commit, transfer accounting, mirrors, signatures, popularity + tags at archive time |
| `licensing` | SPDX id, license files, commercial / redistribution / modification / attribution flags, patent grant, trademark terms |
| `inventory` | per-file size + SHA-256 (+BLAKE3), upstream checksum match, deterministic bundle hash |
| `model_metadata` | parameter count by dtype (from safetensors headers — no torch), architecture, context, tokenizer, languages |
| `runtime` | engines from shipped formats, estimated min RAM/VRAM, measured hardware from `darsay run` |
| `validation` | checksum verification, completeness, tokenizer + inference smoke tests |
| `relationships` | parents, finetunes, known quantizations + GGUF repos (snapshot at archive time) |
| `archive` | date, host, storage tier, backups, last integrity check |
| `security` | integrity status, unexpected-change flags, trust level |
| `curation` | historical significance, capabilities, limitations, notes (via `curation.md`) |

`schema_version` is recorded in every manifest. Field-by-field reference:
[docs/MANIFEST.md](docs/MANIFEST.md).

</details>

## Commands

| You want to… | Type |
|---|---|
| Price a source, download nothing | `darsay estimate Qwen/Qwen3.8-27B` |
| Keep it | `darsay archive Qwen/Qwen3-0.6B` |
| Talk to it | `darsay run qwen--qwen3-0.6b "Say hello"` |
| See what you have | `darsay list` (id + path) |
| Re-hash and compare | `darsay verify qwen--qwen3-0.6b` |
| Pack one file for a USB drive | `darsay export qwen--qwen3-0.6b -o /backups` |
| Bring that file back | `darsay import /backups/<file>.mvb.tar` |

Source refs are provider-qualified — `huggingface:Qwen/Qwen3-0.6B`,
`huggingface:datasets/owner/name`. Unprefixed `owner/name` /
`datasets/owner/name` and huggingface.co URLs are Hugging Face shorthand.

<details>
<summary><strong>The rest of the CLI</strong></summary>

```bash
darsay estimate Qwen/Qwen3.8-27B --variants
darsay estimate unsloth/Qwen3.8-27B-GGUF --include '*Q4_K_M*'
darsay archive  unsloth/Qwen3.8-27B-GGUF --include '*Q4_K_M*'
darsay estimate datasets/saidutta69/fable-5-premium
darsay archive  datasets/saidutta69/fable-5-premium
darsay archive  Qwen/Qwen3.8-27B --max-gb 10          # pause; rerun to resume
darsay archive  Qwen/Qwen3.8-27B --dry-run            # verified / partial / missing
darsay archive  Qwen/Qwen3.8-27B --shard 1/3 --max-gb 20
darsay --vault ./combined assemble /usb/alice/<bundle> /usb/bob/<bundle>
darsay smoke    <bundle> [--inference]
darsay info     <bundle>                              # path, id, or unique prefix
darsay regen    <bundle>                              # rebuild README after editing curation.md
darsay hydrate  <bundle> [--dry-run]
darsay envs [--prune]
darsay dehydrate <bundle>
```

Adding another host is a source provider, not a new CLI:
[docs/SOURCES.md](docs/SOURCES.md).

</details>

## When you need more

<table>
<tr>
<td width="50%" valign="top">

**Pause, resume, share the work.**
Ctrl-C, `--max-gb`, a USB stick, `--shard 1/3`
with a collaborator. Partial bundles are
relocatable. [Incremental transfer](docs/INCREMENTAL.md)
· [recipe](examples/README.md#pause-and-resume-a-large-archive)

</td>
<td width="50%" valign="top">

**Datasets are the same shape.**
Addressed `datasets/owner/name`, payload under
`data/`. Everything else is identical.
[Datasets](docs/DATASETS.md)
· [recipe](examples/README.md#archive-a-dataset)

</td>
</tr>
<tr>
<td width="50%" valign="top">

**Export is a plain tar.**
Deterministic `.mvb.tar`, marker first, no
compression, recoverable with stock `tar`.
[MVB format](docs/MVB-FORMAT.md)
· [recipe](examples/README.md#export-to-a-usb-drive)

</td>
<td width="50%" valign="top">

**Quants are history or cache, never both.**
Archive the master. Archive published quants
that matter. Derive the rest at run time.
[Quantization](docs/QUANTIZATION.md)

</td>
</tr>
</table>

`verify` re-hashes the payload against the manifest and exits non-zero on
drift — suitable for cron. `import` fully re-hashes **before** a bundle
enters the vault; failures register nothing.

`run` hydrates first: picks an engine from what the payload ships
(safetensors → `transformers`, GGUF → `llama-cpp`), builds a dedicated
venv **outside the bundle** under `<vault>/.runtime/`, and infers with
`HF_HUB_OFFLINE=1`. Envs are disposable. Design:
[docs/HYDRATION.md](docs/HYDRATION.md).

## Documentation

| Start here | Then | When you need the spec |
|---|---|---|
| [**Getting started**](docs/GETTING-STARTED.md) | [Concepts](docs/CONCEPTS.md) · [Examples](examples/README.md) | [Documentation home](docs/README.md) |
| [Manifest](docs/MANIFEST.md) | [MVB format](docs/MVB-FORMAT.md) | the two documents a 2040 reader still needs |
| [Hydration](docs/HYDRATION.md) · [Incremental](docs/INCREMENTAL.md) · [Datasets](docs/DATASETS.md) | [Sources](docs/SOURCES.md) · [Quantization](docs/QUANTIZATION.md) | how the verbs actually work |
| [Design](docs/DESIGN.md) | [Distribution](docs/DISTRIBUTION.md) · [Testing](docs/TESTING.md) | why Python, how a release is consumed, what CI keeps |

The tool is replaceable. The formats are not — plain JSON and plain tar,
each documented for recovery without darsay. Full rationale:
[docs/DESIGN.md](docs/DESIGN.md).

## License

Apache License 2.0. See [LICENSE](LICENSE). Bundles record *upstream* model
and dataset licenses separately in `manifest.json`; those do not change the
license of this tool.

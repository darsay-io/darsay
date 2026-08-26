<p align="center">
  <a href="GETTING-STARTED.md">Start here</a> ·
  <a href="CONCEPTS.md">Concepts</a> ·
  <a href="../examples/README.md">Examples</a> ·
  <a href="README.md">All docs</a> ·
  <a href="../README.md">README</a>
</p>

# Start here

Three commands keep a model and talk to it. This page walks through them
with a tiny Hub repo so the first success is measured in seconds, then
shows the same path on a real model.

If you only remember one picture, remember this:

```
source  ──archive──►  vault/bundle  ──run──►  tokens
                         │
                      payload never changes
```

## 1. Install

Python 3.10+, macOS or Linux.

```bash
pipx install darsay
darsay --help
```

No `pipx`? One-shot with no install:

```bash
uvx darsay --help
```

More install paths: [Distribution](DISTRIBUTION.md).

## 2. Price it (optional, free)

`estimate` talks to the Hub API and writes nothing. Use it whenever you
are about to spend disk.

```bash
darsay estimate sshleifer/tiny-gpt2
```

You get a pinned revision, a file count, a size, a disk verdict, and the
exact `archive` command to run next. If free space is short, the command
exits non-zero — it is a script-friendly guard, not just a printout.

## 3. Keep it

```bash
darsay archive sshleifer/tiny-gpt2
```

That pins `main` to a commit, copies every file, hashes them, checks the
hashes against upstream, captures the license, and writes a bundle under
`~/darsay` (override with `--vault` or `$DARSAY_HOME`). When it finishes:

```
Bundle ready: ~/darsay/sshleifer--tiny-gpt2/<rev>
  manifest:     …/manifest.json
  readme:       …/README.md
  verification: …/VERIFICATION.md
  curation:     …/curation.md  <- edit this, then `darsay regen`
```

Find it later with:

```bash
darsay list
darsay info sshleifer--tiny-gpt2
```

`list` prints the bundle id (`name@<rev>`) and a copy-pasteable `PATH`.
`<rev>` is the first 12 characters of the pinned commit. `info`, `run`,
`verify`, and the other bundle commands accept the path, the id, or a unique
prefix (`sshleifer--tiny-gpt2`, `tiny-gpt2`, the revision).

### What just landed on disk

```
~/darsay/sshleifer--tiny-gpt2/<rev>/
├── model/            # the repo, frozen
├── manifest.json     # facts, never guesses
├── README.md         # generated view of those facts
├── curation.md       # yours — the tool will not overwrite this
└── LICENSE
```

Two rules that make the rest of darsay obvious:

1. **Nothing under `model/` is ever modified again.** That is the archive.
2. **Everything the tool writes later lives beside it**, at the bundle
   root — verification reports, hydration records, export logs.

The mental model in full: [Concepts](CONCEPTS.md).

## 4. Use it

Point any Hugging Face-compatible loader at the payload. No conversion:

```python
from transformers import AutoModelForCausalLM, AutoTokenizer

path = "vault/sshleifer--tiny-gpt2/<rev>/model"
tok = AutoTokenizer.from_pretrained(path)
model = AutoModelForCausalLM.from_pretrained(path)
```

Or let darsay build an isolated env and run **offline**
(`HF_HUB_OFFLINE=1`):

```bash
darsay run vault/sshleifer--tiny-gpt2/<rev> "Hello"
```

The tokens will be nonsense — the model is tiny. The point is the path
works. The first `run` downloads engine packages (torch, transformers)
into `<vault>/.runtime/` — outside the bundle, shared with any other
bundle that needs the same env. The payload is not touched. Deleting
the env never deletes the archive.

## 5. A model you would actually keep

Same three verbs. Larger bytes. Identical shape.

```bash
darsay estimate Qwen/Qwen3-0.6B
darsay archive  Qwen/Qwen3-0.6B
darsay run      qwen--qwen3-0.6b Say hello
# or: darsay run qwen--qwen3-0.6b --repl
```

Qwen3-0.6B is about 1.5 GiB. `estimate` tells you before you commit.
If a later source is gated, set `$HF_TOKEN` or run
`huggingface-cli login`.

## 6. The moves you will actually need

| Situation | What to do |
|---|---|
| The download is huge | `darsay archive … --max-gb 10` — exits 10, rerun to resume |
| You hit Ctrl-C | Rerun the same `archive` command. Completed files are kept |
| You only want one GGUF from a pack | `darsay archive REPO --include '*Q4_K_M*'` — prices first with `estimate --include` |
| It is a dataset, not a model | `darsay archive datasets/owner/name` — payload lands in `data/` |
| You want one file for a USB drive | `darsay export <bundle> -o /backups` |
| You want to know it still matches | `darsay verify <bundle>` |
| You wrote curator notes | edit `curation.md`, then `darsay regen <bundle>` |

Copy-paste for each of those: [Examples](../examples/README.md).

## If something feels wrong

- **`No bundles in …/darsay/`** — you passed `--vault` / `$DARSAY_HOME`
  when you archived, or you are looking at a leftover `./vault` from an
  older darsay. The default is `~/darsay`; `list` prints the path it used.
- **Disk verdict `insufficient`** — `estimate` is doing its job. Free
  space, or pick a smaller source.
- **Archive paused with exit code 10** — a budget ran out. Rerun the
  same command; that is success, not failure.
- **`run` wants to install torch** — expected, once, into the shared
  runtime. The bundle itself stays a few files of metadata plus `model/`.
- **Windows** — untested best-effort. macOS and Linux are the supported
  targets.

## You already know enough

A vault is a folder of bundles. A bundle is a pinned snapshot you can
still load. `estimate` prices, `archive` keeps, `run` proves.

Everything else in this repository is a sharpening of that loop:
[resumable transfer](INCREMENTAL.md), [dataset bundles](DATASETS.md),
[offline hydration](HYDRATION.md), [single-file export](MVB-FORMAT.md).
When you want the map of all of it: [Documentation home](README.md).

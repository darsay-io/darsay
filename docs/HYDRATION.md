> [Documentation](README.md) · [Project README](../README.md)

# Hydration — from archived bundle to running model

`modelvault hydrate` turns an archived bundle into a locally runnable install;
`modelvault run` executes a prompt against it. The whole path is designed so
that a bundle that passed `verify` can produce tokens with one command,
without ever touching the archived payload:

```bash
modelvault run vault/qwen--qwen3-0.6b/c1899de289a0            # hello prompt
modelvault run vault/qwen--qwen3-0.6b/c1899de289a0 "2+2=?"    # your prompt
```

`run` hydrates automatically when needed. macOS and Linux are the supported
targets (Windows is untested best-effort). Hydration applies to model
bundles: a dataset bundle matches no engine and `hydrate`/`run` exit with the
"no known engine" message by design — its payload under `data/` is plain
files any reader opens directly (see [DATASETS.md](DATASETS.md)).

## Design rules

1. **The payload stays immutable.** Environments are built *outside* the
   bundle, under `<vault>/.runtime/envs/` (override with `$MODELVAULT_RUNTIME`).
   The only thing hydration writes into the bundle is the bundle-root record
   `hydration.json` — volatile machine-local state, excluded from `.mvb.tar`
   exports exactly like `exports.json`.
2. **Runs are offline.** Runners execute with `HF_HUB_OFFLINE=1` /
   `TRANSFORMERS_OFFLINE=1`, so a passing run is evidence the archived payload
   is self-sufficient — nothing was quietly fetched from the network.
   (Building an env installs packages from PyPI; that is the one network step,
   and it happens before any model file is opened.)
3. **Record, don't fabricate.** `hydration.json` records the exact
   interpreter, installer, requirement set, and resolved package versions;
   each run records device, dtype, prompt mode, sampling, timing, and output
   (capped at 2000 chars, with `output_truncated` set when capped). A
   successful run also writes a measured entry into the manifest's
   `runtime.tested_hardware` (see MANIFEST.md) — one entry per
   (host, device, engine), refreshed on each pass.
4. **Engines are registry entries.** `ENGINES` in `src/modelvault/hydrate.py`
   maps an engine to detection globs (over the manifest inventory), pip
   requirements, and a runner script. New runtimes (MLX, vLLM, ONNX…) are
   added there, not special-cased elsewhere.

## Engines

| Engine | Detected from | Installs | Runner |
|---|---|---|---|
| `transformers` (preferred) | `model/config.json` + safetensors/`.bin`/`.pt` weights | `torch`, `transformers>=X` (floor taken from the payload's own `config.json` `transformers_version`) | `runners/transformers_runner.py` — device auto (cuda → mps → cpu), chat template when the tokenizer ships one, greedy by default (`--sample` for the model's own sampling defaults) |
| `llama-cpp` | `model/*.gguf` | `llama-cpp-python` | `runners/llama_cpp_runner.py` — GPU offload when available; with several GGUF files, pick one with `--weights model/foo.gguf` |

Auto-detection prefers the first matching registry entry; override with
`--engine`.

## Environments

Envs are **shared, content-addressed venvs**: keyed by
`<engine>-py<major.minor>-<sha256(requirements)[:8]>`, so every bundle with
the same needs reuses one env, and changing the requirement set naturally
creates a new one. Each env carries an `env.json` (interpreter, installer,
requirements, full `pip list`); an env directory without one is treated as
half-built and rebuilt. Install failures remove the partial env and exit
non-zero — a broken env is never registered.

- Interpreter: `--python PATH` > `$MODELVAULT_PYTHON` > the python running
  modelvault. `uv` is used when on PATH, else `venv` + `pip`.
- `modelvault envs` lists envs, sizes, and which bundles reference them;
  `modelvault envs --prune` deletes unreferenced ones.
- `modelvault dehydrate <bundle>` drops the bundle's `hydration.json`
  (its run history included); manifest `tested_hardware` entries survive.
- `modelvault hydrate --dry-run` prints the full plan without touching
  anything; `--force` rebuilds an existing env in place.

## Runner contract

Runners are standalone scripts in `src/modelvault/runners/` — stdlib + their
engine only, since modelvault is not installed inside hydrated envs. They are
invoked as `<env-python> <runner>.py --json-out FILE …`, stream human output
to stdout/stderr, and write one JSON result object to `--json-out`.
`--probe` (used by `hydrate`) validates the env against the payload without
loading weights: imports, versions, device availability, tokenizer load.
Runners report failures in the JSON (`status: "fail"`, `error`) rather than
crashing silently, and never write into the model directory.

## `hydration.json`

```jsonc
{
  "hydration_schema": 1,
  "bundle_id": "qwen--qwen3-0.6b@c1899de289a0",
  "engine": "transformers",
  "weights": null,                   // payload-relative GGUF path for llama-cpp
  "hydrated_at": "…",
  "env": {"key", "path", "python", "python_executable",
          "created_at", "installer", "requirements"},
  "engine_packages": {"torch": "2.13.0", "transformers": "5.15.1", …},
  "probe": {"at", "status", "versions", "devices", "tokenizer", …},
  "runs": [ /* last 20: at, status, prompt, prompt_mode, device, dtype,
               sampling, new_tokens, stop_reason, timings,
               tokens_per_second, output (capped), error */ ]
}
```

Delete the file at any time — nothing else depends on it; the next
`modelvault run` re-hydrates.

---

[Documentation index](README.md)

"""Hydrate a bundle into a runnable local install, and run inference against it.

The payload under model/ stays byte-immutable: hydration builds a dedicated
virtualenv *outside* the bundle (under `<vault>/.runtime/envs/`, or
$DARSAY_RUNTIME) and records what it did in the bundle-root file
`hydration.json` — volatile state, excluded from exports like exports.json.

Environments are shared: keyed by (engine, python major.minor, hash of the
resolved requirement list), so ten bundles with the same needs use one env.
Engines are registry entries (ENGINES), mirroring schema.ARTIFACT_TYPES —
new runtimes slot in there, not as special cases elsewhere.

Inference runs fully offline (HF_HUB_OFFLINE=1): a run that passes proves the
archived payload is self-sufficient. Successful runs are recorded in
manifest.runtime.tested_hardware — measured facts, never estimates.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import socket
import subprocess
import sys
import tempfile
from fnmatch import fnmatch
from pathlib import Path

HYDRATION_FILE = "hydration.json"
HYDRATION_SCHEMA = 1
RUNS_HISTORY = 20
OUTPUT_RECORD_CAP = 2000  # chars of generated text kept per recorded run

DEFAULT_PROMPT = "Say hello in one short sentence."

# engine -> how to detect it from the manifest inventory, what to install,
# and which standalone runner script (runners/) drives it inside the env.
ENGINES = {
    "transformers": {
        "label": "Hugging Face transformers (safetensors / PyTorch weights)",
        "detect": {
            "all": ["model/config.json"],
            "any": ["model/*.safetensors", "model/*.bin", "model/*.pt", "model/*.pth"],
        },
        "requirements": ["torch", "transformers"],
        "runner": "transformers_runner.py",
        "weights_globs": None,  # loads the payload directory itself
        "architecture_suffixes": ("ForCausalLM", "LMHeadModel"),
        "install_hint": "first hydrate installs torch + transformers (often 1–2 GiB)",
    },
    "llama-cpp": {
        "label": "llama.cpp via llama-cpp-python (GGUF weights)",
        "detect": {"all": [], "any": ["model/*.gguf"]},
        "requirements": ["llama-cpp-python"],
        "runner": "llama_cpp_runner.py",
        "weights_globs": ["model/*.gguf"],
        "architecture_suffixes": None,  # GGUF embeds the architecture
        "install_hint": "first hydrate installs llama-cpp-python",
    },
    "mlx": {
        "label": "Apple MLX (mlx-lm)",
        "detect": {
            "all": ["model/config.json"],
            "any": ["model/*.npz"],
        },
        # Opt-in on a regular HF safetensors snapshot (`--engine mlx`).
        "compatible": {
            "all": ["model/config.json"],
            "any": ["model/*.safetensors", "model/*.npz", "model/*.bin"],
        },
        "requirements": ["mlx", "mlx-lm"],
        "runner": "mlx_runner.py",
        "weights_globs": None,
        "architecture_suffixes": ("ForCausalLM", "LMHeadModel"),
        "install_hint": "first hydrate installs mlx + mlx-lm",
        "platforms": ("darwin",),
        "machines": ("arm64", "aarch64"),
    },
}


def available_ram_bytes() -> int | None:
    """Physical RAM when the OS tells us; None if unknown. No extra deps."""
    try:
        if sys.platform == "darwin":
            out = subprocess.run(
                ["sysctl", "-n", "hw.memsize"],
                capture_output=True,
                text=True,
            )
            if out.returncode == 0 and out.stdout.strip().isdigit():
                return int(out.stdout.strip())
        if sys.platform.startswith("linux"):
            for line in Path("/proc/meminfo").read_text().splitlines():
                if line.startswith("MemAvailable:"):
                    parts = line.split()
                    return int(parts[1]) * 1024  # kB
    except (OSError, ValueError):
        return None
    return None


def engine_supports_payload(engine: str, manifest: dict) -> tuple[bool, str | None]:
    """Whether this engine's runner can serve the payload. Honest, not guessed."""
    spec = ENGINES[engine]
    suffixes = spec.get("architecture_suffixes")
    if not suffixes:
        return True, None
    arch = (manifest.get("model_metadata") or {}).get("architecture")
    loader = {
        "transformers": "AutoModelForCausalLM",
        "mlx": "mlx-lm generate",
    }.get(engine, engine)
    if not arch:
        return True, (f"{engine} runner uses {loader}; payload architecture is unknown")
    if any(str(arch).endswith(suffix) for suffix in suffixes):
        return True, None
    return False, (
        f"architecture {arch} is not a causal LM. `darsay run` / the {engine} "
        f"runner uses {loader}. Point a compatible loader at model/, "
        "or pass --ignore-preflight to try anyway."
    )


def preflight_run(
    manifest: dict,
    engine: str,
    *,
    env_exists: bool,
    ram_bytes: int | None = None,
) -> list[dict]:
    """Capability / resource issues before installing an env or loading weights."""
    issues: list[dict] = []
    ok, detail = engine_supports_payload(engine, manifest)
    if not ok:
        issues.append(
            {"level": "error", "code": "unsupported-architecture", "message": detail}
        )
    elif detail:
        issues.append(
            {"level": "warning", "code": "unknown-architecture", "message": detail}
        )

    needed = (manifest.get("runtime") or {}).get("estimated_min_ram_gb")
    have = available_ram_bytes() if ram_bytes is None else ram_bytes
    if needed and have is not None:
        have_gb = have / 1024**3
        ram_label = "physical RAM" if sys.platform == "darwin" else "available RAM"
        if ram_bytes is not None:
            ram_label = "RAM"
        if have_gb < needed:
            issues.append(
                {
                    "level": "error",
                    "code": "insufficient-ram",
                    "message": (
                        f"this payload wants ~{needed} GB RAM (weights × 1.2); "
                        f"this machine reports {have_gb:.1f} GB {ram_label}. "
                        "Archive a GGUF subset or pass --ignore-preflight."
                    ),
                }
            )
        elif have_gb < needed * 1.3:
            issues.append(
                {
                    "level": "warning",
                    "code": "tight-ram",
                    "message": (
                        f"this payload wants ~{needed} GB RAM; this machine reports "
                        f"{have_gb:.1f} GB {ram_label} — likely tight."
                    ),
                }
            )

    if not env_exists:
        hint = ENGINES[engine].get("install_hint")
        if hint:
            issues.append({"level": "info", "code": "env-install", "message": hint})
    if (
        engine == "transformers"
        and _platform_ok(ENGINES["mlx"])
        and _engine_applies(
            ENGINES["mlx"],
            [f["path"] for f in manifest.get("inventory", {}).get("files", [])],
            "compatible",
        )
    ):
        issues.append(
            {
                "level": "info",
                "code": "mlx-available",
                "message": "Apple Silicon: --engine mlx is often faster and a smaller install",
            }
        )
    return issues


def _emit_preflight(issues: list[dict], progress, *, ignore: bool) -> None:
    for issue in issues:
        progress(f"preflight {issue['level']}: {issue['message']}")
    errors = [i for i in issues if i["level"] == "error"]
    if errors and not ignore:
        raise SystemExit(
            "error: preflight failed — "
            + errors[0]["message"]
            + " (pass --ignore-preflight to try anyway)"
        )


# ---------------------------------------------------------------- paths & keys


def runtime_root(bundle_dir: Path) -> Path:
    """Envs live next to the vault that holds the bundle (vault/<name>/<rev>),
    or wherever $DARSAY_RUNTIME points."""
    override = os.environ.get("DARSAY_RUNTIME")
    if override:
        return Path(override)
    return bundle_dir.parent.parent / ".runtime"


def env_python(env_dir: Path) -> Path:
    if os.name == "nt":  # pragma: no cover — macOS/Linux are the priority targets
        return env_dir / "Scripts" / "python.exe"
    return env_dir / "bin" / "python"


def _env_key(engine: str, py_minor: str, requirements: list[str]) -> str:
    digest = hashlib.sha256("\n".join(requirements).encode("utf-8")).hexdigest()[:8]
    return f"{engine}-py{py_minor}-{digest}"


def _runner_path(engine: str) -> Path:
    return Path(__file__).parent / "runners" / ENGINES[engine]["runner"]


# ------------------------------------------------------------ engine selection


def _matches(inventory_paths: list[str], patterns: list[str]) -> list[str]:
    return sorted({p for p in inventory_paths for pat in patterns if fnmatch(p, pat)})


def _platform_ok(spec: dict) -> bool:
    platforms = spec.get("platforms")
    if platforms and sys.platform not in platforms:
        return False
    machines = spec.get("machines")
    if machines and platform.machine() not in machines:
        return False
    return True


def _engine_applies(spec: dict, inventory_paths: list[str], key: str) -> bool:
    det = spec.get(key)
    if not det:
        return False
    return all(_matches(inventory_paths, [pat]) for pat in det["all"]) and (
        not det["any"] or bool(_matches(inventory_paths, det["any"]))
    )


def detect_engines(inventory_paths: list[str]) -> list[str]:
    """Engines the payload can support, in registry (preference) order."""
    found = []
    for name, spec in ENGINES.items():
        if not _platform_ok(spec):
            continue
        if _engine_applies(spec, inventory_paths, "detect"):
            found.append(name)
    return found


def select_engine(manifest: dict, requested: str | None) -> str:
    inventory_paths = [f["path"] for f in manifest["inventory"]["files"]]
    detected = detect_engines(inventory_paths)
    if requested:
        if requested not in ENGINES:
            raise SystemExit(
                f"error: unknown engine {requested!r} (known: {', '.join(ENGINES)})"
            )
        spec = ENGINES[requested]
        if not _platform_ok(spec):
            where = ", ".join(spec.get("platforms") or [])
            machines = spec.get("machines") or ()
            extra = f", {', '.join(machines)}" if machines else ""
            raise SystemExit(
                f"error: engine {requested!r} is supported on {where or 'no platforms'}{extra} "
                f"(this OS is {sys.platform}, machine {platform.machine()})"
            )
        if requested in detected or _engine_applies(
            spec, inventory_paths, "compatible"
        ):
            return requested
        raise SystemExit(
            f"error: engine {requested!r} does not match this payload "
            f"(detected: {', '.join(detected) or 'none'})"
        )
    if not detected:
        if manifest.get("artifact_type") == "dataset":
            raise SystemExit(
                "error: datasets have no inference engine — "
                "open data/ directly, or `darsay info` for the recorded facts"
            )
        raise SystemExit(
            "error: no known engine matches this payload "
            f"(known engines: {', '.join(ENGINES)})"
        )
    return detected[0]


def select_weights(manifest: dict, engine: str, requested: str | None) -> str | None:
    """For engines that load a single weights file (GGUF), pick or validate it."""
    globs = ENGINES[engine]["weights_globs"]
    if globs is None:
        return None
    inventory_paths = [f["path"] for f in manifest["inventory"]["files"]]
    candidates = _matches(inventory_paths, globs)
    if engine == "llama-cpp":
        from .weight_variants import gguf_groups, is_projector

        groups = [
            g
            for g in gguf_groups([{"path": p} for p in candidates])
            if not is_projector(g["items"][0]["path"])
        ]
        incomplete = [g for g in groups if not g["complete"]]
        if incomplete and (
            not requested
            or any(requested in {f["path"] for f in g["items"]} for g in incomplete)
        ):
            raise SystemExit(
                "error: incomplete GGUF shard set — archive every shard before running"
            )
        candidates = [g["items"][0]["path"] for g in groups if g["complete"]]
    if requested:
        if requested not in candidates:
            raise SystemExit(
                f"error: --weights {requested!r} is not a {engine} weights file in this payload; "
                f"candidates: {', '.join(candidates)}"
            )
        return requested
    if len(candidates) == 1:
        return candidates[0]
    raise SystemExit(
        f"error: {len(candidates)} candidate weights files for {engine} — choose one with "
        "--weights:\n  " + "\n  ".join(candidates)
    )


def requirement_name(req: str) -> str:
    for sep in ("===", "==", ">=", "<=", "~=", "!=", ">", "<"):
        if sep in req:
            return req.split(sep, 1)[0].strip()
    return req.strip()


def pin_requirements(loose: list[str], pinned: dict[str, str] | None) -> list[str]:
    """Re-install from recorded package versions when an env is rebuilt."""
    if not pinned:
        return list(loose)
    lookup = {k.lower().replace("_", "-"): v for k, v in pinned.items()}
    out = []
    for req in loose:
        name = requirement_name(req)
        version = lookup.get(name.lower().replace("_", "-"))
        out.append(f"{name}=={version}" if version else req)
    return sorted(out)


def resolve_requirements(engine: str, payload_root: Path) -> list[str]:
    """Registry requirements, tightened with what the payload itself declares
    (config.json transformers_version -> a floor, recorded, never guessed)."""
    reqs = list(ENGINES[engine]["requirements"])
    if engine == "transformers":
        try:
            config = json.loads(
                (payload_root / "config.json").read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError):
            config = {}
        declared = config.get("transformers_version")
        if declared:
            reqs = [
                f"transformers>={declared}" if r == "transformers" else r for r in reqs
            ]
    return sorted(reqs)


# ------------------------------------------------------------- env management


def _pick_python(explicit: str | None) -> str:
    return explicit or os.environ.get("DARSAY_PYTHON") or sys.executable


def _python_version(python_exe: str) -> tuple[str, str]:
    """(full version, major.minor) of an interpreter, by asking it."""
    out = subprocess.run(
        [python_exe, "-c", "import sys; print('%d.%d.%d' % sys.version_info[:3])"],
        capture_output=True,
        text=True,
    )
    if out.returncode != 0:
        raise SystemExit(
            f"error: cannot run python interpreter {python_exe!r}: {out.stderr.strip()}"
        )
    full = out.stdout.strip()
    return full, full.rsplit(".", 1)[0]


def _installer() -> tuple[str, str | None]:
    uv = shutil.which("uv")
    return ("uv", uv) if uv else ("pip", None)


def _installed_packages(python_bin: Path) -> dict[str, str]:
    out = subprocess.run(
        [
            str(python_bin),
            "-m",
            "pip",
            "list",
            "--format",
            "json",
            "--disable-pip-version-check",
        ],
        capture_output=True,
        text=True,
    )
    if out.returncode != 0:
        return {}
    return {p["name"]: p["version"] for p in json.loads(out.stdout)}


def ensure_env(
    engine: str,
    requirements: list[str],
    python: str | None,
    root: Path,
    force: bool = False,
    progress=print,
) -> dict:
    """Create (or reuse) the shared env for (engine, python, requirements).
    Returns the env record; on install failure removes the half-built env and
    exits non-zero — a broken env is never registered."""
    from .archiver import utc_now

    python_exe = _pick_python(python)
    py_full, py_minor = _python_version(python_exe)
    key = _env_key(engine, py_minor, requirements)
    env_dir = root / "envs" / key
    marker = env_dir / "env.json"

    if marker.is_file() and not force:
        record = json.loads(marker.read_text(encoding="utf-8"))
        progress(
            f"Reusing env {key} ({record['python']}, {len(record['packages'])} packages)"
        )
        return record

    if env_dir.exists():  # half-built leftover, or --force
        shutil.rmtree(env_dir)
    env_dir.mkdir(parents=True)

    installer, uv_path = _installer()
    progress(f"Creating env {key} with {installer} (python {py_full}) ...")
    started = utc_now()
    try:
        if installer == "uv":
            _run_or_die([uv_path, "venv", "--python", python_exe, str(env_dir)])
            _run_or_die(
                [uv_path, "pip", "install", "--python", str(env_python(env_dir))]
                + requirements
            )
        else:
            _run_or_die([python_exe, "-m", "venv", str(env_dir)])
            _run_or_die(
                [
                    str(env_python(env_dir)),
                    "-m",
                    "pip",
                    "install",
                    "--disable-pip-version-check",
                ]
                + requirements
            )
    except SystemExit:
        shutil.rmtree(env_dir, ignore_errors=True)
        progress(f"Install failed; removed {env_dir}.")
        progress(
            "Hint: a different interpreter may have wheels — retry with "
            "--python /path/to/python3.x (or set $DARSAY_PYTHON)."
        )
        raise

    record = {
        "key": key,
        "engine": engine,
        "path": str(env_dir.resolve()),
        "python": py_full,
        # Absolute path but NOT resolve(): a venv's bin/python is a symlink to
        # the base interpreter, and following it would escape the env.
        "python_executable": str(env_python(env_dir.resolve())),
        "created_at": started,
        "installer": installer,
        "requirements": requirements,
        "packages": _installed_packages(env_python(env_dir)),
    }
    marker.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    progress(f"Env ready: {env_dir}")
    return record


def _run_or_die(cmd: list[str]) -> None:
    rc = subprocess.run(cmd).returncode
    if rc != 0:
        raise SystemExit(f"error: command failed ({rc}): {' '.join(cmd)}")


# ---------------------------------------------------------- runner invocation


def _offline_env() -> dict:
    env = dict(os.environ)
    env.update(
        {
            "HF_HUB_OFFLINE": "1",  # a passing run proves the payload is self-sufficient
            "TRANSFORMERS_OFFLINE": "1",
            "TOKENIZERS_PARALLELISM": "false",
            "PYTHONUNBUFFERED": "1",
        }
    )
    return env


def _invoke_runner(
    env_record: dict, engine: str, runner_args: list[str], timeout: float | None = None
) -> tuple[int, dict | None]:
    """Run the engine's standalone script inside the env; stdio is inherited so
    the user sees progress live, the JSON result comes back via a temp file."""
    with tempfile.NamedTemporaryFile("r", suffix=".json", delete=False) as tf:
        json_out = tf.name
    try:
        cmd = [
            env_record["python_executable"],
            str(_runner_path(engine)),
            "--json-out",
            json_out,
        ] + runner_args
        sys.stdout.flush()  # keep progress lines ordered before the runner's inherited stdout
        try:
            rc = subprocess.run(cmd, env=_offline_env(), timeout=timeout).returncode
        except subprocess.TimeoutExpired:
            return -1, {"status": "fail", "error": f"timed out after {timeout}s"}
        try:
            result = json.loads(Path(json_out).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            result = None
        return rc, result
    finally:
        Path(json_out).unlink(missing_ok=True)


# ------------------------------------------------------------------- hydrate


def load_hydration(bundle_dir: Path) -> dict | None:
    path = bundle_dir / HYDRATION_FILE
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def write_hydration(bundle_dir: Path, record: dict) -> None:
    (bundle_dir / HYDRATION_FILE).write_text(
        json.dumps(record, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


def hydrate_bundle(
    bundle_dir: Path,
    engine: str | None = None,
    python: str | None = None,
    weights: str | None = None,
    force: bool = False,
    dry_run: bool = False,
    ignore_preflight: bool = False,
    progress=print,
) -> dict:
    from .archiver import load_manifest, utc_now, write_manifest
    from .schema import payload_root as manifest_payload_root

    manifest = load_manifest(bundle_dir)
    payload_root = bundle_dir / manifest_payload_root(manifest)
    chosen = select_engine(manifest, engine)
    weights_path = select_weights(manifest, chosen, weights)
    loose = resolve_requirements(chosen, payload_root)
    previous = load_hydration(bundle_dir) or {}
    pinned = None if force else previous.get("engine_packages")
    install_spec = pin_requirements(loose, pinned)
    root = runtime_root(bundle_dir)

    python_exe = _pick_python(python)
    py_full, py_minor = _python_version(python_exe)
    prev_env = previous.get("env") or {}
    prev_marker = None
    if not force and prev_env.get("key"):
        candidate = root / "envs" / prev_env["key"] / "env.json"
        if candidate.is_file():
            prev_marker = candidate
    if prev_marker is not None:
        requirements = json.loads(prev_marker.read_text(encoding="utf-8"))[
            "requirements"
        ]
    else:
        requirements = install_spec
    key = _env_key(chosen, py_minor, requirements)
    env_dir = root / "envs" / key
    env_exists = (env_dir / "env.json").is_file()
    issues = preflight_run(manifest, chosen, env_exists=env_exists)
    _emit_preflight(issues, progress, ignore=ignore_preflight)

    if dry_run:
        progress(f"Plan for {manifest['bundle_id']}:")
        progress(f"  engine:       {chosen} — {ENGINES[chosen]['label']}")
        if weights_path:
            progress(f"  weights:      {weights_path}")
        progress(f"  requirements: {', '.join(requirements)}")
        progress(f"  python:       {python_exe} ({py_full})")
        progress(
            f"  env:          {env_dir} ({'exists, will reuse' if env_exists else 'will create'})"
        )
        progress(f"  installer:    {_installer()[0]}")
        progress(f"  then:         probe env offline, write {HYDRATION_FILE}")
        return {
            "dry_run": True,
            "engine": chosen,
            "weights": weights_path,
            "env_key": key,
            "env_exists": env_exists,
            "requirements": requirements,
            "preflight": issues,
            "rebuild_from_pins": bool(pinned) and not env_exists,
        }

    env_record = ensure_env(
        chosen, requirements, python, root, force=force, progress=progress
    )

    progress("Probing env against the payload (offline) ...")
    probe_args = ["--probe", "--model-dir", str(payload_root)]
    if weights_path:
        probe_args += ["--weights", str(bundle_dir / weights_path)]
    rc, probe = _invoke_runner(env_record, chosen, probe_args)
    probe = probe or {
        "status": "fail",
        "error": f"probe produced no result (exit {rc})",
    }
    progress(f"  probe: {probe.get('status')}")

    now = utc_now()
    record = {
        "hydration_schema": HYDRATION_SCHEMA,
        "bundle_id": manifest["bundle_id"],
        "engine": chosen,
        "weights": weights_path,
        "hydrated_at": now,
        "env": {
            k: env_record[k]
            for k in (
                "key",
                "path",
                "python",
                "python_executable",
                "created_at",
                "installer",
                "requirements",
            )
        },
        "engine_packages": _relevant_packages(env_record["packages"]),
        "probe": {"at": now, **probe},
        "runs": previous.get("runs", []),
    }
    write_hydration(bundle_dir, record)

    manifest["archive"]["last_accessed"] = now
    write_manifest(bundle_dir, manifest)

    if probe.get("status") != "pass":
        progress(
            f"Hydration recorded with a FAILING probe — see {bundle_dir / HYDRATION_FILE}"
        )
    else:
        progress(f"Hydrated. Next: darsay run {manifest['bundle_id']}")
    return record


_RELEVANT = (
    "torch",
    "transformers",
    "tokenizers",
    "safetensors",
    "accelerate",
    "llama_cpp_python",
    "llama-cpp-python",
    "numpy",
    "mlx",
    "mlx-lm",
)


def _relevant_packages(packages: dict[str, str]) -> dict[str, str]:
    return {
        k: v
        for k, v in packages.items()
        if k.lower().replace("_", "-") in {r.replace("_", "-") for r in _RELEVANT}
    }


# ----------------------------------------------------------------------- run


def run_bundle(
    bundle_dir: Path,
    prompt: str | None = None,
    engine: str | None = None,
    max_new_tokens: int = 256,
    raw: bool = False,
    sample: bool = False,
    device: str = "auto",
    dtype: str = "auto",
    trust_remote_code: bool = False,
    seed: int | None = None,
    timeout: float | None = None,
    python: str | None = None,
    weights: str | None = None,
    ignore_preflight: bool = False,
    repl: bool = False,
    dry_run: bool = False,
    progress=print,
) -> dict:
    """Generate against the bundle offline, hydrating first when needed.

    ``dry_run`` prints what would happen — the hydration plan if one is
    needed, then engine, prompt, and settings — and runs nothing.
    """
    from .archiver import load_manifest, utc_now, write_manifest

    hydration = load_hydration(bundle_dir)
    stale = (
        hydration is None
        or (engine and hydration["engine"] != engine)
        or (weights and hydration.get("weights") != weights)
        or not Path(hydration["env"]["python_executable"]).is_file()
    )
    if stale:
        if dry_run:
            progress(
                "Bundle not hydrated for this configuration — run would hydrate first:"
            )
        else:
            progress("Bundle not hydrated for this configuration — hydrating first ...")
        hydration = hydrate_bundle(
            bundle_dir,
            engine=engine,
            python=python,
            weights=weights,
            ignore_preflight=ignore_preflight,
            dry_run=dry_run,
            progress=progress,
        )
    else:
        issues = preflight_run(
            load_manifest(bundle_dir),
            hydration["engine"],
            env_exists=True,
        )
        _emit_preflight(issues, progress, ignore=ignore_preflight)

    from .schema import payload_root as manifest_payload_root

    chosen = hydration["engine"]
    if not repl:
        prompt = prompt if prompt is not None else DEFAULT_PROMPT
    if dry_run:
        return _print_run_plan(
            hydration,
            chosen,
            prompt=prompt,
            repl=repl,
            raw=raw,
            sample=sample,
            seed=seed,
            device=device,
            dtype=dtype,
            max_new_tokens=max_new_tokens,
            progress=progress,
        )

    env_record = {"python_executable": hydration["env"]["python_executable"]}

    payload_dir = bundle_dir / manifest_payload_root(load_manifest(bundle_dir))
    runner_args = [
        "--model-dir",
        str(payload_dir),
        "--max-new-tokens",
        str(max_new_tokens),
        "--device",
        device,
    ]
    if prompt:
        runner_args += ["--prompt", prompt]
    if repl:
        runner_args.append("--repl")
    if chosen == "transformers":
        runner_args += ["--dtype", dtype]
    if hydration.get("weights"):
        runner_args += ["--weights", str(bundle_dir / hydration["weights"])]
    if raw:
        runner_args.append("--raw")
    if sample:
        runner_args.append("--sample")
    if seed is not None:
        runner_args += ["--seed", str(seed)]
    if trust_remote_code:
        runner_args.append("--trust-remote-code")

    progress(f"Running {chosen} inference (offline) ...\n")
    rc, result = _invoke_runner(env_record, chosen, runner_args, timeout=timeout)
    result = result or {
        "status": "fail",
        "error": f"runner produced no result (exit {rc})",
    }
    ok = rc == 0 and result.get("status") == "pass"

    now = utc_now()
    output = result.get("output") or ""
    run_record = {
        "at": now,
        "status": "pass" if ok else "fail",
        "engine": chosen,
        "env_key": hydration["env"]["key"],
        "prompt": prompt,
        "prompt_mode": result.get("prompt_mode"),
        "device": result.get("device"),
        "dtype": result.get("dtype"),
        "sampling": result.get("sampling"),
        "seed": seed,
        "new_tokens": result.get("new_tokens"),
        "stop_reason": result.get("stop_reason"),
        "load_seconds": result.get("load_seconds"),
        "generate_seconds": result.get("generate_seconds"),
        "tokens_per_second": result.get("tokens_per_second"),
        "output": output[:OUTPUT_RECORD_CAP],
        "output_chars": len(output),
        "output_truncated": len(output) > OUTPUT_RECORD_CAP,
        "error": result.get("error"),
    }
    hydration["runs"] = (hydration.get("runs", []) + [run_record])[-RUNS_HISTORY:]
    write_hydration(bundle_dir, hydration)

    manifest = load_manifest(bundle_dir)
    if ok:
        _record_tested_hardware(manifest, run_record, result, now)
    manifest["archive"]["last_accessed"] = now
    write_manifest(bundle_dir, manifest)

    progress("")
    if ok:
        bits = ["Run PASSED"]
        if run_record["new_tokens"] is not None:
            bits.append(f"— {run_record['new_tokens']} tokens")
        if run_record["device"]:
            bits.append(f"on {run_record['device']}")
        extras = []
        if run_record["tokens_per_second"] is not None:
            extras.append(f"{run_record['tokens_per_second']} tok/s")
        if run_record["load_seconds"] is not None:
            extras.append(f"load {run_record['load_seconds']}s")
        if run_record["generate_seconds"] is not None:
            extras.append(f"generate {run_record['generate_seconds']}s")
        line = " ".join(bits)
        if extras:
            line += f" ({', '.join(extras)})"
        progress(line)
        progress(
            f"Recorded in {bundle_dir / HYDRATION_FILE} and manifest runtime.tested_hardware"
        )
    else:
        progress(f"Run FAILED: {run_record['error'] or 'see runner output above'}")
        progress(f"Recorded in {bundle_dir / HYDRATION_FILE}")
    return run_record


def _print_run_plan(
    hydration: dict,
    engine: str,
    *,
    prompt: str | None,
    repl: bool,
    raw: bool,
    sample: bool,
    seed: int | None,
    device: str,
    dtype: str,
    max_new_tokens: int,
    progress,
) -> dict:
    """What ``run`` would do once the env is ready; the dry-run record."""
    if repl:
        prompt_line = "interactive (--repl; /quit to exit)"
    else:
        mode = "raw completion, no chat template" if raw else "chat template"
        prompt_line = f"{json.dumps(prompt)}  ({mode})"
    decoding = "greedy"
    if sample:
        decoding = "sampled with the model's generation defaults"
        if seed is not None:
            decoding += f", seed {seed}"
    settings = [f"up to {max_new_tokens} new tokens", f"device {device}"]
    if engine == "transformers":
        settings.append(f"dtype {dtype}")
    progress(f"Would run {engine} inference (offline, HF_HUB_OFFLINE=1):")
    progress(f"  prompt:       {prompt_line}")
    progress(f"  generation:   {decoding}; {', '.join(settings)}")
    if hydration.get("weights"):
        progress(f"  weights:      {hydration['weights']}")
    progress(
        "  records:      hydration.json runs[] and, on a pass, "
        "manifest runtime.tested_hardware"
    )
    return {
        "dry_run": True,
        "engine": engine,
        "hydrate_first": bool(hydration.get("dry_run")),
        "prompt": prompt,
        "repl": repl,
    }


def _chip() -> str | None:
    try:
        if sys.platform == "darwin":
            out = subprocess.run(
                ["sysctl", "-n", "machdep.cpu.brand_string"],
                capture_output=True,
                text=True,
            )
            return out.stdout.strip() or None
        if sys.platform.startswith("linux"):
            for line in Path("/proc/cpuinfo").read_text().splitlines():
                if line.lower().startswith("model name"):
                    return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return None


def _record_tested_hardware(
    manifest: dict, run_record: dict, result: dict, now: str
) -> None:
    """One entry per (host, device, engine), refreshed on each successful run."""
    entry = {
        "at": now,
        "host": socket.gethostname(),
        "os": platform.platform(),
        "chip": _chip(),
        "device": run_record["device"],
        "engine": run_record["engine"],
        "engine_versions": result.get("versions"),
        "tokens_per_second": run_record["tokens_per_second"],
        "status": "pass",
        "via": "darsay run",
    }
    tested = manifest["runtime"].get("tested_hardware") or []

    def keyfn(e):
        return (e.get("host"), e.get("device"), e.get("engine"))

    tested = [e for e in tested if keyfn(e) != keyfn(entry)] + [entry]
    manifest["runtime"]["tested_hardware"] = tested


# ------------------------------------------------------------- env lifecycle


def dehydrate_bundle(
    bundle_dir: Path, progress=print, *, dry_run: bool = False
) -> bool:
    """Drop the bundle's hydration record. Returns whether there was one."""
    path = bundle_dir / HYDRATION_FILE
    if not path.is_file():
        progress(f"{bundle_dir} is not hydrated — nothing to do.")
        return False
    detail = "run history included"
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
        runs = len(record.get("runs") or [])
        detail = (
            f"{record.get('engine') or 'unknown engine'}; "
            f"{runs} run{'s' if runs != 1 else ''} recorded"
        )
    except (OSError, ValueError, AttributeError):
        pass
    if dry_run:
        progress(f"Would remove {path} ({detail}).")
    else:
        path.unlink()
        progress(f"Removed {path} ({detail}).")
    progress("Shared envs are untouched; reclaim disk with: darsay envs --prune")
    return True


def _dir_size(path: Path) -> int:
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file())


def list_envs(vault: Path, progress=print) -> list[dict]:
    root = Path(os.environ.get("DARSAY_RUNTIME") or vault / ".runtime")
    refs: dict[str, list[str]] = {}
    for hyd in sorted(vault.glob(f"*/*/{HYDRATION_FILE}")):
        try:
            data = json.loads(hyd.read_text(encoding="utf-8"))
            refs.setdefault(data["env"]["key"], []).append(data["bundle_id"])
        except (json.JSONDecodeError, KeyError):
            continue
    envs = []
    for marker in sorted(root.glob("envs/*/env.json")):
        record = json.loads(marker.read_text(encoding="utf-8"))
        envs.append(
            {
                "key": record["key"],
                "path": str(marker.parent),
                "python": record["python"],
                "size_bytes": _dir_size(marker.parent),
                "created_at": record["created_at"],
                "used_by": refs.get(record["key"], []),
            }
        )
    return envs


def prune_envs(vault: Path, progress=print, *, dry_run: bool = False) -> int:
    """Delete envs no hydrated bundle references; returns the bytes freed.

    ``dry_run`` lists them and deletes nothing (the return is what would be freed).
    """
    from .readme_gen import human_size

    freed = 0
    for env in list_envs(vault):
        if not env["used_by"]:
            progress(
                f"{'Would remove' if dry_run else 'Removing'} unreferenced env "
                f"{env['key']} ({human_size(env['size_bytes'])})"
            )
            if not dry_run:
                shutil.rmtree(env["path"])
            freed += env["size_bytes"]
    if freed:
        progress(f"{'Would free' if dry_run else 'Freed'} {human_size(freed)}")
    else:
        progress("Nothing to prune — every env is referenced by a hydrated bundle.")
    return freed

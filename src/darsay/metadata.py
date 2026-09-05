"""Extract model / dataset / code metadata from the archived payload.

Everything here reads only the downloaded files — no network — so `regen` and
`verify` can rebuild metadata offline from the archive itself. For datasets,
upstream claims (split sizes from dataset_infos.json / card YAML) are recorded
as *declared*; only facts established from the payload itself (pyarrow row
counts) are *measured* — record-don't-fabricate applied to data.
"""

from __future__ import annotations

import json
from pathlib import Path

from .precision import bytes_per_param, precision_facts
from .safetensors_meta import summarize_safetensors
from .weight_variants import model_weight_bytes


def _load_json(path: Path) -> dict | None:
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None


def extract_model_metadata(payload_root: Path, card_data: dict | None = None) -> dict:
    card_data = card_data or {}
    config = _load_json(payload_root / "config.json") or {}
    generation_config = _load_json(payload_root / "generation_config.json") or {}
    tokenizer_config = _load_json(payload_root / "tokenizer_config.json") or {}

    safetensors_files = sorted(payload_root.glob("*.safetensors"))
    try:
        weights = summarize_safetensors(safetensors_files)
    except (OSError, ValueError):
        # A shard whose header cannot be read leaves the count unknown —
        # never a partial count over the readable shards.
        weights = None

    special_tokens = {
        key: tokenizer_config.get(key)
        for key in (
            "bos_token",
            "eos_token",
            "pad_token",
            "unk_token",
            "sep_token",
            "cls_token",
            "mask_token",
        )
        if tokenizer_config.get(key) is not None
    }

    chat_template_present = bool(tokenizer_config.get("chat_template")) or any(
        payload_root.glob("chat_template.*")
    )

    languages = card_data.get("language")
    if isinstance(languages, str):
        languages = [languages]

    quantization = None
    if config.get("quantization_config"):
        qc = config["quantization_config"]
        quantization = qc.get("quant_method") or "unknown"

    # The release precision and what it actually spends per weight — the
    # numbers that explain the payload's size (precision.py).
    weight_files = [
        p
        for suffix in ("*.safetensors", "*.gguf", "*.bin", "*.pt", "*.pth")
        for p in payload_root.rglob(suffix)
    ]
    gguf_files = [p for p in weight_files if p.suffix.lower() == ".gguf"]
    weight_bytes = model_weight_bytes(
        [
            {"path": p.relative_to(payload_root).as_posix(), "size": p.stat().st_size}
            for p in weight_files
            if p.is_file()
        ]
    )
    precision = precision_facts(
        config=config,
        dominant_dtype=weights["dominant_dtype"] if weights else None,
        dominant_format="gguf" if gguf_files and not safetensors_files else None,
        weight_paths=[p.relative_to(payload_root).as_posix() for p in weight_files],
    )

    return {
        "parameter_count": weights["parameter_count"] if weights else None,
        "parameters_by_dtype": weights["parameters_by_dtype"] if weights else None,
        "architecture": (config.get("architectures") or [None])[0],
        "model_type": config.get("model_type"),
        "context_length": config.get("max_position_embeddings"),
        "precision": precision["label"]
        or config.get("torch_dtype")
        or (weights["dominant_dtype"] if weights else None),
        "precision_detail": precision["detail"],
        "bytes_per_param": bytes_per_param(
            weight_bytes, weights["parameter_count"] if weights else None
        ),
        "quantization": quantization,
        "hidden_size": config.get("hidden_size"),
        "num_hidden_layers": config.get("num_hidden_layers"),
        "num_attention_heads": config.get("num_attention_heads"),
        "num_key_value_heads": config.get("num_key_value_heads"),
        "tie_word_embeddings": config.get("tie_word_embeddings"),
        "tokenizer": {
            "class": tokenizer_config.get("tokenizer_class"),
            "vocab_size": config.get("vocab_size"),
            "model_max_length": tokenizer_config.get("model_max_length"),
            "special_tokens": special_tokens or None,
            "chat_template_present": chat_template_present,
        },
        "languages": languages,
        "training_cutoff": None,  # rarely published; curator fills in when known
        "generation_defaults": {
            key: generation_config[key]
            for key in (
                "temperature",
                "top_p",
                "top_k",
                "do_sample",
                "repetition_penalty",
                "max_new_tokens",
            )
            if key in generation_config
        }
        or None,
        "weight_shards": weights["shard_count"] if weights else None,
    }


def _declared_dataset_info(infos: dict, card_data: dict) -> dict | None:
    """Configs/splits/features as upstream declares them. dataset_infos.json is
    authoritative; the card's `dataset_info` YAML fills in configs it lacks."""
    sources = []
    raw_configs: dict[str, dict] = {}
    if infos:
        sources.append("dataset_infos.json")
        for name, info in infos.items():
            if isinstance(info, dict):
                raw_configs[name or "default"] = info
    card_info = card_data.get("dataset_info")
    if isinstance(card_info, dict):
        card_info = [card_info]
    if isinstance(card_info, list):
        added = False
        for info in card_info:
            if isinstance(info, dict):
                key = info.get("config_name") or "default"
                if key not in raw_configs:
                    raw_configs[key] = info
                    added = True
        if added:
            sources.append("card")
    if not raw_configs:
        return None

    configs = {}
    total_examples = 0
    have_counts = False
    for name, info in raw_configs.items():
        splits_in = info.get("splits") or {}
        if isinstance(
            splits_in, list
        ):  # card YAML lists splits; dataset_infos.json maps them
            splits_in = {s.get("name"): s for s in splits_in if isinstance(s, dict)}
        splits = {}
        for split_name, s in splits_in.items():
            if not isinstance(s, dict):
                continue
            splits[split_name] = {
                "num_examples": s.get("num_examples"),
                "num_bytes": s.get("num_bytes"),
            }
            if s.get("num_examples") is not None:
                total_examples += s["num_examples"]
                have_counts = True
        configs[name] = {
            "features": info.get("features"),
            "splits": splits or None,
            "download_size": info.get("download_size"),
            "dataset_size": info.get("dataset_size"),
        }
    return {
        "sources": sources,
        "configs": configs,
        "example_count_total": total_examples if have_counts else None,
    }


def _measured_row_counts(payload_root: Path) -> dict:
    """Row counts established from the payload itself. Parquet only, and only
    when pyarrow is available — otherwise recorded as skipped, never guessed."""
    parquet_files = sorted(
        p
        for p in payload_root.rglob("*.parquet")
        if p.is_file() and ".cache" not in p.relative_to(payload_root).parts
    )
    if not parquet_files:
        return {
            "status": "skipped",
            "reason": "no parquet files in payload (row counting covers parquet only)",
        }
    try:
        import pyarrow.parquet as pq
    except ImportError:
        return {
            "status": "skipped",
            "reason": "pyarrow not installed (pip install darsay[datasets])",
        }
    rows = {}
    errors = {}
    for p in parquet_files:
        rel = p.relative_to(payload_root).as_posix()
        try:
            rows[rel] = pq.ParquetFile(p).metadata.num_rows
        except Exception as exc:  # record the failure, never crash the archive
            errors[rel] = str(exc)
    out = {
        "status": "measured" if not errors else "partial",
        "method": "pyarrow parquet metadata",
        "row_counts": rows or None,
        "total_rows": sum(rows.values()) if rows else None,
    }
    if errors:
        out["errors"] = errors
    return out


def extract_dataset_metadata(
    payload_root: Path,
    card_data: dict | None = None,
    file_records: list[dict] | None = None,
) -> dict:
    card_data = card_data or {}
    infos = _load_json(payload_root / "dataset_infos.json") or {}

    formats: dict[str, dict] = {}
    for r in file_records or []:
        ext = Path(r["path"]).suffix.lower().lstrip(".") or "(none)"
        entry = formats.setdefault(ext, {"file_count": 0, "total_size_bytes": 0})
        entry["file_count"] += 1
        entry["total_size_bytes"] += r["size"] or 0
    formats = dict(
        sorted(formats.items(), key=lambda kv: (-kv[1]["total_size_bytes"], kv[0]))
    )

    def listed(value):
        if isinstance(value, str):
            return [value]
        return list(value) if value else None

    return {
        "formats": formats or None,
        "declared": _declared_dataset_info(infos, card_data),
        "measured": _measured_row_counts(payload_root),
        "task_categories": listed(card_data.get("task_categories")),
        "size_categories": listed(card_data.get("size_categories")),
        "languages": listed(card_data.get("language")),
    }


# The standard files that say how a source tree is built or run — the
# vocabulary a harness layer dispatches on. Each entry: label, name
# patterns (matched against the file name at any depth, or against the
# whole path when the pattern has a slash), and whether only the tree root
# counts. Recorded from the inventory as evidence, never as a verdict.
RUNTIME_DECLARATIONS = (
    (
        "container",
        ("Dockerfile", "Dockerfile.*", "*.Dockerfile", "Containerfile"),
        False,
    ),
    (
        "compose",
        (
            "compose.yaml",
            "compose.yml",
            "docker-compose.yaml",
            "docker-compose.yml",
            "docker-compose.*.yaml",
            "docker-compose.*.yml",
        ),
        False,
    ),
    (
        "python",
        (
            "pyproject.toml",
            "setup.py",
            "setup.cfg",
            "requirements.txt",
            "requirements-*.txt",
            "requirements/*.txt",
            "environment.yml",
            "environment.yaml",
            "uv.lock",
            "poetry.lock",
        ),
        False,
    ),
    ("node", ("package.json",), False),
    ("rust", ("Cargo.toml",), False),
    ("go", ("go.mod",), False),
    ("nix", ("flake.nix", "shell.nix", "default.nix"), False),
    ("make", ("Makefile", "makefile", "GNUmakefile", "justfile", "Justfile"), True),
    ("env_template", (".env.sample", ".env.example", ".env.template"), True),
    ("shell", ("*.sh",), True),
)
RUNTIME_DECLARATION_CAP = 20


def runtime_declarations(paths: list[str]) -> dict:
    """Which standard build/run files a source tree carries, by label.

    ``paths`` are payload-relative. Lists are sorted and capped at
    ``RUNTIME_DECLARATION_CAP`` entries with the true count beside them;
    a label with no match is absent. ``read_from`` says where this came
    from: the inventory, not a claim about what the tree is for.
    """
    from fnmatch import fnmatch

    found: dict[str, list[str]] = {}
    for label, patterns, root_only in RUNTIME_DECLARATIONS:
        matched = set()
        for path in paths:
            if root_only and "/" in path:
                continue
            name = path.rsplit("/", 1)[-1]
            for pattern in patterns:
                target = path if "/" in pattern else name
                if fnmatch(target, pattern):
                    matched.add(path)
                    break
        if matched:
            found[label] = sorted(matched)
    counts = {label: len(items) for label, items in found.items()}
    return {
        "read_from": "inventory",
        "found": {
            label: items[:RUNTIME_DECLARATION_CAP] for label, items in found.items()
        }
        or None,
        "counts": counts or None,
    }


def extract_code_metadata(
    payload_root: Path,
    metadata: dict | None = None,
    file_records: list[dict] | None = None,
    *,
    self_ref: str | None = None,
) -> dict:
    """The record for a source tree: what upstream said about the repository
    (``metadata["repository"]``, as the provider recorded it at pin time)
    and what the inventory shows. Nothing here asserts a purpose."""
    repo = (metadata or {}).get("repository") or {}
    paths = [
        r["path"].split("/", 1)[1] if "/" in r["path"] else r["path"]
        for r in (file_records or [])
    ]
    if not paths:
        paths = [
            p.relative_to(payload_root).as_posix()
            for p in payload_root.rglob("*")
            if p.is_file() and ".cache" not in p.relative_to(payload_root).parts
        ]
    languages = repo.get("languages")
    if not isinstance(languages, dict) or not languages:
        languages = None
    return {
        "description": repo.get("description"),
        "references": scan_payload_references(payload_root, self_ref=self_ref),
        "homepage": repo.get("homepage"),
        "topics": list(repo.get("topics") or []) or None,
        "languages": languages,
        "default_branch": repo.get("default_branch"),
        "archived_upstream": repo.get("archived_upstream"),
        "submodules": repo.get("submodules"),
        "symlinks": repo.get("symlinks"),
        "runtime_declarations": runtime_declarations(paths),
    }


def scan_payload_references(payload_root: Path, *, self_ref: str | None = None) -> dict:
    """``code_metadata.references`` from the tree on disk — offline, bounded
    by ``references.LOCAL_SCAN``, binaries counted and not read."""
    from .hashing import iter_payload_files
    from .references import LOCAL_SCAN, scan_references, select_scan_files

    listed = iter_payload_files(payload_root)
    sizes = {}
    for rel, path in listed:
        try:
            sizes[rel] = path.stat().st_size
        except OSError:
            sizes[rel] = None
    chosen, skipped = select_scan_files(list(sizes.items()), LOCAL_SCAN)
    by_rel = dict(listed)
    files = []
    for rel in chosen:
        try:
            files.append((rel, by_rel[rel].read_bytes()))
        except OSError:
            skipped["unreadable"] = skipped.get("unreadable", 0) + 1
    return scan_references(files, skipped=skipped, self_ref=self_ref)


def estimate_runtime(payload_root: Path, model_metadata: dict) -> dict:
    """Runtime requirements. Sizes are estimates from weight bytes; tested_hardware
    stays null until a curator actually runs the model somewhere."""
    files = [
        {"path": p.relative_to(payload_root).as_posix(), "size": p.stat().st_size}
        for p in payload_root.rglob("*")
        if p.is_file() and p.suffix.lower() in (".safetensors", ".bin", ".gguf")
    ]
    weight_bytes = model_weight_bytes(files)
    est_gb = round(weight_bytes * 1.2 / 1024**3, 1) if weight_bytes else None

    has_safetensors = any(f["path"].endswith(".safetensors") for f in files)
    has_gguf = any(f["path"].endswith(".gguf") for f in files)
    engines = []
    if has_safetensors:
        engines.append("transformers")
    if has_gguf:
        engines.append("llama.cpp")

    return {
        "supported_engines": engines or None,
        "estimated_min_ram_gb": est_gb,
        "estimated_min_vram_gb": est_gb,
        "tested_hardware": None,
        "os_support": ["linux", "macos", "windows"]
        if has_safetensors or has_gguf
        else None,
        "cuda_notes": None,
        "rocm_notes": None,
        "cpu_inference": True if (has_safetensors or has_gguf) else None,
        "notes": "RAM/VRAM is not estimated for a GGUF pack or an incomplete model selection. Engines are listed from shipped formats only."
        if has_gguf and weight_bytes is None
        else "RAM/VRAM figures are estimates (weight bytes x1.2); engines listed from shipped formats only.",
    }

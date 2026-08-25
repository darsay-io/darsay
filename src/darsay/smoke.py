"""Smoke tests: does the archived payload actually work?

Model bundles: the tokenizer test needs only the `tokenizers` package (or
transformers as a fallback); the inference test needs transformers + torch and
is opt-in. Dataset bundles: stdlib-only structural checks (parquet magic
bytes, JSONL first-line parse, CSV/TSV dialect sniff). Results are recorded in
manifest.validation.smoke_tests.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

SAMPLE_TEXT = "The quick brown fox jumps over the lazy dog. 数字123 test."
PROMPT = "The capital of France is"

PARQUET_MAGIC = b"PAR1"
CSV_SNIFF_BYTES = 64 * 1024
JSONL_FIRST_LINE_CAP = 10 * 1024 * 1024


def tokenizer_test(payload_root: Path) -> dict:
    tokenizer_json = payload_root / "tokenizer.json"
    try:
        from tokenizers import Tokenizer
    except ImportError:
        Tokenizer = None

    if Tokenizer is not None and tokenizer_json.is_file():
        try:
            tok = Tokenizer.from_file(str(tokenizer_json))
            enc = tok.encode(SAMPLE_TEXT)
            decoded = tok.decode(enc.ids)
            ok = "quick brown fox" in decoded
            return {
                "status": "pass" if ok else "fail",
                "engine": "tokenizers",
                "token_count": len(enc.ids),
                "roundtrip_exact": decoded == SAMPLE_TEXT,
            }
        except Exception as exc:  # archive tool: record the failure, don't crash
            return {"status": "fail", "engine": "tokenizers", "error": str(exc)}

    try:
        from transformers import AutoTokenizer
        tok = AutoTokenizer.from_pretrained(str(payload_root))
        ids = tok.encode(SAMPLE_TEXT)
        decoded = tok.decode(ids, skip_special_tokens=True)
        ok = "quick brown fox" in decoded
        return {"status": "pass" if ok else "fail", "engine": "transformers",
                "token_count": len(ids), "roundtrip_exact": decoded == SAMPLE_TEXT}
    except ImportError:
        if Tokenizer is not None:
            return {"status": "skipped",
                    "reason": "payload has no tokenizer.json; install `transformers` for the slow-tokenizer fallback"}
        return {"status": "skipped", "reason": "neither `tokenizers` nor `transformers` installed"}
    except Exception as exc:
        return {"status": "fail", "engine": "transformers", "error": str(exc)}


def inference_test(payload_root: Path, max_new_tokens: int = 8) -> dict:
    try:
        import torch
        from transformers import AutoModelForCausalLM, AutoTokenizer
    except ImportError:
        return {"status": "skipped", "reason": "transformers/torch not installed (install darsay[inference])"}
    try:
        tok = AutoTokenizer.from_pretrained(str(payload_root))
        model = AutoModelForCausalLM.from_pretrained(str(payload_root), torch_dtype="auto")
        model.eval()
        inputs = tok(PROMPT, return_tensors="pt")
        with torch.no_grad():
            out = model.generate(**inputs, max_new_tokens=max_new_tokens, do_sample=False)
        text = tok.decode(out[0], skip_special_tokens=True)
        return {"status": "pass", "engine": "transformers", "prompt": PROMPT,
                "output": text, "new_tokens": int(out.shape[-1] - inputs["input_ids"].shape[-1])}
    except Exception as exc:
        return {"status": "fail", "engine": "transformers", "error": str(exc)}


# ---------------------------------------------------- dataset structure checks

def _check_parquet(path: Path) -> str | None:
    if path.stat().st_size < 12:  # PAR1 + footer + PAR1 minimum
        return "file too small to be a parquet file"
    with open(path, "rb") as f:
        head = f.read(4)
        f.seek(-4, 2)
        tail = f.read(4)
    if head != PARQUET_MAGIC or tail != PARQUET_MAGIC:
        return "missing PAR1 magic at head/tail"
    return None


def _check_jsonl(path: Path) -> str | None:
    with open(path, "rb") as f:
        first = f.readline(JSONL_FIRST_LINE_CAP)
    if not first.strip():
        return "first line is empty"
    try:
        json.loads(first)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        return f"first line is not valid JSON: {exc}"
    return None


def _check_csv(path: Path) -> str | None:
    with open(path, "r", encoding="utf-8", errors="replace", newline="") as f:
        sample = f.read(CSV_SNIFF_BYTES)
    if not sample.strip():
        return "file is empty"
    try:
        csv.Sniffer().sniff(sample)
    except csv.Error as exc:
        return f"cannot sniff CSV dialect: {exc}"
    return None


DATASET_CHECKS = {
    ".parquet": _check_parquet,
    ".jsonl": _check_jsonl,
    ".csv": _check_csv,
    ".tsv": _check_csv,
}


def structure_test(payload_root: Path) -> dict:
    """Stdlib-only structural checks on dataset payload files: a real
    integrity signal (truncation, wrong format, encoding damage) with zero
    heavy dependencies."""
    from .hashing import iter_payload_files

    checked = passed = 0
    failures = []
    by_format: dict[str, dict] = {}
    for rel, abs_path in iter_payload_files(payload_root):
        check = DATASET_CHECKS.get(Path(rel).suffix.lower())
        if check is None:
            continue
        fmt = Path(rel).suffix.lower().lstrip(".")
        stats = by_format.setdefault(fmt, {"checked": 0, "failed": 0})
        stats["checked"] += 1
        checked += 1
        try:
            error = check(abs_path)
        except Exception as exc:  # record the failure, never crash
            error = str(exc)
        if error:
            stats["failed"] += 1
            failures.append({"path": rel, "error": error})
        else:
            passed += 1
    if not checked:
        return {"status": "skipped", "reason": "no parquet/jsonl/csv/tsv files to check"}
    return {
        "status": "pass" if not failures else "fail",
        "files_checked": checked,
        "by_format": by_format,
        "failures": failures or None,
    }


def run_smoke(bundle_dir: Path, inference: bool = False, progress=print) -> dict:
    from .archiver import load_manifest, utc_now, write_manifest
    from .schema import payload_root as manifest_payload_root

    manifest = load_manifest(bundle_dir)
    payload_root = bundle_dir / manifest_payload_root(manifest)
    now = utc_now()

    if manifest["artifact_type"] == "dataset":
        progress("Running dataset structure checks ...")
        result = {"at": now, **structure_test(payload_root)}
        progress(f"  structure: {result['status']}")
        if inference:
            progress("  (--inference applies to model bundles; ignored)")
        manifest["validation"]["smoke_tests"] = {"structure": result}
    else:
        progress("Running tokenizer smoke test ...")
        tok_result = {"at": now, **tokenizer_test(payload_root)}
        progress(f"  tokenizer: {tok_result['status']}")

        if inference:
            progress("Running inference smoke test (loads the model) ...")
            inf_result = {"at": now, **inference_test(payload_root)}
            progress(f"  inference: {inf_result['status']}")
        else:
            inf_result = manifest["validation"]["smoke_tests"].get("inference", {"status": "not-run"})

        manifest["validation"]["smoke_tests"] = {"tokenizer": tok_result, "inference": inf_result}

    manifest["archive"]["last_accessed"] = now
    write_manifest(bundle_dir, manifest)
    return manifest["validation"]["smoke_tests"]

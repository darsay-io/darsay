"""Smoke tests: does the archived payload actually work?

Tokenizer test needs only the `tokenizers` package (or transformers as a
fallback); the inference test needs transformers + torch and is opt-in.
Results are recorded in manifest.validation.smoke_tests.
"""

from __future__ import annotations

from pathlib import Path

SAMPLE_TEXT = "The quick brown fox jumps over the lazy dog. 数字123 test."
PROMPT = "The capital of France is"


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
        return {"status": "skipped", "reason": "transformers/torch not installed (install modelvault[inference])"}
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


def run_smoke(bundle_dir: Path, inference: bool = False, progress=print) -> dict:
    from .archiver import load_manifest, utc_now, write_manifest

    manifest = load_manifest(bundle_dir)
    payload_root = bundle_dir / "model"
    now = utc_now()

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

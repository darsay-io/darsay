"""Run an archived GGUF payload with llama-cpp-python.

Standalone: executed by modelvault with the hydrated env's python; imports
only stdlib + llama_cpp. Writes a JSON result to --json-out and streams
generated text to stdout. Never writes into the model directory.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path


def write_result(path: str, data: dict) -> None:
    Path(path).write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def versions() -> dict:
    import llama_cpp
    return {"llama_cpp_python": llama_cpp.__version__}


def probe(args) -> dict:
    import llama_cpp

    weights = Path(args.weights)
    if not weights.is_file():
        return {"status": "fail", "engine": "llama-cpp", "error": f"weights not found: {weights}"}
    return {
        "status": "pass",
        "engine": "llama-cpp",
        "versions": versions(),
        "devices": {"gpu_offload": bool(llama_cpp.llama_supports_gpu_offload()), "cpu": True},
        "weights": str(weights),
        "weights_size_bytes": weights.stat().st_size,
    }


def generate(args) -> dict:
    from llama_cpp import Llama

    n_gpu_layers = 0 if args.device == "cpu" else -1
    print(f"[runner] loading {args.weights} (n_gpu_layers={n_gpu_layers})", file=sys.stderr)

    t0 = time.perf_counter()
    llm = Llama(model_path=args.weights, n_ctx=4096, n_gpu_layers=n_gpu_layers,
                seed=args.seed if args.seed is not None else -1, verbose=False)
    load_seconds = time.perf_counter() - t0

    # Greedy unless --sample (temperature 0 is llama.cpp's greedy mode).
    temperature = 0.8 if args.sample else 0.0
    t1 = time.perf_counter()
    if args.raw:
        resp = llm(args.prompt, max_tokens=args.max_new_tokens, temperature=temperature)
        choice = resp["choices"][0]
        text = choice["text"]
        prompt_mode = "raw-completion"
    else:
        # Uses the chat template embedded in the GGUF when present,
        # otherwise llama-cpp-python's fallback template.
        resp = llm.create_chat_completion(
            messages=[{"role": "user", "content": args.prompt}],
            max_tokens=args.max_new_tokens, temperature=temperature)
        choice = resp["choices"][0]
        text = choice["message"]["content"]
        prompt_mode = "chat-template"
    generate_seconds = time.perf_counter() - t1

    print(text)
    new_tokens = resp["usage"]["completion_tokens"]
    return {
        "status": "pass",
        "engine": "llama-cpp",
        "versions": versions(),
        "device": "cpu" if n_gpu_layers == 0 else "gpu-offload",
        "dtype": None,  # baked into the GGUF quantization
        "prompt": args.prompt,
        "prompt_mode": prompt_mode,
        "sampling": {"do_sample": args.sample, "temperature": temperature},
        "output": text,
        "new_tokens": new_tokens,
        "stop_reason": "eos" if choice.get("finish_reason") == "stop" else "length",
        "load_seconds": round(load_seconds, 2),
        "generate_seconds": round(generate_seconds, 2),
        "tokens_per_second": round(new_tokens / generate_seconds, 1) if generate_seconds > 0 else None,
    }


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--model-dir", required=True, help="payload directory (context only)")
    p.add_argument("--weights", required=True, help="path to the .gguf file")
    p.add_argument("--json-out", required=True)
    p.add_argument("--probe", action="store_true")
    p.add_argument("--prompt", default="Say hello in one short sentence.")
    p.add_argument("--max-new-tokens", type=int, default=256)
    p.add_argument("--device", default="auto", help="auto (GPU offload when built with it) | cpu")
    p.add_argument("--raw", action="store_true", help="plain completion, skip the chat template")
    p.add_argument("--sample", action="store_true")
    p.add_argument("--seed", type=int, default=None)
    p.add_argument("--trust-remote-code", action="store_true", help="ignored for llama.cpp")
    args = p.parse_args()

    try:
        result = probe(args) if args.probe else generate(args)
    except Exception as exc:  # runner contract: report, never crash silently
        write_result(args.json_out, {"status": "fail", "engine": "llama-cpp",
                                     "error": f"{type(exc).__name__}: {exc}"})
        print(f"[runner] FAILED: {exc}", file=sys.stderr)
        return 1
    write_result(args.json_out, result)
    return 0 if result.get("status") == "pass" else 1


if __name__ == "__main__":
    sys.exit(main())

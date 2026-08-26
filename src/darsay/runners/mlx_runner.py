"""Run an archived model payload with Apple MLX (mlx-lm).

Standalone: executed by darsay with the hydrated env's python; imports
only stdlib + mlx / mlx_lm. Writes a JSON result to --json-out and
streams generated text to stdout. Never writes into the model directory.
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
    import mlx
    import mlx_lm
    return {
        "mlx": getattr(mlx, "__version__", None),
        "mlx_lm": getattr(mlx_lm, "__version__", None),
    }


def probe(args) -> dict:
    import mlx
    import mlx_lm  # noqa: F401  — import is the env check
    return {
        "status": "pass",
        "engine": "mlx",
        "versions": versions(),
        "devices": {"metal": sys.platform == "darwin", "cpu": True},
        "mlx_default_device": str(getattr(mlx, "default_device", lambda: None)()),
        "model_dir": args.model_dir,
    }


def _format_prompt(tokenizer, prompt: str, raw: bool, messages=None) -> tuple[str, bool]:
    use_chat = (not raw) and getattr(tokenizer, "chat_template", None)
    if not use_chat:
        return prompt, False
    history = list(messages or []) + [{"role": "user", "content": prompt}]
    text = tokenizer.apply_chat_template(history, tokenize=False, add_generation_prompt=True)
    return text, True


def _generate(model, tokenizer, prompt: str, args) -> str:
    from mlx_lm import generate

    kwargs = {"max_tokens": args.max_new_tokens, "verbose": False}
    if not args.sample:
        try:
            from mlx_lm.sample_utils import make_sampler
            kwargs["sampler"] = make_sampler(temp=0)
        except Exception:
            pass
    return generate(model, tokenizer, prompt=prompt, **kwargs)


def generate(args) -> dict:
    from mlx_lm import load

    print(f"[runner] loading {args.model_dir} (engine=mlx)", file=sys.stderr)
    t0 = time.perf_counter()
    model, tokenizer = load(args.model_dir)
    load_seconds = time.perf_counter() - t0
    formatted, use_chat = _format_prompt(tokenizer, args.prompt, args.raw)
    t1 = time.perf_counter()
    text = _generate(model, tokenizer, formatted, args)
    generate_seconds = time.perf_counter() - t1
    print(text, flush=True)
    # mlx-lm returns the full completion string; token counts are not always exposed.
    return {
        "status": "pass",
        "engine": "mlx",
        "versions": versions(),
        "device": "metal" if sys.platform == "darwin" else "cpu",
        "dtype": None,
        "prompt": args.prompt,
        "prompt_mode": "chat-template" if use_chat else "raw-completion",
        "sampling": {"do_sample": bool(args.sample)},
        "output": text,
        "new_tokens": None,
        "stop_reason": None,
        "load_seconds": round(load_seconds, 2),
        "generate_seconds": round(generate_seconds, 2),
        "tokens_per_second": None,
    }


def repl(args) -> dict:
    from mlx_lm import load

    print(f"[runner] loading {args.model_dir} (engine=mlx)", file=sys.stderr)
    t0 = time.perf_counter()
    model, tokenizer = load(args.model_dir)
    load_seconds = time.perf_counter() - t0
    print("[repl] model loaded. Type a prompt; /quit to exit.", file=sys.stderr)
    messages = []
    last = None
    pending = args.prompt
    while True:
        if pending is not None:
            user, pending = pending, None
        else:
            try:
                user = input("> ")
            except (EOFError, KeyboardInterrupt):
                print(file=sys.stderr)
                break
        user = user.strip()
        if not user:
            continue
        if user in ("/quit", "/exit"):
            break
        formatted, use_chat = _format_prompt(tokenizer, user, args.raw, messages=messages)
        t1 = time.perf_counter()
        text = _generate(model, tokenizer, formatted, args)
        generate_seconds = time.perf_counter() - t1
        print(text, flush=True)
        if use_chat:
            messages.append({"role": "user", "content": user})
            messages.append({"role": "assistant", "content": text})
        last = {
            "status": "pass",
            "engine": "mlx",
            "versions": versions(),
            "device": "metal" if sys.platform == "darwin" else "cpu",
            "dtype": None,
            "prompt": user,
            "prompt_mode": "chat-template" if use_chat else "raw-completion",
            "sampling": {"do_sample": bool(args.sample)},
            "output": text,
            "new_tokens": None,
            "stop_reason": None,
            "load_seconds": round(load_seconds, 2),
            "generate_seconds": round(generate_seconds, 2),
            "tokens_per_second": None,
            "repl": True,
        }
    return last or {"status": "fail", "engine": "mlx", "error": "repl ended without a prompt"}


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--model-dir", required=True)
    p.add_argument("--json-out", required=True)
    p.add_argument("--probe", action="store_true")
    p.add_argument("--prompt", default=None)
    p.add_argument("--repl", action="store_true")
    p.add_argument("--max-new-tokens", type=int, default=256)
    p.add_argument("--device", default="auto")
    p.add_argument("--raw", action="store_true")
    p.add_argument("--sample", action="store_true")
    p.add_argument("--seed", type=int, default=None)
    p.add_argument("--trust-remote-code", action="store_true")
    p.add_argument("--weights", default=None, help="ignored (mlx-lm loads the directory)")
    p.add_argument("--dtype", default="auto", help="ignored")
    args = p.parse_args()

    try:
        if args.probe:
            result = probe(args)
        elif args.repl:
            result = repl(args)
        else:
            if not args.prompt:
                args.prompt = "Say hello in one short sentence."
            result = generate(args)
    except Exception as exc:
        write_result(args.json_out, {"status": "fail", "engine": "mlx",
                                     "error": f"{type(exc).__name__}: {exc}"})
        print(f"[runner] FAILED: {exc}", file=sys.stderr)
        return 1
    write_result(args.json_out, result)
    return 0 if result.get("status") == "pass" else 1


if __name__ == "__main__":
    sys.exit(main())

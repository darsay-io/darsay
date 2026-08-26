"""Run an archived model payload with Hugging Face transformers.

Standalone: executed by darsay with the hydrated env's python; imports
only stdlib + torch/transformers. Writes a JSON result to --json-out and
streams generated text to stdout. Never writes into the model directory.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path


def write_result(path: str, data: dict) -> None:
    Path(path).write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


def versions() -> dict:
    import torch
    import transformers

    return {"torch": torch.__version__, "transformers": transformers.__version__}


def resolve_device(requested: str) -> str:
    import torch

    if requested != "auto":
        return requested
    if torch.cuda.is_available():
        return "cuda"
    if getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
        return "mps"
    return "cpu"


def probe(args) -> dict:
    import torch
    from transformers import AutoConfig, AutoTokenizer

    result = {
        "status": "pass",
        "engine": "transformers",
        "versions": versions(),
        "devices": {
            "cuda": torch.cuda.is_available(),
            "mps": bool(
                getattr(torch.backends, "mps", None)
                and torch.backends.mps.is_available()
            ),
            "cpu": True,
        },
    }
    config = AutoConfig.from_pretrained(
        args.model_dir, trust_remote_code=args.trust_remote_code
    )
    result["architecture"] = (getattr(config, "architectures", None) or [None])[0]
    tok = AutoTokenizer.from_pretrained(
        args.model_dir, trust_remote_code=args.trust_remote_code
    )
    result["tokenizer"] = {
        "status": "pass",
        "vocab_size": tok.vocab_size,
        "chat_template_present": tok.chat_template is not None,
    }
    return result


def load_model(args):
    from transformers import AutoModelForCausalLM

    try:
        return AutoModelForCausalLM.from_pretrained(
            args.model_dir, dtype=args.dtype, trust_remote_code=args.trust_remote_code
        )
    except TypeError:  # transformers < 4.56 spells it torch_dtype
        return AutoModelForCausalLM.from_pretrained(
            args.model_dir,
            torch_dtype=args.dtype,
            trust_remote_code=args.trust_remote_code,
        )


def _complete(model, tok, device, prompt, args, messages=None):
    import torch

    use_chat = tok.chat_template is not None and not args.raw
    if use_chat:
        history = list(messages or []) + [{"role": "user", "content": prompt}]
        encoded = tok.apply_chat_template(
            history,
            add_generation_prompt=True,
            return_tensors="pt",
        )
        if hasattr(encoded, "keys"):
            encoded = encoded["input_ids"]
        input_ids = encoded.to(device)
    else:
        input_ids = tok(prompt, return_tensors="pt").input_ids.to(device)

    gen_kwargs = {"max_new_tokens": args.max_new_tokens}
    if args.sample:
        if args.seed is not None:
            torch.manual_seed(args.seed)
    else:
        gen_kwargs.update(do_sample=False, temperature=None, top_p=None, top_k=None)

    t1 = time.perf_counter()
    with torch.no_grad():
        out = model.generate(input_ids, **gen_kwargs)
    generate_seconds = time.perf_counter() - t1
    new_tokens = int(out.shape[-1] - input_ids.shape[-1])
    text = tok.decode(out[0, input_ids.shape[-1] :], skip_special_tokens=True)
    print(text, flush=True)
    gc = model.generation_config
    sampling = (
        {
            "do_sample": True,
            "temperature": gc.temperature,
            "top_p": gc.top_p,
            "top_k": gc.top_k,
        }
        if args.sample
        else {"do_sample": False}
    )
    return text, new_tokens, generate_seconds, use_chat, sampling


def generate(args) -> dict:
    from transformers import AutoTokenizer

    device = resolve_device(args.device)
    print(
        f"[runner] loading {args.model_dir} (device={device}, dtype={args.dtype})",
        file=sys.stderr,
    )

    t0 = time.perf_counter()
    tok = AutoTokenizer.from_pretrained(
        args.model_dir, trust_remote_code=args.trust_remote_code
    )
    model = load_model(args)
    model.to(device)
    model.eval()
    load_seconds = time.perf_counter() - t0

    text, new_tokens, generate_seconds, use_chat, sampling = _complete(
        model,
        tok,
        device,
        args.prompt,
        args,
    )
    return {
        "status": "pass",
        "engine": "transformers",
        "versions": versions(),
        "device": device,
        "dtype": str(model.dtype).replace("torch.", ""),
        "prompt": args.prompt,
        "prompt_mode": "chat-template" if use_chat else "raw-completion",
        "sampling": sampling,
        "output": text,
        "new_tokens": new_tokens,
        "stop_reason": "eos" if new_tokens < args.max_new_tokens else "length",
        "load_seconds": round(load_seconds, 2),
        "generate_seconds": round(generate_seconds, 2),
        "tokens_per_second": round(new_tokens / generate_seconds, 1)
        if generate_seconds > 0
        else None,
    }


def _read_repl_line():
    try:
        return input("> ")
    except (EOFError, KeyboardInterrupt):
        print(file=sys.stderr)
        return None


def repl(args) -> dict:
    from transformers import AutoTokenizer

    device = resolve_device(args.device)
    print(
        f"[runner] loading {args.model_dir} (device={device}, dtype={args.dtype})",
        file=sys.stderr,
    )
    t0 = time.perf_counter()
    tok = AutoTokenizer.from_pretrained(
        args.model_dir, trust_remote_code=args.trust_remote_code
    )
    model = load_model(args)
    model.to(device)
    model.eval()
    load_seconds = time.perf_counter() - t0
    print("[repl] model loaded. Type a prompt; /quit to exit.", file=sys.stderr)

    messages = []
    last = None
    pending = args.prompt
    while True:
        if pending is not None:
            user = pending
            pending = None
        else:
            user = _read_repl_line()
            if user is None:
                break
        user = user.strip()
        if not user:
            continue
        if user in ("/quit", "/exit"):
            break
        text, new_tokens, generate_seconds, use_chat, sampling = _complete(
            model,
            tok,
            device,
            user,
            args,
            messages=messages,
        )
        if use_chat:
            messages.append({"role": "user", "content": user})
            messages.append({"role": "assistant", "content": text})
        last = {
            "status": "pass",
            "engine": "transformers",
            "versions": versions(),
            "device": device,
            "dtype": str(model.dtype).replace("torch.", ""),
            "prompt": user,
            "prompt_mode": "chat-template" if use_chat else "raw-completion",
            "sampling": sampling,
            "output": text,
            "new_tokens": new_tokens,
            "stop_reason": "eos" if new_tokens < args.max_new_tokens else "length",
            "load_seconds": round(load_seconds, 2),
            "generate_seconds": round(generate_seconds, 2),
            "tokens_per_second": round(new_tokens / generate_seconds, 1)
            if generate_seconds > 0
            else None,
            "repl": True,
        }
    return last or {
        "status": "fail",
        "engine": "transformers",
        "error": "repl ended without a prompt",
    }


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--model-dir", required=True)
    p.add_argument("--json-out", required=True)
    p.add_argument("--probe", action="store_true")
    p.add_argument("--prompt", default=None)
    p.add_argument(
        "--repl", action="store_true", help="interactive loop; model stays loaded"
    )
    p.add_argument("--max-new-tokens", type=int, default=256)
    p.add_argument("--device", default="auto", help="auto | cpu | cuda | mps")
    p.add_argument(
        "--dtype", default="auto", help="auto | float32 | bfloat16 | float16"
    )
    p.add_argument(
        "--raw", action="store_true", help="plain completion, skip the chat template"
    )
    p.add_argument(
        "--sample",
        action="store_true",
        help="sample with generation_config defaults (default: greedy)",
    )
    p.add_argument("--seed", type=int, default=None)
    p.add_argument("--trust-remote-code", action="store_true")
    p.add_argument(
        "--weights", default=None, help="ignored (transformers loads the directory)"
    )
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
    except Exception as exc:  # runner contract: report, never crash silently
        write_result(
            args.json_out,
            {
                "status": "fail",
                "engine": "transformers",
                "error": f"{type(exc).__name__}: {exc}",
            },
        )
        print(f"[runner] FAILED: {exc}", file=sys.stderr)
        return 1
    write_result(args.json_out, result)
    return 0 if result.get("status") == "pass" else 1


if __name__ == "__main__":
    sys.exit(main())

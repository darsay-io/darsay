from __future__ import annotations

import pytest

from darsay.hydrate import (
    ENGINES,
    _env_key,
    _offline_env,
    _record_tested_hardware,
    detect_engines,
    engine_supports_payload,
    pin_requirements,
    preflight_run,
    requirement_name,
    resolve_requirements,
    runtime_root,
    select_engine,
    select_weights,
)


def test_detect_transformers_and_llama_cpp():
    assert detect_engines(["model/config.json", "model/model.safetensors"]) == ["transformers"]
    assert detect_engines(["model/toy.gguf"]) == ["llama-cpp"]
    both = detect_engines(
        ["model/config.json", "model/model.safetensors", "model/toy.gguf"]
    )
    assert both == ["transformers", "llama-cpp"]  # registry preference order
    assert detect_engines(["model/README.md"]) == []


def test_select_engine_mlx_opt_in_on_safetensors():
    import platform
    import sys

    manifest = {
        "inventory": {
            "files": [
                {"path": "model/config.json"},
                {"path": "model/model.safetensors"},
            ]
        }
    }
    assert select_engine(manifest, None) == "transformers"
    assert detect_engines([f["path"] for f in manifest["inventory"]["files"]]) == ["transformers"]
    npz = detect_engines(["model/config.json", "model/weights.npz"])
    apple_silicon = sys.platform == "darwin" and platform.machine() in {"arm64", "aarch64"}
    if apple_silicon:
        assert select_engine(manifest, "mlx") == "mlx"
        assert "mlx" in npz
    else:
        with pytest.raises(SystemExit, match="darwin"):
            select_engine(manifest, "mlx")


def test_select_engine_auto_and_requested():
    manifest = {
        "inventory": {
            "files": [
                {"path": "model/config.json"},
                {"path": "model/model.safetensors"},
                {"path": "model/toy.gguf"},
            ]
        }
    }
    assert select_engine(manifest, None) == "transformers"
    assert select_engine(manifest, "llama-cpp") == "llama-cpp"
    with pytest.raises(SystemExit, match="unknown engine"):
        select_engine(manifest, "vllm")
    empty = {"inventory": {"files": [{"path": "model/README.md"}]}}
    with pytest.raises(SystemExit, match="no known engine"):
        select_engine(empty, None)


def test_select_weights_gguf():
    manifest = {
        "inventory": {
            "files": [
                {"path": "model/a.gguf"},
                {"path": "model/b.gguf"},
            ]
        }
    }
    with pytest.raises(SystemExit, match="candidate weights"):
        select_weights(manifest, "llama-cpp", None)
    assert select_weights(manifest, "llama-cpp", "model/a.gguf") == "model/a.gguf"
    assert select_weights(manifest, "transformers", None) is None


def test_engine_supports_causal_lm_and_rejects_vlm():
    causal = {"model_metadata": {"architecture": "Qwen3ForCausalLM"}}
    assert engine_supports_payload("transformers", causal) == (True, None)
    gpt2 = {"model_metadata": {"architecture": "GPT2LMHeadModel"}}
    assert engine_supports_payload("transformers", gpt2) == (True, None)
    assert engine_supports_payload("mlx", gpt2) == (True, None)
    vlm = {"model_metadata": {"architecture": "Qwen3VLForConditionalGeneration"}}
    ok, detail = engine_supports_payload("transformers", vlm)
    assert ok is False
    assert "not a causal LM" in detail
    assert "AutoModelForCausalLM" in detail
    mlx_ok, mlx_detail = engine_supports_payload("mlx", vlm)
    assert mlx_ok is False
    assert "mlx-lm" in mlx_detail
    unknown = {"model_metadata": {"architecture": None}}
    ok, detail = engine_supports_payload("transformers", unknown)
    assert ok is True
    assert "unknown" in detail
    assert engine_supports_payload("llama-cpp", vlm)[0] is True


def test_preflight_run_architecture_ram_and_install():
    manifest = {
        "model_metadata": {"architecture": "ToyForCausalLM"},
        "runtime": {"estimated_min_ram_gb": 24.0},
    }
    issues = preflight_run(
        manifest, "transformers", env_exists=False, ram_bytes=8 * 1024**3,
    )
    codes = {i["code"]: i["level"] for i in issues}
    assert codes["insufficient-ram"] == "error"
    assert codes["env-install"] == "info"

    vlm = {
        "model_metadata": {"architecture": "ToyForConditionalGeneration"},
        "runtime": {"estimated_min_ram_gb": 0.0},
    }
    vlm_issues = preflight_run(vlm, "transformers", env_exists=True, ram_bytes=64 * 1024**3)
    assert any(i["code"] == "unsupported-architecture" for i in vlm_issues)
    assert not any(i["code"] == "env-install" for i in vlm_issues)


def test_env_key_is_content_addressed():
    a = _env_key("transformers", "3.12", ["torch", "transformers"])
    b = _env_key("transformers", "3.12", ["torch", "transformers"])
    c = _env_key("transformers", "3.12", ["torch", "transformers>=4.40"])
    d = _env_key("transformers", "3.11", ["torch", "transformers"])
    assert a == b
    assert a != c
    assert a != d
    assert a.startswith("transformers-py3.12-")


def test_pin_requirements_uses_recorded_versions():
    assert requirement_name("transformers>=4.40.0") == "transformers"
    assert requirement_name("torch") == "torch"
    loose = ["torch", "transformers>=4.40.0"]
    assert pin_requirements(loose, None) == loose
    pinned = pin_requirements(loose, {"torch": "2.2.0", "transformers": "4.40.0"})
    assert pinned == ["torch==2.2.0", "transformers==4.40.0"]
    # Unknown extras in the pin set are ignored; unmatched reqs stay loose.
    assert pin_requirements(["mlx"], {"torch": "2.2.0"}) == ["mlx"]
    mlx_pinned = pin_requirements(
        ["mlx", "mlx-lm"], {"mlx": "0.22.0", "mlx-lm": "0.21.0"},
    )
    assert mlx_pinned == ["mlx-lm==0.21.0", "mlx==0.22.0"]


def test_resolve_requirements_uses_declared_floor(tmp_path):
    (tmp_path / "config.json").write_text('{"transformers_version": "4.40.0"}')
    reqs = resolve_requirements("transformers", tmp_path)
    assert "transformers>=4.40.0" in reqs
    assert "torch" in reqs
    gguf = resolve_requirements("llama-cpp", tmp_path)
    assert gguf == ["llama-cpp-python"]


def test_runtime_root_env_override(monkeypatch, tmp_path):
    bundle = tmp_path / "vault" / "name" / "rev"
    bundle.mkdir(parents=True)
    monkeypatch.delenv("DARSAY_RUNTIME", raising=False)
    assert runtime_root(bundle) == tmp_path / "vault" / ".runtime"
    monkeypatch.setenv("DARSAY_RUNTIME", str(tmp_path / "rt"))
    assert runtime_root(bundle) == tmp_path / "rt"


def test_offline_env_sets_hub_offline(monkeypatch):
    monkeypatch.setenv("HF_HUB_OFFLINE", "0")
    env = _offline_env()
    assert env["HF_HUB_OFFLINE"] == "1"
    assert env["TRANSFORMERS_OFFLINE"] == "1"


def test_every_engine_ships_a_runner():
    from pathlib import Path

    import darsay

    root = Path(darsay.__file__).parent / "runners"
    missing = [spec["runner"] for spec in ENGINES.values() if not (root / spec["runner"]).is_file()]
    assert missing == []


def test_record_tested_hardware_leaves_schema_version_alone():
    manifest = {"schema_version": "1.6.0", "runtime": {"tested_hardware": None}}
    _record_tested_hardware(
        manifest,
        {"device": "cpu", "engine": "transformers", "tokens_per_second": 1.0},
        {"versions": {}},
        "2026-01-01T00:00:00+00:00",
    )
    assert manifest["schema_version"] == "1.6.0"
    assert manifest["runtime"]["tested_hardware"][0]["engine"] == "transformers"

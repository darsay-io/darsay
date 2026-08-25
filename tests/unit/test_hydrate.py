from __future__ import annotations

import pytest

from darsay.hydrate import (
    ENGINES,
    _env_key,
    _offline_env,
    detect_engines,
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


def test_env_key_is_content_addressed():
    a = _env_key("transformers", "3.12", ["torch", "transformers"])
    b = _env_key("transformers", "3.12", ["torch", "transformers"])
    c = _env_key("transformers", "3.12", ["torch", "transformers>=4.40"])
    d = _env_key("transformers", "3.11", ["torch", "transformers"])
    assert a == b
    assert a != c
    assert a != d
    assert a.startswith("transformers-py3.12-")


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

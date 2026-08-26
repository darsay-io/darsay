from __future__ import annotations

import json
import sys

import pytest

from darsay.archiver import load_manifest
from darsay.hydrate import dehydrate_bundle, hydrate_bundle, list_envs, prune_envs, run_bundle
from tests.conftest import silent
from tests.integration.conftest import archive_quiet
from tests.payloads import model_files


def _fake_hydration(bundle):
    return {
        "hydration_schema": 1,
        "bundle_id": load_manifest(bundle)["bundle_id"],
        "engine": "transformers",
        "weights": None,
        "env": {
            "key": "transformers-py3.14-deadbeef",
            "path": "/tmp/fake-env",
            "python": "3.14.0",
            "python_executable": sys.executable,
        },
        "runs": [],
    }


def test_run_bundle_records_pass_without_installing(vault, test_provider, monkeypatch):
    test_provider.add_repo("acme/toy", model_files())
    bundle = archive_quiet("test:acme/toy", vault=vault)
    payload = (bundle / "model" / "model.safetensors").read_bytes()
    schema = load_manifest(bundle)["schema_version"]
    (bundle / "hydration.json").write_text(json.dumps(_fake_hydration(bundle)) + "\n")

    def fake_invoke(env_record, engine, runner_args, timeout=None):
        assert engine == "transformers"
        assert env_record["python_executable"] == sys.executable
        assert "--prompt" in runner_args
        assert runner_args[runner_args.index("--prompt") + 1] == "Hi there"
        return 0, {
            "status": "pass",
            "output": "hello from stub",
            "new_tokens": 4,
            "device": "cpu",
            "dtype": "float32",
            "prompt_mode": "chat-template",
            "load_seconds": 0.1,
            "generate_seconds": 0.2,
            "tokens_per_second": 20.0,
            "versions": {"torch": "0.0", "transformers": "0.0"},
        }

    monkeypatch.setattr("darsay.hydrate._invoke_runner", fake_invoke)
    record = run_bundle(bundle, prompt="Hi there", progress=silent)
    assert record["status"] == "pass"
    assert record["new_tokens"] == 4
    hyd = json.loads((bundle / "hydration.json").read_text())
    assert hyd["runs"][-1]["output"] == "hello from stub"
    manifest = load_manifest(bundle)
    assert manifest["schema_version"] == schema
    assert manifest["runtime"]["tested_hardware"][0]["engine"] == "transformers"
    assert (bundle / "model" / "model.safetensors").read_bytes() == payload


def test_hydrate_preflight_rejects_non_causal_architecture(vault, test_provider, tmp_path, monkeypatch):
    files = model_files()
    config = json.loads(files["config.json"])
    config["architectures"] = ["ToyForConditionalGeneration"]
    files["config.json"] = json.dumps(config).encode()
    test_provider.add_repo("acme/vlm", files)
    bundle = archive_quiet("test:acme/vlm", vault=vault)
    monkeypatch.setenv("DARSAY_RUNTIME", str(tmp_path / "runtime"))
    with pytest.raises(SystemExit, match="not a causal LM"):
        hydrate_bundle(bundle, dry_run=True, progress=silent)
    record = hydrate_bundle(
        bundle, dry_run=True, ignore_preflight=True, progress=silent,
    )
    assert record["dry_run"] is True
    assert any(i["code"] == "unsupported-architecture" for i in record["preflight"])


def test_hydrate_dry_run_rebuilds_from_pinned_packages(vault, test_provider, monkeypatch, tmp_path):
    test_provider.add_repo("acme/toy", model_files())
    bundle = archive_quiet("test:acme/toy", vault=vault)
    runtime = tmp_path / "runtime"
    monkeypatch.setenv("DARSAY_RUNTIME", str(runtime))
    (bundle / "hydration.json").write_text(
        json.dumps(
            {
                "hydration_schema": 1,
                "bundle_id": "x",
                "engine": "transformers",
                "engine_packages": {"torch": "2.2.0", "transformers": "4.40.0"},
                "env": {"key": "gone", "python_executable": str(tmp_path / "missing")},
                "runs": [{"status": "pass"}],
            }
        )
        + "\n"
    )
    record = hydrate_bundle(bundle, dry_run=True, progress=silent)
    assert record["rebuild_from_pins"] is True
    assert "torch==2.2.0" in record["requirements"]
    assert "transformers==4.40.0" in record["requirements"]
    assert not runtime.exists()


def test_hydrate_dry_run_creates_nothing(vault, test_provider, monkeypatch, tmp_path):
    test_provider.add_repo("acme/toy", model_files())
    bundle = archive_quiet("test:acme/toy", vault=vault)
    runtime = tmp_path / "runtime"
    monkeypatch.setenv("DARSAY_RUNTIME", str(runtime))
    record = hydrate_bundle(bundle, dry_run=True, progress=silent)
    assert record["dry_run"] is True
    assert record["engine"] == "transformers"
    assert "transformers>=4.40.0" in record["requirements"]
    assert not runtime.exists()
    assert not (bundle / "hydration.json").exists()


def test_dehydrate_does_not_touch_payload(vault, test_provider):
    test_provider.add_repo("acme/toy", model_files())
    bundle = archive_quiet("test:acme/toy", vault=vault)
    payload = (bundle / "model" / "model.safetensors").read_bytes()
    (bundle / "hydration.json").write_text(
        json.dumps(
            {
                "hydration_schema": 1,
                "bundle_id": "x",
                "engine": "transformers",
                "env": {"key": "fake"},
                "runs": [{"status": "pass"}],
            }
        )
        + "\n"
    )
    dehydrate_bundle(bundle, progress=silent)
    assert not (bundle / "hydration.json").exists()
    assert (bundle / "model" / "model.safetensors").read_bytes() == payload
    assert (bundle / "manifest.json").is_file()


def test_prune_envs_only_removes_unreferenced(vault, tmp_path, monkeypatch):
    runtime = tmp_path / "runtime"
    env_dir = runtime / "envs" / "transformers-py3.12-deadbeef"
    env_dir.mkdir(parents=True)
    (env_dir / "env.json").write_text(
        json.dumps(
            {
                "key": "transformers-py3.12-deadbeef",
                "python": "3.12.0",
                "created_at": "2026-01-01T00:00:00+00:00",
            }
        )
    )
    (env_dir / "dummy").write_bytes(b"x" * 10)
    monkeypatch.setenv("DARSAY_RUNTIME", str(runtime))
    envs = list_envs(vault, progress=silent)
    assert envs[0]["used_by"] == []
    freed = prune_envs(vault, progress=silent)
    assert freed >= 10
    assert not env_dir.exists()

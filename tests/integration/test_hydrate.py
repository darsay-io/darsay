from __future__ import annotations

import json

from modelvault.hydrate import dehydrate_bundle, hydrate_bundle, list_envs, prune_envs
from tests.conftest import silent
from tests.integration.conftest import archive_quiet
from tests.payloads import model_files


def test_hydrate_dry_run_creates_nothing(vault, test_provider, monkeypatch, tmp_path):
    test_provider.add_repo("acme/toy", model_files())
    bundle = archive_quiet("test:acme/toy", vault=vault)
    runtime = tmp_path / "runtime"
    monkeypatch.setenv("MODELVAULT_RUNTIME", str(runtime))
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
    monkeypatch.setenv("MODELVAULT_RUNTIME", str(runtime))
    envs = list_envs(vault, progress=silent)
    assert envs[0]["used_by"] == []
    freed = prune_envs(vault, progress=silent)
    assert freed >= 10
    assert not env_dir.exists()

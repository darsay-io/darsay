from __future__ import annotations

import json

import pytest

from darsay.archiver import (
    _guess_version,
    _warn_include_vs_pin,
    archive_model,
    bundle_dir_for,
    bundle_name_for,
    hub_url,
    load_manifest,
    parse_repo_ref,
    write_manifest,
)
from darsay.schema import MANIFEST_KIND, MANIFEST_TOP_KEYS
from darsay.sources import parse_source


def test_parse_repo_ref_model_and_dataset():
    assert parse_repo_ref("Qwen/Qwen3-0.6B") == ("model", "Qwen/Qwen3-0.6B")
    assert parse_repo_ref("datasets/owner/name") == ("dataset", "owner/name")


def test_hub_url():
    assert hub_url("Qwen/Qwen3-0.6B") == "https://huggingface.co/Qwen/Qwen3-0.6B"
    assert hub_url("owner/name", "dataset") == "https://huggingface.co/datasets/owner/name"


def test_bundle_name_for_stable_huggingface_layout():
    assert bundle_name_for("Qwen/Qwen3-0.6B") == "qwen--qwen3-0.6b"
    assert bundle_name_for("owner/name", "dataset") == "datasets--owner--name"


def test_bundle_dir_for_uses_revision_prefix(tmp_path):
    dest = bundle_dir_for(tmp_path, "Qwen/Qwen3-0.6B", "c1899de289a0deadbeef")
    assert dest == tmp_path / "qwen--qwen3-0.6b" / "c1899de289a0"
    ref = parse_source("Qwen/Qwen3-0.6B")
    assert bundle_dir_for(tmp_path, ref, "c1899de289a0deadbeef") == dest


def test_archive_model_wrapper_builds_source_ref(monkeypatch, tmp_path):
    seen = {}

    def fake_archive(source, **kwargs):
        seen["source"] = source
        seen["kwargs"] = kwargs
        return tmp_path

    monkeypatch.setattr("darsay.archiver.archive", fake_archive)
    assert archive_model("Qwen/Qwen3-0.6B", vault=tmp_path) == tmp_path
    assert seen["source"] == "Qwen/Qwen3-0.6B"
    archive_model("owner/name", repo_type="dataset", vault=tmp_path, force=True)
    assert seen["source"] == "datasets/owner/name"
    assert seen["kwargs"]["force"] is True


def test_estimate_repo_wrapper_builds_source_ref(monkeypatch, tmp_path):
    from darsay.estimate import estimate_repo

    seen = {}

    def fake_estimate(source, **kwargs):
        seen["source"] = source
        return {"ok": True}

    monkeypatch.setattr("darsay.estimate.estimate", fake_estimate)
    assert estimate_repo("Qwen/Qwen3-0.6B", vault=tmp_path) == {"ok": True}
    assert seen["source"] == "Qwen/Qwen3-0.6B"
    estimate_repo("owner/name", repo_type="dataset", vault=tmp_path)
    assert seen["source"] == "datasets/owner/name"


def test_guess_version():
    assert _guess_version("Qwen3-0.6B", "qwen3") == "3"
    assert _guess_version("Qwen3-0.6B", None) == "3"
    assert _guess_version("Llama-2-7b", None) is None
    assert _guess_version("toy", None) is None


def _minimal_manifest(**overrides):
    data = {
        "schema_version": "1.6.0",
        "kind": MANIFEST_KIND,
        "artifact_type": "model",
        "bundle_id": "acme--toy@aaaaaaaaaaaa",
    }
    data.update(overrides)
    return data


def test_load_manifest_requires_schema_version(tmp_path):
    (tmp_path / "manifest.json").write_text('{"bundle_id": "x"}', encoding="utf-8")
    with pytest.raises(SystemExit, match="schema_version missing"):
        load_manifest(tmp_path)


def test_load_manifest_refuses_major_newer(tmp_path):
    write_manifest(tmp_path, _minimal_manifest(schema_version="2.0.0"))
    with pytest.raises(SystemExit, match="newer than this darsay"):
        load_manifest(tmp_path)


def test_load_manifest_refuses_wrong_kind(tmp_path):
    write_manifest(tmp_path, _minimal_manifest(kind="darsay.catalog"))
    with pytest.raises(SystemExit, match="kind is not"):
        load_manifest(tmp_path)


def test_warn_include_refuses_full_repo_resume_of_subset_pin():
    notes = []
    ledger = {"subset": {"include": ["*Q4_K_M*"]}}
    with pytest.raises(SystemExit, match="this pin is a subset"):
        _warn_include_vs_pin(None, ledger, notes.append)
    _warn_include_vs_pin(["*Q4_K_M*"], ledger, notes.append)
    assert notes == []
    _warn_include_vs_pin(["*.gguf", "*Q4_K_M*"], ledger, notes.append)
    assert any("differs from the pinned subset" in n for n in notes)


def test_load_manifest_missing_kind_is_implied_on_1x(tmp_path):
    data = _minimal_manifest()
    del data["kind"]
    (tmp_path / "manifest.json").write_text(json.dumps(data), encoding="utf-8")
    loaded = load_manifest(tmp_path)
    assert loaded["kind"] == MANIFEST_KIND


def test_write_manifest_preserves_unknown_top_level(tmp_path):
    write_manifest(tmp_path, _minimal_manifest(future_field=1, identity={"model_name": "toy"}))
    raw = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
    assert raw["future_field"] == 1
    keys = list(raw)
    known = [k for k in MANIFEST_TOP_KEYS if k in raw]
    assert keys[: len(known)] == known
    assert keys[-1] == "future_field"
    assert load_manifest(tmp_path)["future_field"] == 1

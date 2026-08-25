from __future__ import annotations

from modelvault.archiver import (
    _guess_version,
    archive_model,
    bundle_dir_for,
    bundle_name_for,
    hub_url,
    parse_repo_ref,
)
from modelvault.sources import parse_source


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

    monkeypatch.setattr("modelvault.archiver.archive", fake_archive)
    assert archive_model("Qwen/Qwen3-0.6B", vault=tmp_path) == tmp_path
    assert seen["source"] == "Qwen/Qwen3-0.6B"
    archive_model("owner/name", repo_type="dataset", vault=tmp_path, force=True)
    assert seen["source"] == "datasets/owner/name"
    assert seen["kwargs"]["force"] is True


def test_estimate_repo_wrapper_builds_source_ref(monkeypatch, tmp_path):
    from modelvault.estimate import estimate_repo

    seen = {}

    def fake_estimate(source, **kwargs):
        seen["source"] = source
        return {"ok": True}

    monkeypatch.setattr("modelvault.estimate.estimate", fake_estimate)
    assert estimate_repo("Qwen/Qwen3-0.6B", vault=tmp_path) == {"ok": True}
    assert seen["source"] == "Qwen/Qwen3-0.6B"
    estimate_repo("owner/name", repo_type="dataset", vault=tmp_path)
    assert seen["source"] == "datasets/owner/name"


def test_guess_version():
    assert _guess_version("Qwen3-0.6B", "qwen3") == "3"
    assert _guess_version("Qwen3-0.6B", None) == "3"
    assert _guess_version("Llama-2-7b", None) is None
    assert _guess_version("toy", None) is None

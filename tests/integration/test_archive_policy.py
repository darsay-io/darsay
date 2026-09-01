"""The masters-first archive default: skip prints, freeze the pin, degrade safely."""

from __future__ import annotations

import json

from darsay import SCHEMA_VERSION
from darsay.archiver import load_manifest
from darsay.cli import main
from darsay.providers.base import SourceError
from tests.integration.conftest import archive_quiet
from tests.payloads import make_gguf, model_files


def _repo_with_print(test_provider, locator="acme/toy"):
    files = model_files(extra={"Q4_K_M.gguf": make_gguf({"general.file_type": 15})})
    test_provider.add_repo(locator, files)
    return files


def test_archive_default_skips_confident_prints(vault, test_provider):
    _repo_with_print(test_provider)
    notes = []
    bundle = archive_quiet("test:acme/toy", vault=vault, progress=notes.append)
    manifest = load_manifest(bundle)
    assert manifest["schema_version"] == SCHEMA_VERSION == "1.8.0"
    subset = manifest["source"]["subset"]
    assert subset["policy"] == "masters"
    assert subset["include"] == ["model.safetensors"]
    classification = subset["classification"]
    assert classification["read"]["caps"]["header_file_cap"] == 64
    rules = {s["rule"]: s for s in classification["sets"]}
    assert rules["R9"]["action"] == "skip"
    assert rules["R4"]["verdict"] == "master"
    # The skipped print was never requested from the provider.
    assert "Q4_K_M.gguf" not in test_provider.downloads
    assert not (bundle / "model" / "Q4_K_M.gguf").exists()
    assert (bundle / "model" / "model.safetensors").is_file()
    assert (bundle / "model" / "config.json").is_file()
    assert "Q4_K_M.gguf" in {f["path"] for f in subset["full_files"]}
    assert any("masters-first: fetching" in str(line) for line in notes)
    assert any("--full fetches everything" in str(line) for line in notes)


def test_archive_resume_does_not_reclassify(vault, test_provider):
    _repo_with_print(test_provider)
    assert (
        main(
            [
                "--vault",
                str(vault),
                "archive",
                "test:acme/toy",
                "--max-bytes",
                "1",
                "--jobs",
                "1",
            ]
        )
        == 10
    )
    reads_after_pin = len(test_provider.reads)
    assert reads_after_pin > 0
    assert main(["--vault", str(vault), "archive", "test:acme/toy", "--jobs", "1"]) == 0
    # The pinned selection resumed; no header was re-read.
    assert len(test_provider.reads) == reads_after_pin
    bundle_dirs = list(vault.glob("*/*/manifest.json"))
    assert len(bundle_dirs) == 1
    manifest = json.loads(bundle_dirs[0].read_text())
    assert manifest["source"]["subset"]["policy"] == "masters"


def test_archive_force_reclassifies(vault, test_provider):
    _repo_with_print(test_provider)
    archive_quiet("test:acme/toy", vault=vault)
    reads = len(test_provider.reads)
    archive_quiet("test:acme/toy", vault=vault, force=True)
    assert len(test_provider.reads) > reads


def test_archive_include_bypasses_policy(vault, test_provider):
    _repo_with_print(test_provider)
    bundle = archive_quiet("test:acme/toy", vault=vault, include=["*Q4_K_M*"])
    subset = load_manifest(bundle)["source"]["subset"]
    assert subset["include"] == ["*Q4_K_M*"]
    assert "policy" not in subset
    assert test_provider.reads == []  # no classification ran


def test_archive_full_bypasses_policy(vault, test_provider):
    _repo_with_print(test_provider)
    bundle = archive_quiet("test:acme/toy", vault=vault, full=True)
    manifest = load_manifest(bundle)
    assert manifest["source"]["subset"] is None
    assert (bundle / "model" / "Q4_K_M.gguf").is_file()
    assert test_provider.reads == []


def test_archive_classification_failure_degrades_to_full(vault, test_provider):
    _repo_with_print(test_provider)
    # config.json unreadable -> the weight set cannot be established ->
    # the GGUF is ambiguous -> nothing skippable -> full fetch.
    test_provider.fail_next_read("config.json", SourceError("error: reset"))
    notes = []
    bundle = archive_quiet("test:acme/toy", vault=vault, progress=notes.append)
    manifest = load_manifest(bundle)
    assert manifest["source"]["subset"] is None
    assert (bundle / "model" / "Q4_K_M.gguf").is_file()
    assert any("darsay will not guess" in str(line) for line in notes)


def test_archive_full_on_a_policy_pin_refuses(vault, test_provider):
    _repo_with_print(test_provider)
    assert (
        main(
            [
                "--vault",
                str(vault),
                "archive",
                "test:acme/toy",
                "--max-bytes",
                "1",
                "--jobs",
                "1",
            ]
        )
        == 10
    )
    import pytest

    with pytest.raises(SystemExit, match="--force --full"):
        archive_quiet("test:acme/toy", vault=vault, full=True)


def test_archive_plain_repo_unchanged_by_policy(vault, test_provider):
    test_provider.add_repo("acme/plain", model_files())
    notes = []
    bundle = archive_quiet("test:acme/plain", vault=vault, progress=notes.append)
    manifest = load_manifest(bundle)
    assert manifest["source"]["subset"] is None
    assert not any("masters-first" in str(line) for line in notes)


def test_archive_default_skips_intra_repo_duplicates(vault, test_provider):
    from tests.payloads import make_safetensors

    blob = make_safetensors({"w": ("F32", [2, 4])})
    files = model_files()
    files["FL2VA/model.safetensors"] = files["model.safetensors"]
    files["FL2VA/config.json"] = files["config.json"]
    files["unique/model.safetensors"] = blob[:-1] + b"x"
    files["unique/config.json"] = files["config.json"]
    test_provider.add_repo("acme/tripled", files)
    bundle = archive_quiet("test:acme/tripled", vault=vault)
    manifest = load_manifest(bundle)
    subset = manifest["source"]["subset"]
    assert subset["policy"] == "masters"
    rules = {s["rule"]: s for s in subset["classification"]["sets"]}
    assert rules["R15"]["action"] == "skip"
    assert not (bundle / "model" / "FL2VA" / "model.safetensors").exists()
    assert (bundle / "model" / "model.safetensors").is_file()
    assert (bundle / "model" / "unique" / "model.safetensors").is_file()
    # The duplicate's sidecar config still rides along.
    assert (bundle / "model" / "FL2VA" / "config.json").is_file()

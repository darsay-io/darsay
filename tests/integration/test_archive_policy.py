"""The archive default: omit only local exact duplicates, freeze the pin."""

from __future__ import annotations

import json

from darsay import SCHEMA_VERSION
from darsay.archiver import load_manifest
from darsay.cli import main
from darsay.providers.base import SourceError
from tests.integration.conftest import archive_quiet
from tests.payloads import make_gguf, model_files


def _repo_with_print(test_provider, locator="acme/toy"):
    files = model_files()
    files["mirror/model.safetensors"] = files["model.safetensors"]
    test_provider.add_repo(locator, files)
    return files


def test_archive_default_skips_only_same_bundle_duplicates(vault, test_provider):
    _repo_with_print(test_provider)
    notes = []
    bundle = archive_quiet("test:acme/toy", vault=vault, progress=notes.append)
    manifest = load_manifest(bundle)
    assert manifest["schema_version"] == SCHEMA_VERSION == "2.3.0"
    subset = manifest["source"]["subset"]
    assert subset["policy"] == "negatives"
    assert subset["include"] == ["/model.safetensors"]
    classification = subset["classification"]
    assert classification["read"]["caps"]["header_file_cap"] == 64
    rules = {s["rule"]: s for s in classification["sets"]}
    assert rules["R15"]["action"] == "skip"
    assert rules["R4"]["verdict"] == "negative"
    # The skipped print was never requested from the provider.
    assert "mirror/model.safetensors" not in test_provider.downloads
    assert not (bundle / "model" / "mirror/model.safetensors").exists()
    assert (bundle / "model" / "model.safetensors").is_file()
    assert (bundle / "model" / "config.json").is_file()
    assert "mirror/model.safetensors" in {f["path"] for f in subset["full_files"]}
    assert any("archive: fetching" in str(line) for line in notes)
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
    assert manifest["source"]["subset"]["policy"] == "negatives"


def test_archive_force_reclassifies(vault, test_provider):
    _repo_with_print(test_provider)
    archive_quiet("test:acme/toy", vault=vault)
    reads = len(test_provider.reads)
    archive_quiet("test:acme/toy", vault=vault, force=True)
    assert len(test_provider.reads) > reads


def test_archive_include_bypasses_policy(vault, test_provider):
    test_provider.add_repo(
        "acme/toy",
        model_files(extra={"Q4_K_M.gguf": make_gguf({"general.file_type": 15})}),
    )
    bundle = archive_quiet("test:acme/toy", vault=vault, include=["*Q4_K_M*"])
    subset = load_manifest(bundle)["source"]["subset"]
    assert subset["include"] == ["*Q4_K_M*"]
    assert "policy" not in subset
    assert test_provider.reads == []  # no classification ran
    assert (bundle / "model" / "Q4_K_M.gguf").is_file()
    assert not (bundle / "model" / "model.safetensors").exists()


def test_archive_full_bypasses_policy(vault, test_provider):
    _repo_with_print(test_provider)
    bundle = archive_quiet("test:acme/toy", vault=vault, full=True)
    manifest = load_manifest(bundle)
    assert manifest["source"]["subset"] is None
    assert (bundle / "model" / "mirror/model.safetensors").is_file()
    assert test_provider.reads == []


def test_archive_classification_failure_degrades_to_full(vault, test_provider):
    test_provider.add_repo(
        "acme/toy",
        model_files(extra={"Q4_K_M.gguf": make_gguf({"general.file_type": 15})}),
    )
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
    assert not any("negatives" in str(line) for line in notes)


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
    assert subset["policy"] == "negatives"
    rules = {s["rule"]: s for s in subset["classification"]["sets"]}
    assert rules["R15"]["action"] == "skip"
    assert not (bundle / "model" / "FL2VA" / "model.safetensors").exists()
    assert (bundle / "model" / "model.safetensors").is_file()
    assert (bundle / "model" / "unique" / "model.safetensors").is_file()
    # The duplicate's sidecar config still rides along.
    assert (bundle / "model" / "FL2VA" / "config.json").is_file()


def test_archive_retains_colocated_gguf_without_regeneration_proof(
    vault, test_provider
):
    files = model_files(extra={"Q4_K_M.gguf": make_gguf({"general.file_type": 15})})
    test_provider.add_repo("acme/gguf", files)
    bundle = archive_quiet("test:acme/gguf", vault=vault)
    manifest = load_manifest(bundle)
    assert manifest["source"]["subset"] is None
    assert (bundle / "model" / "Q4_K_M.gguf").read_bytes() == files["Q4_K_M.gguf"]
    assert manifest["inventory"]["total_size_bytes"] == sum(map(len, files.values()))


def test_archive_retains_base_match_when_archived_base_revision_is_older(
    vault, test_provider
):
    old_base = model_files(param_shape=[2, 4])
    test_provider.add_repo("acme/base", old_base, revision="a" * 40)
    base_bundle = archive_quiet("test:acme/base", vault=vault, full=True)
    new_base = model_files(param_shape=[3, 4])
    test_provider.add_repo("acme/base", new_base, revision="b" * 40)
    child_files = model_files(
        param_shape=[4, 4],
        extra={"copy/model.safetensors": new_base["model.safetensors"]},
    )
    test_provider.add_repo(
        "acme/child",
        child_files,
        revision="c" * 40,
        metadata={"tags": ["base_model:acme/base"], "card_data": {}, "gated": False},
    )
    bundle = archive_quiet("test:acme/child", vault=vault)
    assert load_manifest(base_bundle)["source"]["revision"] == "a" * 40
    assert (bundle / "model" / "copy/model.safetensors").read_bytes() == new_base[
        "model.safetensors"
    ]
    assert load_manifest(bundle)["source"]["subset"] is None


def test_policy_preserves_arbitrary_support_and_literal_paths(vault, test_provider):
    from darsay.estimate import estimate

    files = model_files()
    files["model[raw]*?.safetensors"] = files.pop("model.safetensors")
    files["copy/model[raw]*?.safetensors"] = files["model[raw]*?.safetensors"]
    files["calibration.dat"] = b"preserve calibration inputs"
    files["recipes/calibration[1]*?.dat"] = b"preserve every support path"
    test_provider.add_repo("acme/support", files)
    priced = estimate("test:acme/support", vault=vault, progress=lambda _: None)
    bundle = archive_quiet("test:acme/support", vault=vault)
    retained = set(files) - {"copy/model[raw]*?.safetensors"}
    payload = bundle / "model"
    assert {
        p.relative_to(payload).as_posix() for p in payload.rglob("*") if p.is_file()
    } == retained
    for path in retained:
        assert (payload / path).read_bytes() == files[path]
    manifest = load_manifest(bundle)
    assert (
        priced["payload"]["total_size_bytes"]
        == manifest["inventory"]["total_size_bytes"]
        == sum(len(files[p]) for p in retained)
    )
    assert {f["path"] for f in manifest["source"]["subset"]["full_files"]} == set(files)

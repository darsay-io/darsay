"""darsay classify end to end against the fake provider."""

from __future__ import annotations

import json

import pytest

from darsay.cli import main
from darsay.providers.base import SourceError
from tests.integration.conftest import archive_quiet
from tests.payloads import dataset_files, make_gguf, make_safetensors, model_files


def _index(mapping: dict[str, str]) -> bytes:
    return json.dumps({"metadata": {}, "weight_map": mapping}).encode("utf-8")


def test_classify_single_candidate_plus_gguf_retains_unknown(
    vault, test_provider, capsys
):
    files = model_files(extra={"Q4_K_M.gguf": make_gguf({"general.file_type": 15})})
    test_provider.add_repo("acme/toy", files)
    assert main(["--vault", str(vault), "classify", "test:acme/toy"]) == 0
    out = capsys.readouterr().out
    assert "test:acme/toy @ main" in out
    assert "SKIPPED" not in out and "[R9]" in out
    assert "regeneration are not established" in out
    assert "The archive retains the whole repository" in out
    assert "print = hash-identical published bytes" in out

    assert main(["--vault", str(vault), "classify", "test:acme/toy", "--json"]) == 0
    out = capsys.readouterr().out
    data = json.loads(out[out.index("{") :])
    assert data["selection"] is None
    assert data["source"]["address"] == "test:acme/toy"
    assert data["read"]["caps"]["header_file_cap"] == 64
    assert data["unclassified_count"] == 1
    assert data["skip"]["files"] == 0


def test_classify_case_study_shape_refuses_to_guess(vault, test_provider, capsys):
    files = model_files(
        extra={
            "model-00001-of-00002.safetensors": make_safetensors(
                {"a": ("F32", [2, 2])}
            ),
            "model-00002-of-00002.safetensors": make_safetensors(
                {"b": ("F32", [2, 2])}
            ),
            "stray-00001-of-00002.safetensors": make_safetensors(
                {"a": ("F32", [3, 2])}
            ),
            "stray-00002-of-00002.safetensors": make_safetensors(
                {"b": ("F32", [3, 2])}
            ),
            "model.safetensors.index.json": _index(
                {
                    "a": "model-00001-of-00002.safetensors",
                    "b": "model-00002-of-00002.safetensors",
                }
            ),
            "Q4_K_M.gguf": make_gguf({"general.name": "s99-merged-fixed"}),
        }
    )
    del files["model.safetensors"]
    test_provider.add_repo("acme/oblit", files)
    assert main(["--vault", str(vault), "classify", "test:acme/oblit", "--json"]) == 0
    out = capsys.readouterr().out
    data = json.loads(out[out.index("{") :])
    by_rule = {s["rule"]: s for s in data["sets"]}
    assert by_rule["R3"]["verdict"] == "negative"
    assert by_rule["R6"]["verdict"] == "unknown"
    assert by_rule["R11"]["verdict"] == "unknown"
    assert data["skip"]["bytes"] == 0
    assert data["selection"] is None

    assert main(["--vault", str(vault), "classify", "test:acme/oblit"]) == 0
    human = capsys.readouterr().out
    assert "The archive retains the whole repository" in human
    assert "need your decision" in human


def test_classify_r2_retained_even_when_base_bundle_is_present(
    vault, test_provider, capsys
):
    shared = make_safetensors({"w": ("F32", [2, 2])})
    test_provider.add_repo("acme/base", model_files())
    base_blob = test_provider.repos[("acme/base", "main")].files["model.safetensors"]
    files = model_files(
        param_shape=[3, 3], extra={"copyof/base_model.safetensors": base_blob or shared}
    )
    test_provider.add_repo(
        "acme/copy",
        files,
        metadata={
            "card_data": {},
            "tags": ["base_model:acme/base"],
            "gated": False,
        },
    )
    # An upstream identity claim does not replace retained local bytes.
    assert main(["--vault", str(vault), "classify", "test:acme/copy", "--json"]) == 0
    out = capsys.readouterr().out
    data = json.loads(out[out.index("{") :])
    copy_set = next(
        s for s in data["sets"] if "copyof/base_model.safetensors" in s["paths"]
    )
    assert (copy_set["rule"], copy_set["action"]) == ("R2", "fetch")
    assert "base_in_vault" not in data["source"]

    archive_quiet("test:acme/base", vault=vault)
    assert main(["--vault", str(vault), "classify", "test:acme/copy", "--json"]) == 0
    out = capsys.readouterr().out
    data = json.loads(out[out.index("{") :])
    copy_set = next(
        s for s in data["sets"] if "copyof/base_model.safetensors" in s["paths"]
    )
    assert (copy_set["rule"], copy_set["action"]) == ("R2", "fetch")
    assert "base_in_vault" not in data["source"]


def test_classify_dataset_refused(vault, test_provider):
    test_provider.add_repo(
        "acme/reviews",
        dataset_files(),
        artifact_type="dataset",
        pipeline_tag=None,
        metadata={"card_data": {}, "tags": [], "gated": False},
    )
    with pytest.raises(SystemExit, match="applies to models"):
        main(["--vault", str(vault), "classify", "test:datasets/acme/reviews"])


def test_classify_read_failures_degrade_to_fetch(vault, test_provider, capsys):
    files = model_files(extra={"Q4_K_M.gguf": make_gguf({"general.file_type": 15})})
    test_provider.add_repo("acme/toy", files)
    test_provider.fail_next_read("Q4_K_M.gguf", SourceError("error: reset"))
    assert main(["--vault", str(vault), "classify", "test:acme/toy", "--json"]) == 0
    out = capsys.readouterr().out
    data = json.loads(out[out.index("{") :])
    gguf_set = next(s for s in data["sets"] if "Q4_K_M.gguf" in s["paths"])
    assert (gguf_set["verdict"], gguf_set["action"]) == ("unknown", "fetch")
    assert data["selection"] is None


def test_classify_provider_without_read_capability(
    vault, test_provider, capsys, monkeypatch
):
    test_provider.add_repo(
        "acme/toy",
        model_files(extra={"Q4_K_M.gguf": make_gguf({"general.file_type": 15})}),
    )

    def no_reads(*args, **kwargs):
        raise SourceError("error: Test Source does not support remote byte-range reads")

    monkeypatch.setattr(test_provider, "read_bytes", no_reads)
    assert main(["--vault", str(vault), "classify", "test:acme/toy"]) == 0
    out = capsys.readouterr().out
    assert "The archive retains the whole repository" in out
    assert "need your decision" in out

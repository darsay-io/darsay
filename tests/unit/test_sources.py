from __future__ import annotations

import pytest

from darsay.sources import get_provider, parse_source, provider_names, source_from_ledger


def test_huggingface_is_registered():
    assert "huggingface" in provider_names()
    provider = get_provider("huggingface")
    assert provider.name == "huggingface"
    assert get_provider("hf") is provider


def test_unknown_provider_errors():
    with pytest.raises(SystemExit, match="unknown source provider"):
        get_provider("modelscope")


def test_parse_empty_ref():
    with pytest.raises(SystemExit, match="empty source ref"):
        parse_source("")
    with pytest.raises(SystemExit, match="empty source ref"):
        parse_source("   ")


def test_parse_unprefixed_model():
    ref = parse_source("Qwen/Qwen3-0.6B")
    assert ref.provider == "huggingface"
    assert ref.artifact_type == "model"
    assert ref.locator == "Qwen/Qwen3-0.6B"
    assert ref.canonical == "huggingface:Qwen/Qwen3-0.6B"
    assert ref.bundle_name == "qwen--qwen3-0.6b"
    assert ref.publisher == "Qwen"
    assert ref.name == "Qwen3-0.6B"
    assert ref.url == "https://huggingface.co/Qwen/Qwen3-0.6B"


def test_parse_qualified_and_alias():
    a = parse_source("huggingface:Qwen/Qwen3-0.6B")
    b = parse_source("hf:Qwen/Qwen3-0.6B")
    assert a.canonical == b.canonical == "huggingface:Qwen/Qwen3-0.6B"


def test_parse_dataset_shorthand_and_qualified():
    shorthand = parse_source("datasets/owner/name")
    qualified = parse_source("huggingface:datasets/owner/name")
    assert shorthand.artifact_type == qualified.artifact_type == "dataset"
    assert shorthand.locator == qualified.locator == "owner/name"
    assert shorthand.canonical == "huggingface:datasets/owner/name"
    assert shorthand.bundle_name == "datasets--owner--name"
    assert shorthand.url == "https://huggingface.co/datasets/owner/name"


def test_parse_hub_urls():
    urls = [
        "https://huggingface.co/Qwen/Qwen3-0.6B",
        "https://huggingface.co/Qwen/Qwen3-0.6B/tree/main",
        "https://hf.co/Qwen/Qwen3-0.6B",
        "https://www.huggingface.co/Qwen/Qwen3-0.6B",
    ]
    canonicals = [parse_source(u).canonical for u in urls]
    assert set(canonicals) == {"huggingface:Qwen/Qwen3-0.6B"}


def test_parse_unknown_scheme_is_not_huggingface_shorthand():
    with pytest.raises(SystemExit, match="unknown source provider"):
        parse_source("modelscope:qwen/foo")


def test_parse_unknown_host():
    with pytest.raises(SystemExit, match="no source provider for host"):
        parse_source("https://example.com/owner/name")


def test_source_from_ledger_canonical_address():
    ref = source_from_ledger({"address": "huggingface:Qwen/Qwen3-0.6B"})
    assert ref.canonical == "huggingface:Qwen/Qwen3-0.6B"


def test_source_from_ledger_pre_provider_model():
    ref = source_from_ledger({"repo_id": "Qwen/Qwen3-0.6B"})
    assert ref.provider == "huggingface"
    assert ref.artifact_type == "model"
    assert ref.locator == "Qwen/Qwen3-0.6B"


def test_source_from_ledger_pre_provider_dataset():
    ref = source_from_ledger({"repo_id": "owner/name", "repo_type": "dataset"})
    assert ref.artifact_type == "dataset"
    assert ref.locator == "owner/name"
    assert ref.canonical == "huggingface:datasets/owner/name"

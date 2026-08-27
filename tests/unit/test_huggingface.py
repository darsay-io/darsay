from __future__ import annotations

from datetime import datetime, timezone

from darsay.providers.huggingface import (
    HuggingFaceProvider,
    _json_value,
    _license_from_tags,
    _variant_formats,
    parse_base_model_tags,
)


def test_parse_rejects_malformed_locator():
    provider = HuggingFaceProvider()
    try:
        provider.parse("just-a-name")
        raise AssertionError("expected SystemExit")
    except SystemExit as exc:
        assert "cannot parse source ref" in str(exc)


def test_parse_strips_query_and_fragment():
    provider = HuggingFaceProvider()
    ref = provider.parse("Qwen/Qwen3-0.6B?foo=1#bar")
    assert ref.locator == "Qwen/Qwen3-0.6B"


def test_parse_base_model_tags_relations_and_order():
    tags = [
        "text-generation",
        "base_model:meta-llama/Llama-2-7b-hf",
        "base_model:finetune:owner/adapter-base",
        "base_model:quantized:TheBloke/Llama-2-7B-GGUF",
        "license:llama2",
    ]
    ids, relations = parse_base_model_tags(tags)
    assert ids == [
        "meta-llama/Llama-2-7b-hf",
        "owner/adapter-base",
        "TheBloke/Llama-2-7B-GGUF",
    ]
    assert relations["owner/adapter-base"] == "finetune"
    assert relations["TheBloke/Llama-2-7B-GGUF"] == "quantized"
    assert "meta-llama/Llama-2-7b-hf" not in relations


def test_license_from_tags():
    assert _license_from_tags(["text-generation", "license:apache-2.0"]) == "apache-2.0"
    assert _license_from_tags(["text-generation"]) is None
    assert _license_from_tags(None) is None


def test_variant_formats_from_tags_and_name():
    assert "gguf" in (_variant_formats("owner/toy-gguf", ["gguf"]) or [])
    assert "awq" in (_variant_formats("owner/toy-AWQ", []) or [])
    assert _variant_formats("owner/vanilla", ["text-generation"]) is None


def test_progress_wrapper_hides_tqdm_and_counts_network_bytes():
    from darsay.progress import TransferMeter
    from darsay.transfer import NetworkCounter

    session = {"bytes_network": 0, "bytes_local_sources": 0, "files_completed": 0}
    counter = NetworkCounter(session)
    meter = TransferMeter(
        total_bytes=80,
        total_files=1,
        verified_bytes=0,
        verified_files=0,
        partial_bytes=0,
        session=session,
    )
    tqdm_class = HuggingFaceProvider().progress_wrapper(counter, meter=meter)
    bar = tqdm_class(total=80, desc="weights.bin", disable=False)
    assert bar.disable is True
    bar.update(20)
    bar.update_transfer(20)
    assert session["bytes_network"] == 20
    meter.set_current("weights.bin", 80)
    meter.attach_bar(bar, "weights.bin")
    assert meter.snapshot()["done_bytes"] == 20
    bar.close()


def test_progress_wrapper_without_meter_still_counts():
    from darsay.transfer import NetworkCounter

    session = {"bytes_network": 0}
    counter = NetworkCounter(session)
    tqdm_class = HuggingFaceProvider().progress_wrapper(counter)
    bar = tqdm_class(total=10, desc="a.bin", disable=True)
    bar.update_transfer(4)
    assert session["bytes_network"] == 4
    bar.close()


def test_json_value_datetime_and_nested():
    ts = datetime(2026, 1, 1, tzinfo=timezone.utc)
    converted = _json_value(
        {"at": ts, "n": 1, "ok": True, "xs": (1, 2), "other": object()}
    )
    assert converted["at"].startswith("2026-01-01")
    assert converted["n"] == 1
    assert converted["xs"] == [1, 2]
    assert isinstance(converted["other"], str)

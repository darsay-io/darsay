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
    bar = tqdm_class(total=80, desc="weights.bin", disable=False, initial=8)
    assert bar.disable is True
    bar.update(20)
    bar.update_transfer(20)
    assert session["bytes_network"] == 20
    # A disabled tqdm would leave n at its initial value; the panel's file
    # line needs the real count, resume offset included.
    assert bar.n == 28
    meter.set_current("weights.bin", 80)
    meter.attach_bar(bar, "weights.bin")
    snap = meter.snapshot()
    assert snap["done_bytes"] == 20
    assert snap["current"][0]["n"] == 28
    bar.close()
    # The count survives the bar closing (a dropped connection closes it).
    assert meter.snapshot()["current"][0]["n"] == 28


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


def test_transient_network_error_classifies_transport_failures():
    import socket

    import httpx
    from huggingface_hub.utils import HfHubHTTPError

    from darsay.providers.huggingface import HuggingFaceProvider

    provider = HuggingFaceProvider()
    # The shape a laptop leaving Wi-Fi produces: httpx wraps httpcore wraps
    # the resolver's gaierror.
    try:
        try:
            raise socket.gaierror(8, "nodename nor servname provided, or not known")
        except socket.gaierror as inner:
            raise httpx.ConnectError("[Errno 8] nodename nor servname") from inner
    except httpx.ConnectError as exc:
        dns = exc
    assert provider.transient_network_error(dns) == "DNS lookup failed"
    # A metadata call that failed offline surfaces as a Hub "not found" that
    # was raised *from* the transport error; the chain still tells the truth.
    try:
        raise ValueError("cannot locate the file on the Hub") from dns
    except ValueError as exc:
        wrapped = exc
    assert provider.transient_network_error(wrapped) == "DNS lookup failed"
    assert provider.transient_network_error(httpx.ReadTimeout("slow")) == "timed out"
    assert (
        provider.transient_network_error(httpx.ConnectTimeout("slow"))
        == "connection timed out"
    )
    assert (
        provider.transient_network_error(httpx.RemoteProtocolError("closed"))
        == "connection closed by the server"
    )
    assert provider.transient_network_error(ConnectionResetError()) == (
        "connection reset"
    )
    request = httpx.Request("GET", "https://huggingface.co/x")
    gateway = HfHubHTTPError(
        "bad gateway", response=httpx.Response(502, request=request)
    )
    assert provider.transient_network_error(gateway) == "Hugging Face responded 502"
    forbidden = HfHubHTTPError("nope", response=httpx.Response(403, request=request))
    assert provider.transient_network_error(forbidden) is None
    assert provider.transient_network_error(ValueError("too large")) is None
    assert provider.transient_network_error(FileNotFoundError("gone")) is None
    assert (
        provider.transient_network_error(
            OSError(
                "Consistency check failed: file should be of size 10 but has size 4"
            )
        )
        == "transfer cut short"
    )


def test_throttled_chunk_size_targets_a_quarter_second():
    from darsay.providers.huggingface import throttled_chunk_size

    default = 10 * 1024**2
    assert throttled_chunk_size(4 * 1024**2, default) == 1024**2
    assert throttled_chunk_size(100 * 1024, default) == 64 * 1024  # floor
    assert (
        throttled_chunk_size(10**9, default) == default
    )  # never above the client's own


def test_transfer_session_shrinks_chunks_and_filters_retry_chatter(tmp_path):
    import logging

    import huggingface_hub.constants as hub_constants

    from darsay.providers.huggingface import HuggingFaceProvider

    original = hub_constants.DOWNLOAD_CHUNK_SIZE
    hub_logger = logging.getLogger("huggingface_hub")
    handler = logging.Handler()
    hub_logger.addHandler(handler)
    try:
        with HuggingFaceProvider().transfer_session(tmp_path, max_rate=1024**2):
            assert hub_constants.DOWNLOAD_CHUNK_SIZE == 256 * 1024
            record = logging.LogRecord(
                "huggingface_hub.file_download",
                logging.WARNING,
                __file__,
                1,
                "Error while downloading from %s: %s\nTrying to resume download...",
                ("https://x", "boom"),
                None,
            )
            assert not handler.filter(record)
            other = logging.LogRecord(
                "huggingface_hub.utils._http",
                logging.WARNING,
                __file__,
                1,
                "Rate limited. Waiting 3s before retry",
                (),
                None,
            )
            assert handler.filter(other)
        assert original == hub_constants.DOWNLOAD_CHUNK_SIZE
        assert handler.filter(record)  # filter removed
        with HuggingFaceProvider().transfer_session(tmp_path):
            assert original == hub_constants.DOWNLOAD_CHUNK_SIZE
    finally:
        hub_logger.removeHandler(handler)

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from darsay.providers.base import SourceNotFoundError
from darsay.providers.huggingface import (
    HuggingFaceProvider,
    _json_value,
    _license_from_tags,
    _variant_formats,
    parse_base_model_tags,
)


def _hub_not_found():
    import httpx
    from huggingface_hub.errors import RepositoryNotFoundError

    request = httpx.Request("GET", "https://huggingface.co/api/models/x")
    return RepositoryNotFoundError(
        "not found", response=httpx.Response(401, request=request)
    )


def _hub_info(*, sha="b" * 40, filename="train.jsonl"):
    sibling = SimpleNamespace(rfilename=filename, size=12, lfs=None, blob_id="abc123")
    return SimpleNamespace(
        sha=sha,
        siblings=[sibling],
        card_data=SimpleNamespace(to_dict=lambda: {"license": "mit"}),
        last_modified=datetime(2026, 1, 1, tzinfo=timezone.utc),
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        tags=["license:mit"],
        gated=False,
        downloads=1,
        likes=0,
        pipeline_tag=None,
        safetensors=None,
    )


def test_pin_retargets_model_shaped_dataset_only_id(monkeypatch):
    provider = HuggingFaceProvider()
    calls: list[tuple[str, str]] = []

    class FakeApi:
        def model_info(self, repo_id, revision=None, files_metadata=False):
            calls.append(("model", repo_id))
            raise _hub_not_found()

        def dataset_info(self, repo_id, revision=None, files_metadata=False):
            calls.append(("dataset", repo_id))
            return _hub_info()

    monkeypatch.setattr("huggingface_hub.HfApi", FakeApi)
    snap = provider.pin(provider.parse("saidutta69/fable-5-premium"), "main")
    assert snap.source.artifact_type == "dataset"
    assert snap.source.canonical == "huggingface:datasets/saidutta69/fable-5-premium"
    assert snap.source.bundle_name == "datasets--saidutta69--fable-5-premium"
    assert calls == [
        ("model", "saidutta69/fable-5-premium"),
        ("dataset", "saidutta69/fable-5-premium"),
    ]


def test_pin_does_not_probe_dataset_when_model_exists(monkeypatch):
    provider = HuggingFaceProvider()

    class FakeApi:
        def model_info(self, repo_id, revision=None, files_metadata=False):
            return _hub_info(filename="model.safetensors")

        def dataset_info(self, repo_id, revision=None, files_metadata=False):
            raise AssertionError("dataset_info should not run")

    monkeypatch.setattr("huggingface_hub.HfApi", FakeApi)
    snap = provider.pin(provider.parse("Qwen/Qwen3-0.6B"), "main")
    assert snap.source.artifact_type == "model"
    assert snap.source.canonical == "huggingface:Qwen/Qwen3-0.6B"


def test_pin_explicit_dataset_does_not_probe_model(monkeypatch):
    provider = HuggingFaceProvider()

    class FakeApi:
        def model_info(self, repo_id, revision=None, files_metadata=False):
            raise AssertionError("model_info should not run")

        def dataset_info(self, repo_id, revision=None, files_metadata=False):
            raise _hub_not_found()

    monkeypatch.setattr("huggingface_hub.HfApi", FakeApi)
    with pytest.raises(SourceNotFoundError, match="dataset"):
        provider.pin(provider.parse("datasets/missing/repo"), "main")


def test_pin_missing_both_keeps_the_model_error(monkeypatch):
    provider = HuggingFaceProvider()

    class FakeApi:
        def model_info(self, repo_id, revision=None, files_metadata=False):
            raise _hub_not_found()

        def dataset_info(self, repo_id, revision=None, files_metadata=False):
            raise _hub_not_found()

    monkeypatch.setattr("huggingface_hub.HfApi", FakeApi)
    with pytest.raises(SourceNotFoundError, match="model 'missing/repo'"):
        provider.pin(provider.parse("missing/repo"), "main")


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


def _hub_record(message: str, *args):
    import logging

    return logging.LogRecord(
        "huggingface_hub.file_download",
        logging.WARNING,
        __file__,
        1,
        message,
        args,
        None,
    )


def test_retry_chatter_becomes_a_panel_state(tmp_path):
    import logging

    from darsay.providers.huggingface import HuggingFaceProvider, _RetryChatterFilter

    seen: list = []
    chatter = _RetryChatterFilter(on_retry=seen.append)
    resume = _hub_record(
        "Error while downloading from %s: %s\nTrying to resume download...",
        "https://cas-bridge.xethub.hf.co/x?X-Xet-Signed-Range=bytes%3D0-1",
        "timed out",
    )
    assert chatter.filter(resume) is False
    gave_up = _hub_record(
        "Error while downloading from %s: %s\nMax retries exceeded.",
        "https://x",
        "boom",
    )
    assert chatter.filter(gave_up) is False
    assert chatter.filter(_hub_record("Rate limited. Waiting 3s before retry")) is True
    assert seen == ["timed out", "boom"]

    def broken(_reason):
        raise RuntimeError("panel gone")

    # A failing callback never breaks the transport's logging.
    assert _RetryChatterFilter(on_retry=broken).filter(resume) is False
    assert _RetryChatterFilter().filter(resume) is False

    # And the session plumbs it onto the Hub client's handlers.
    hub_logger = logging.getLogger("huggingface_hub")
    handler = logging.Handler()
    hub_logger.addHandler(handler)
    seen.clear()
    try:
        with HuggingFaceProvider().transfer_session(tmp_path, on_retry=seen.append):
            assert not handler.filter(resume)
        assert seen == ["timed out"]
    finally:
        hub_logger.removeHandler(handler)


def test_resumable_download_skips_the_hub_disk_warning(tmp_path, monkeypatch):
    """darsay checks headroom against its floor before a file begins; the
    client's per-file UserWarning would only repeat that, badly."""
    import huggingface_hub.file_download as file_download

    from darsay.providers.huggingface import HuggingFaceProvider

    def forbidden(*args, **kwargs):
        raise AssertionError("the Hub client's disk check ran")

    monkeypatch.setattr(file_download, "_check_disk_space", forbidden)

    def fake_http_get(url, temp_file, *, resume_size=0, expected_size=None, **kwargs):
        temp_file.write(b"x" * (expected_size - resume_size))

    monkeypatch.setattr(file_download, "http_get", fake_http_get)
    incomplete = tmp_path / "w.bin.incomplete"
    incomplete.write_bytes(b"xx")
    destination = tmp_path / "w.bin"
    with HuggingFaceProvider().transfer_session(tmp_path):
        file_download._download_to_tmp_and_move(
            incomplete,
            destination,
            "https://x/w.bin",
            {},
            5,
            "w.bin",
            False,
            "etag",
            None,
            tqdm_class=None,
        )
    assert destination.read_bytes() == b"xxxxx"
    assert not incomplete.exists()

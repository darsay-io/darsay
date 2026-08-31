"""The provider byte-range primitive: base default, fake, and Hub transport."""

from __future__ import annotations

import pytest

from darsay.providers.base import (
    SourceError,
    SourceGatedError,
    SourceNotFoundError,
    SourceProvider,
)
from darsay.providers.huggingface import HuggingFaceProvider
from tests.fakes import TestProvider as FakeProvider


class _BareProvider(SourceProvider):
    name = "bare"
    label = "Bare Source"

    def parse(self, locator, *, from_url=False):  # pragma: no cover - unused
        raise NotImplementedError

    def pin(self, source, revision, *, require_access=False):  # pragma: no cover
        raise NotImplementedError

    def download_file(self, *args, **kwargs):  # pragma: no cover - unused
        raise NotImplementedError


def _test_ref(provider):
    return provider.parse("acme/toy")


def test_base_default_raises_source_error():
    provider = _BareProvider()
    ref = FakeProvider().parse("acme/toy")
    with pytest.raises(SourceError, match="byte-range reads"):
        provider.read_bytes(ref, "main", "config.json", 0, 4)


def test_fake_provider_slices_and_records():
    provider = FakeProvider()
    provider.add_repo("acme/toy", {"weights.bin": b"0123456789"})
    ref = _test_ref(provider)
    assert provider.read_bytes(ref, "main", "weights.bin", 2, 4) == b"2345"
    # Short only at EOF; empty past it; zero length reads nothing.
    assert provider.read_bytes(ref, "main", "weights.bin", 8, 10) == b"89"
    assert provider.read_bytes(ref, "main", "weights.bin", 20, 4) == b""
    assert provider.read_bytes(ref, "main", "weights.bin", 0, 0) == b""
    assert provider.reads == [
        ("weights.bin", 2, 4),
        ("weights.bin", 8, 10),
        ("weights.bin", 20, 4),
    ]


def test_fake_provider_missing_file_and_faults():
    provider = FakeProvider()
    provider.add_repo("acme/toy", {"weights.bin": b"abc"})
    ref = _test_ref(provider)
    with pytest.raises(SourceNotFoundError, match="nope.bin"):
        provider.read_bytes(ref, "main", "nope.bin", 0, 1)
    provider.fail_next_read("weights.bin", SourceError("error: boom"))
    with pytest.raises(SourceError, match="boom"):
        provider.read_bytes(ref, "main", "weights.bin", 0, 1)
    assert provider.read_bytes(ref, "main", "weights.bin", 0, 1) == b"a"


def test_fake_provider_access_denied_read():
    provider = FakeProvider()
    provider.add_repo("acme/locked", {"weights.bin": b"abc"}, access_denied=True)
    ref = provider.parse("acme/locked")
    with pytest.raises(SourceGatedError):
        provider.read_bytes(ref, "main", "weights.bin", 0, 1)


class _HTTPError(Exception):
    def __init__(self, response):
        super().__init__(f"HTTP {response.status_code}")
        self.response = response


class _FakeResponse:
    def __init__(self, status_code, body=b""):
        self.status_code = status_code
        self._body = body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise _HTTPError(self)

    def iter_bytes(self):
        yield self._body[:3]
        yield self._body[3:]


class _FakeSession:
    def __init__(self, response):
        self._response = response
        self.calls: list[tuple[str, str, dict]] = []

    def stream(self, method, url, headers=None, follow_redirects=False):
        self.calls.append((method, url, dict(headers or {})))
        response = self._response

        class _Ctx:
            def __enter__(self):
                return response

            def __exit__(self, *exc):
                return False

        return _Ctx()


def _hub_read(monkeypatch, response, *, start=0, length=8, relative="model.gguf"):
    provider = HuggingFaceProvider()
    session = _FakeSession(response)
    monkeypatch.setattr("huggingface_hub.utils.get_session", lambda: session)
    ref = provider.parse("acme/toy")
    result = provider.read_bytes(ref, "a" * 40, relative, start, length)
    return result, session


def test_hub_read_bytes_range_request_and_cap(monkeypatch):
    result, session = _hub_read(
        monkeypatch,
        _FakeResponse(206, b"0123456789abcdef"),
        start=5,
        length=8,
    )
    # Body capped to the requested length even when the server sends more.
    assert result == b"01234567"
    (method, url, headers) = session.calls[0]
    assert method == "GET"
    assert "acme/toy/resolve" in url and url.endswith("model.gguf")
    assert headers["Range"] == "bytes=5-12"


def test_hub_read_bytes_refuses_ignored_range(monkeypatch):
    with pytest.raises(SourceError, match="ignored a Range"):
        _hub_read(monkeypatch, _FakeResponse(200, b"whole file"), start=5)


def test_hub_read_bytes_full_body_from_start_is_fine(monkeypatch):
    result, _ = _hub_read(
        monkeypatch, _FakeResponse(200, b"whole file"), start=0, length=5
    )
    assert result == b"whole"


def test_hub_read_bytes_past_eof_is_empty(monkeypatch):
    result, _ = _hub_read(monkeypatch, _FakeResponse(416), start=999)
    assert result == b""


def test_hub_read_bytes_maps_auth_and_transport_errors(monkeypatch):
    with pytest.raises(SourceGatedError, match="HTTP 403"):
        _hub_read(monkeypatch, _FakeResponse(403))
    with pytest.raises(SourceError, match="cannot read bytes"):
        _hub_read(monkeypatch, _FakeResponse(500))

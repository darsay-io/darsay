"""The GitHub provider against a fake HTTP opener: no network, every wire
shape darsay depends on (REST pin, raw blobs, LFS media, Range resume,
the error statuses) played back from memory."""

from __future__ import annotations

import io
import json
import socket
from http.client import IncompleteRead, RemoteDisconnected
from urllib.error import HTTPError, URLError
from urllib.parse import unquote

import pytest

from darsay.providers.base import (
    SourceError,
    SourceGatedError,
    SourceNotFoundError,
)
from darsay.providers.github import (
    API_ROOT,
    MEDIA_ROOT,
    RAW_ROOT,
    GitHubProvider,
    lfs_patterns,
    matches_lfs_pattern,
    parse_lfs_pointer,
)
from darsay.sources import get_provider, parse_source, provider_names

SHA = "203834ca88000c8192112e396b80d886b522caa0"
POINTER = (
    b"version https://git-lfs.github.com/spec/v1\n"
    b"oid sha256:" + b"ab" * 32 + b"\n"
    b"size 5\n"
)


class _Headers(dict):
    def get(self, key, default=None):
        for name, value in self.items():
            if name.lower() == key.lower():
                return value
        return default


class FakeResponse:
    def __init__(self, body: bytes, status: int = 200, headers: dict | None = None):
        self._body = body
        self._pos = 0
        self.status = status
        self.headers = _Headers(headers or {"Content-Length": str(len(body))})

    def read(self, n: int = -1) -> bytes:
        if n is None or n < 0:
            chunk = self._body[self._pos :]
            self._pos = len(self._body)
            return chunk
        chunk = self._body[self._pos : self._pos + n]
        self._pos += len(chunk)
        return chunk

    def getcode(self) -> int:
        return self.status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _http_error(url: str, code: int, headers: dict | None = None, body: bytes = b""):
    return HTTPError(
        url, code, f"HTTP {code}", _Headers(headers or {}), io.BytesIO(body)
    )


class FakeGitHub:
    """An opener: ``(request, timeout=...) -> response`` playing one repository."""

    def __init__(self, locator: str = "MiaAI-Lab/Recipe"):
        self.locator = locator
        self.sha = SHA
        self.refs = {"HEAD": SHA, "main": SHA, "v1": "1" * 40}
        self.repo = {
            "full_name": locator,
            "description": "Serve acme/toy on one box",
            "homepage": "https://example.invalid/mia",
            "topics": ["vllm", "recipe"],
            "license": {"key": "agpl-3.0", "spdx_id": "AGPL-3.0"},
            "private": False,
            "fork": False,
            "parent": None,
            "forks_count": 14,
            "stargazers_count": 147,
            "default_branch": "main",
            "archived": False,
            "created_at": "2026-09-04T11:23:46Z",
            "pushed_at": "2026-09-05T12:26:34Z",
        }
        self.languages = {"Python": 90_000, "Shell": 45_000}
        self.raw = {
            "README.md": b"# Recipe\n",
            "start.sh": b"#!/bin/sh\necho start\n",
            "files/patch.py": b"print('patched')\n",
        }
        self.media: dict[str, bytes] = {}
        self.extra_tree: list[dict] = []
        self.truncated = False
        self.range_ok = True
        self.calls: list[tuple[str, str | None, str | None]] = []
        # url substring -> exception raised for the next matching request(s)
        self.fail: dict[str, list[BaseException]] = {}

    # -- wiring

    def tree(self) -> list[dict]:
        entries = [
            {
                "path": path,
                "type": "blob",
                "mode": "100644",
                "sha": f"{i:040x}",
                "size": len(data),
            }
            for i, (path, data) in enumerate(sorted(self.raw.items()), start=1)
        ]
        return entries + self.extra_tree

    def __call__(self, request, timeout=None):
        url = request.full_url
        self.calls.append(
            (url, request.get_header("Range"), request.get_header("Authorization"))
        )
        for key, queued in self.fail.items():
            if key in url and queued:
                raise queued.pop(0)
        if url.startswith(API_ROOT):
            return self._api(url[len(API_ROOT) :])
        if url.startswith(RAW_ROOT):
            return self._blob(url, RAW_ROOT, self.raw, request)
        if url.startswith(MEDIA_ROOT):
            return self._blob(url, MEDIA_ROOT, self.media, request)
        raise AssertionError(f"unexpected URL {url}")

    def _api(self, path: str):
        base = f"/repos/{self.locator}"
        if path == base:
            return self._json(self.repo)
        if path.startswith(f"{base}/commits/"):
            ref = unquote(path[len(base) + len("/commits/") :])
            sha = self.refs.get(ref) or (ref if ref in self.refs.values() else None)
            if sha is None:
                return self._raise(
                    path,
                    422,
                    body=json.dumps({"message": f"No commit found for SHA: {ref}"}),
                )
            return self._json(
                {"sha": sha, "commit": {"committer": {"date": "2026-09-05T12:26:24Z"}}}
            )
        if path.startswith(f"{base}/git/trees/"):
            return self._json(
                {"sha": self.sha, "tree": self.tree(), "truncated": self.truncated}
            )
        if path == f"{base}/languages":
            return self._json(self.languages)
        return self._raise(path, 404)

    def _blob(self, url: str, root: str, store: dict, request):
        rest = url[len(root) + 1 :]
        _owner, _repo, _sha, relative = rest.split("/", 3)
        relative = unquote(relative)
        if relative not in store:
            return self._raise(url, 404)
        body = store[relative]
        wanted = request.get_header("Range")
        if wanted and self.range_ok:
            spec = wanted.removeprefix("bytes=")
            start_s, _, end_s = spec.partition("-")
            start = int(start_s)
            end = int(end_s) if end_s else len(body) - 1
            if start >= len(body):
                return self._raise(url, 416)
            piece = body[start : end + 1]
            return FakeResponse(
                piece,
                206,
                {
                    "Content-Length": str(len(piece)),
                    "Content-Range": f"bytes {start}-{start + len(piece) - 1}/{len(body)}",
                },
            )
        return FakeResponse(body, 200)

    @staticmethod
    def _json(payload):
        return FakeResponse(json.dumps(payload).encode("utf-8"))

    @staticmethod
    def _raise(url: str, code: int, headers: dict | None = None, body: str = ""):
        raise _http_error(url, code, headers, body.encode("utf-8"))


@pytest.fixture
def fake():
    return FakeGitHub()


@pytest.fixture
def provider(fake):
    return GitHubProvider(opener=fake)


@pytest.fixture
def source(provider):
    return provider.parse("MiaAI-Lab/Recipe")


# ---------------------------------------------------------------- addresses


def test_github_is_registered_with_alias_and_host():
    assert "github" in provider_names()
    assert get_provider("gh") is get_provider("github")
    ref = parse_source("https://github.com/MiaAI-Lab/Recipe")
    assert ref.provider == "github"
    assert ref.artifact_type == "code"


@pytest.mark.parametrize(
    "text",
    [
        "github:MiaAI-Lab/Recipe",
        "gh:MiaAI-Lab/Recipe",
        "github://MiaAI-Lab/Recipe",
        "https://github.com/MiaAI-Lab/Recipe",
        "https://github.com/MiaAI-Lab/Recipe/",
        "https://github.com/MiaAI-Lab/Recipe.git",
        "https://www.github.com/MiaAI-Lab/Recipe?tab=readme-ov-file#usage",
        "https://github.com/MiaAI-Lab/Recipe/issues/12",
        "https://github.com/MiaAI-Lab/Recipe/pulls",
    ],
)
def test_parse_every_spelling_of_one_repository(text):
    ref = parse_source(text)
    assert ref.provider == "github"
    assert ref.locator == "MiaAI-Lab/Recipe"
    assert ref.canonical == "github:MiaAI-Lab/Recipe"
    assert ref.url == "https://github.com/MiaAI-Lab/Recipe"
    assert ref.bundle_name == "github--miaai-lab--recipe"
    assert ref.publisher == "MiaAI-Lab"
    assert ref.name == "Recipe"
    assert ref.artifact_type == "code"


@pytest.mark.parametrize(
    ("url", "ref"),
    [
        ("https://github.com/o/r/tree/v1.2", "v1.2"),
        ("https://github.com/o/r/tree/main/files/patch.py", "main"),
        ("https://github.com/o/r/blob/main/README.md", "main"),
        ("https://github.com/o/r/commit/203834ca8800", "203834ca8800"),
        ("https://github.com/o/r/releases/tag/v1.2", "v1.2"),
    ],
)
def test_parse_refuses_a_revision_buried_in_the_url_and_names_the_flag(url, ref):
    with pytest.raises(SystemExit) as exc:
        parse_source(url)
    message = str(exc.value)
    assert "names a revision" in message
    assert f"darsay archive github:o/r --revision {ref}" in message


@pytest.mark.parametrize("text", ["github:only", "github:a/b/c", "github:a b/c", "gh:"])
def test_parse_rejects_malformed_locators(text):
    with pytest.raises(SystemExit, match="cannot parse source ref"):
        parse_source(text)


def test_parse_refuses_a_foreign_url(provider):
    with pytest.raises(SystemExit, match="not a GitHub URL"):
        provider.parse("https://huggingface.co/Qwen/Qwen3-0.6B")


# ---------------------------------------------------------------------- pin


def test_pin_records_the_commit_the_tree_and_what_upstream_said(provider, source, fake):
    snap = provider.pin(source, None)
    assert snap.revision == SHA
    assert snap.revision_ref == "HEAD"
    assert [f.path for f in snap.files] == ["README.md", "files/patch.py", "start.sh"]
    readme = snap.files[0]
    assert readme.size == len(fake.raw["README.md"])
    assert readme.git_sha1 == "0000000000000000000000000000000000000001"
    assert readme.sha256 is None
    assert snap.license_id == "agpl-3.0"
    assert snap.parameters is None and snap.pipeline_tag is None
    assert snap.last_modified == "2026-09-05T12:26:24Z"
    meta = snap.metadata
    assert meta["card_data"] == {"license": "agpl-3.0"}
    assert meta["tags"] == ["vllm", "recipe"]
    assert meta["gated"] is False
    assert meta["likes"] == 147 and meta["downloads"] is None
    repo = meta["repository"]
    assert repo["description"] == "Serve acme/toy on one box"
    assert repo["languages"] == {"Python": 90_000, "Shell": 45_000}
    assert repo["default_branch"] == "main"
    assert repo["forks_count"] == 14 and repo["stars"] == 147
    assert repo["submodules"] is None and repo["symlinks"] is None
    assert repo["lfs_file_count"] == 0
    # The API calls a pin costs, in order: repo, commit, tree, languages.
    api = [url for url, _, _ in fake.calls if url.startswith(API_ROOT)]
    assert [u.split("/repos/MiaAI-Lab/Recipe")[1] for u in api] == [
        "",
        "/commits/HEAD",
        f"/git/trees/{SHA}?recursive=1",
        "/languages",
    ]


def test_pin_takes_a_named_revision(provider, source, fake):
    snap = provider.pin(source, "v1")
    assert snap.revision == "1" * 40
    assert snap.revision_ref == "v1"
    assert any("/commits/v1" in url for url, _, _ in fake.calls)


def test_pin_records_submodules_and_symlinks_without_fetching_them(
    provider, source, fake
):
    fake.extra_tree = [
        {"path": "vendor/vllm", "type": "commit", "mode": "160000", "sha": "f" * 40},
        {
            "path": "latest",
            "type": "blob",
            "mode": "120000",
            "sha": "e" * 40,
            "size": 8,
        },
    ]
    fake.raw["latest"] = b"start.sh"
    snap = provider.pin(source, None)
    repo = snap.metadata["repository"]
    assert repo["submodules"] == [{"path": "vendor/vllm", "revision": "f" * 40}]
    assert repo["symlinks"] == ["latest"]
    assert "vendor/vllm" not in [f.path for f in snap.files]
    assert "latest" in [f.path for f in snap.files]


def test_pin_resolves_lfs_pointers_to_object_size_and_digest(provider, source, fake):
    fake.raw[".gitattributes"] = b"*.bin filter=lfs diff=lfs merge=lfs -text\n"
    fake.raw["weights.bin"] = POINTER
    fake.raw["notes.bin.txt"] = b"not routed\n"
    snap = provider.pin(source, None)
    by_path = {f.path: f for f in snap.files}
    weights = by_path["weights.bin"]
    assert weights.size == 5
    assert weights.sha256 == "ab" * 32
    assert weights.git_sha1 is None
    assert by_path["notes.bin.txt"].git_sha1 is not None
    assert snap.metadata["repository"]["lfs_file_count"] == 1
    # Only the pointer-sized routed blobs were read, once each.
    reads = [url for url, _, _ in fake.calls if url.startswith(RAW_ROOT)]
    assert sorted(u.rsplit("/", 1)[1] for u in reads) == [
        ".gitattributes",
        "weights.bin",
    ]


def test_pin_scopes_a_nested_gitattributes_to_its_directory(provider, source, fake):
    fake.raw["models/.gitattributes"] = b"*.bin filter=lfs\n"
    fake.raw["models/w.bin"] = POINTER
    fake.raw["top.bin"] = b"plain bytes, not routed"
    snap = provider.pin(source, None)
    by_path = {f.path: f for f in snap.files}
    assert by_path["models/w.bin"].sha256 == "ab" * 32
    assert by_path["top.bin"].sha256 is None


def test_pin_refuses_a_routed_blob_that_is_not_a_pointer(provider, source, fake):
    fake.raw[".gitattributes"] = b"*.bin filter=lfs\n"
    fake.raw["broken.bin"] = b"this is not a pointer"
    with pytest.raises(SourceError, match="broken.bin .*not a readable LFS pointer"):
        provider.pin(source, None)


def test_pin_refuses_a_truncated_tree(provider, source, fake):
    fake.truncated = True
    with pytest.raises(SourceError, match="partial inventory"):
        provider.pin(source, None)


def test_pin_survives_a_failed_languages_query(provider, source, fake):
    fake.fail["/languages"] = [_http_error("x", 500)]
    snap = provider.pin(source, None)
    assert snap.metadata["repository"]["languages"] is None


def test_pin_not_found(provider, source, fake):
    fake.fail["/repos/MiaAI-Lab/Recipe"] = [_http_error("x", 404)]
    with pytest.raises(SourceNotFoundError, match="not found on GitHub"):
        provider.pin(source, None)


def test_pin_bad_token(provider, source, fake):
    fake.fail["/repos/MiaAI-Lab/Recipe"] = [_http_error("x", 401)]
    with pytest.raises(SourceGatedError, match="rejected the token"):
        provider.pin(source, None)


def test_pin_private_without_token(provider, source, fake):
    fake.fail["/repos/MiaAI-Lab/Recipe"] = [_http_error("x", 403)]
    with pytest.raises(SourceGatedError, match="set GITHUB_TOKEN"):
        provider.pin(source, None)


def test_pin_rate_limit_names_the_reset_and_the_token(
    provider, source, fake, monkeypatch
):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    fake.fail["/repos/MiaAI-Lab/Recipe"] = [
        _http_error(
            "x", 403, {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "1800000000"}
        )
    ]
    with pytest.raises(SourceError) as exc:
        provider.pin(source, None)
    message = str(exc.value)
    assert "rate limit exhausted" in message
    assert "resets 2027-01-15" in message
    assert "set GITHUB_TOKEN" in message


def test_pin_unknown_revision_carries_githubs_reason(provider, source):
    with pytest.raises(SourceError, match="No commit found for SHA: nope"):
        provider.pin(source, "nope")


def test_pin_offline_says_so(provider, source, fake):
    fake.fail["/repos/MiaAI-Lab/Recipe"] = [URLError(socket.gaierror(8, "nodename"))]
    with pytest.raises(SourceError, match="cannot reach GitHub.*DNS lookup failed"):
        provider.pin(source, None)


def test_token_travels_as_a_bearer_header(provider, source, fake, monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "ghp_test")
    provider.pin(source, None)
    assert all(auth == "Bearer ghp_test" for _, _, auth in fake.calls)


# ------------------------------------------------------------------ download


class _Counter:
    def __init__(self):
        self.added: list[int] = []

    def add(self, n, defer_only=False):
        self.added.append(int(n))

    def poll(self):
        return None


def _bar(provider):
    counter = _Counter()
    return provider.progress_wrapper(counter), counter


def test_download_writes_the_file_and_reports_every_byte(
    provider, source, fake, tmp_path
):
    bar, counter = _bar(provider)
    provider.download_file(
        source, SHA, "files/patch.py", tmp_path, force=False, tqdm_class=bar
    )
    assert (tmp_path / "files" / "patch.py").read_bytes() == fake.raw["files/patch.py"]
    assert sum(counter.added) == len(fake.raw["files/patch.py"])
    assert not list((tmp_path / ".cache").rglob("*.incomplete"))


def test_download_skips_a_file_already_in_place(provider, source, fake, tmp_path):
    (tmp_path / "README.md").write_bytes(b"already here")
    provider.download_file(
        source, SHA, "README.md", tmp_path, force=False, tqdm_class=None
    )
    assert (tmp_path / "README.md").read_bytes() == b"already here"
    assert fake.calls == []


def test_download_resumes_a_partial_with_a_range_request(
    provider, source, fake, tmp_path
):
    fake.raw["big.bin"] = bytes(range(256)) * 4
    incomplete = tmp_path / ".cache" / "github" / "big.bin.incomplete"
    incomplete.parent.mkdir(parents=True)
    incomplete.write_bytes(fake.raw["big.bin"][:300])
    assert provider.partial_bytes(tmp_path, {"path": "big.bin"}) == 300
    bar, counter = _bar(provider)
    provider.download_file(
        source, SHA, "big.bin", tmp_path, force=False, tqdm_class=bar
    )
    assert (tmp_path / "big.bin").read_bytes() == fake.raw["big.bin"]
    assert sum(counter.added) == len(fake.raw["big.bin"]) - 300
    assert any(rng == "bytes=300-" for _, rng, _ in fake.calls)


def test_download_restarts_when_the_host_ignores_range(
    provider, source, fake, tmp_path
):
    fake.range_ok = False
    fake.raw["big.bin"] = bytes(range(256)) * 4
    incomplete = tmp_path / ".cache" / "github" / "big.bin.incomplete"
    incomplete.parent.mkdir(parents=True)
    incomplete.write_bytes(b"garbage that must not survive")
    provider.download_file(
        source, SHA, "big.bin", tmp_path, force=False, tqdm_class=None
    )
    assert (tmp_path / "big.bin").read_bytes() == fake.raw["big.bin"]


def test_download_force_discards_the_partial(provider, source, fake, tmp_path):
    incomplete = tmp_path / ".cache" / "github" / "README.md.incomplete"
    incomplete.parent.mkdir(parents=True)
    incomplete.write_bytes(b"stale")
    (tmp_path / "README.md").write_bytes(b"stale copy")
    provider.download_file(
        source, SHA, "README.md", tmp_path, force=True, tqdm_class=None
    )
    assert (tmp_path / "README.md").read_bytes() == fake.raw["README.md"]
    assert not any(rng for _, rng, _ in fake.calls)


def test_download_fetches_the_lfs_object_never_the_pointer(
    provider, source, fake, tmp_path
):
    fake.raw["weights.bin"] = POINTER
    fake.media["weights.bin"] = b"12345"
    bar, counter = _bar(provider)
    provider.download_file(
        source, SHA, "weights.bin", tmp_path, force=False, tqdm_class=bar
    )
    assert (tmp_path / "weights.bin").read_bytes() == b"12345"
    assert sum(counter.added) == 5
    hosts = [url.split("/")[2] for url, _, _ in fake.calls]
    assert hosts == ["raw.githubusercontent.com", "media.githubusercontent.com"]


def test_download_missing_file_is_not_found(provider, source, tmp_path):
    with pytest.raises(SourceNotFoundError, match="HTTP 404"):
        provider.download_file(
            source, SHA, "gone.txt", tmp_path, force=False, tqdm_class=None
        )


def test_download_forbidden_is_gated(provider, source, fake, tmp_path):
    fake.fail["README.md"] = [_http_error("x", 403)]
    with pytest.raises(SourceGatedError, match="GITHUB_TOKEN"):
        provider.download_file(
            source, SHA, "README.md", tmp_path, force=False, tqdm_class=None
        )


def test_download_outage_status_propagates_as_transient(
    provider, source, fake, tmp_path
):
    fake.fail["README.md"] = [_http_error("x", 503)]
    with pytest.raises(HTTPError) as exc:
        provider.download_file(
            source, SHA, "README.md", tmp_path, force=False, tqdm_class=None
        )
    assert provider.transient_network_error(exc.value) == "GitHub responded 503"


def test_rate_cap_shrinks_the_read_chunk(provider, tmp_path):
    assert provider._chunk == 1024 * 1024
    with provider.transfer_session(tmp_path, max_rate=512 * 1024):
        assert provider._chunk == 128 * 1024
    assert provider._chunk == 1024 * 1024


# ---------------------------------------------------------------- transient


@pytest.mark.parametrize(
    ("exc", "reason"),
    [
        (URLError(socket.gaierror(8, "nodename")), "DNS lookup failed"),
        (URLError(ConnectionResetError()), "connection reset"),
        (URLError("unreachable"), "connection failed"),
        (_http_error("x", 502), "GitHub responded 502"),
        (_http_error("x", 429), "GitHub responded 429"),
        (_http_error("x", 404), None),
        (_http_error("x", 403), None),
        # RemoteDisconnected is a ConnectionResetError first; that reading wins.
        (RemoteDisconnected("gone"), "connection reset"),
        (IncompleteRead(b"half"), "connection closed by the server"),
        (TimeoutError("slow"), "timed out"),
        (ValueError("not the network"), None),
    ],
)
def test_transient_classification(provider, exc, reason):
    assert provider.transient_network_error(exc) == reason


# --------------------------------------------------------------- read_bytes


def test_read_bytes_is_a_bounded_range_read(provider, source, fake):
    fake.raw["big.bin"] = bytes(range(256))
    assert provider.read_bytes(source, SHA, "big.bin", 10, 5) == bytes(range(10, 15))
    assert provider.read_bytes(source, SHA, "big.bin", 300, 5) == b""
    assert provider.read_bytes(source, SHA, "big.bin", 0, 0) == b""


def test_read_bytes_refuses_a_host_that_ignores_range(provider, source, fake):
    fake.raw["big.bin"] = bytes(range(256))
    fake.range_ok = False
    with pytest.raises(SourceError, match="ignored a Range request"):
        provider.read_bytes(source, SHA, "big.bin", 10, 5)


# ------------------------------------------------------------------- record


def test_lineage_is_the_fork_edge_and_the_fork_count(provider, source):
    plain = {"repository": {"fork": False, "parent": None, "forks_count": 3}}
    assert provider.declared_parents(source, plain) is None
    forked = {"repository": {"fork": True, "parent": "lancelind/qwen3.8-Flash-DGX"}}
    assert provider.declared_parents(source, forked) == [
        {
            "source": "github:lancelind/qwen3.8-Flash-DGX",
            "relation": "fork",
            "declared_by": "api",
        }
    ]
    record = provider.lineage(source, plain)
    assert record["parents"] is None
    assert record["descendants"] == {"forks_count": 3}
    assert record["query_limit"] is None
    assert record["successors"] is None and record["related"] is None


def test_access_record_names_a_private_repository(provider):
    assert provider.access_record({"gated": False}) == {"gated": False, "notes": None}
    private = provider.access_record({"gated": "private"})
    assert private["gated"] == "private"
    assert "GITHUB_TOKEN" in private["notes"]


def test_lfs_helpers():
    assert parse_lfs_pointer(POINTER) == {"sha256": "ab" * 32, "size": 5}
    assert parse_lfs_pointer(b"just text") is None
    assert parse_lfs_pointer(POINTER + b"x" * 2000) is None
    text = "# comment\n*.bin filter=lfs diff=lfs merge=lfs -text\n*.md text\nmodels/*.pt filter=lfs\n"
    assert lfs_patterns(text) == ["*.bin", "models/*.pt"]
    assert matches_lfs_pattern("deep/dir/w.bin", "*.bin")
    assert matches_lfs_pattern("models/w.pt", "models/*.pt")
    assert not matches_lfs_pattern("other/w.pt", "models/*.pt")
    assert matches_lfs_pattern("w.pt", "/w.pt")
    assert not matches_lfs_pattern("sub/w.pt", "/w.pt")

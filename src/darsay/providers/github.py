"""GitHub acquisition backend.

``darsay archive github:owner/repo`` (``gh:owner/repo``, or a github.com
repository URL) pins one commit, lists its tree, and fetches every blob.
Standard library HTTP only: the REST API for the pin, raw content for the
bytes, and Git LFS media for pointers the tree names.

A GitHub repository is archived as a **code** bundle — a source tree at a
commit, kept under ``code/``. It is what the tree *is*; what it is *for* (a
serving harness, training code, a paper) is a curator's call, and the
manifest records which standard runtime declarations the tree carries
(``code_metadata.runtime_declarations``) without asserting a purpose.

Every file carries the git blob SHA-1 the tree names, so ``verify`` has an
upstream expectation for each byte; an LFS pointer resolves to the object's
SHA-256 and true size at pin time. Authentication is ``GITHUB_TOKEN`` or
``GH_TOKEN`` in the environment — needed for private repositories and for
more than the unauthenticated API allowance.
"""

from __future__ import annotations

import json
import os
import re
import socket
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from fnmatch import fnmatch
from http.client import HTTPException, IncompleteRead, RemoteDisconnected
from pathlib import Path, PurePosixPath
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen

from .base import (
    FileSpec,
    Snapshot,
    SourceError,
    SourceGatedError,
    SourceNotFoundError,
    SourceProvider,
    SourceRef,
    describe_network_error,
    iter_causes,
    throttled_chunk_size,
)

API_ROOT = "https://api.github.com"
API_VERSION = "2022-11-28"
RAW_ROOT = "https://raw.githubusercontent.com"
MEDIA_ROOT = "https://media.githubusercontent.com/media"
TIMEOUT_S = 60
CHUNK_BYTES = 1024 * 1024

# Responses that mean "try again shortly", not "this request is wrong".
_TRANSIENT_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})

# A Git LFS pointer: three lines, the object's SHA-256 and true size.
LFS_POINTER_HEAD = b"version https://git-lfs.github.com/spec/v1"
LFS_POINTER_MAX_BYTES = 1024
_LFS_OID = re.compile(rb"^oid sha256:([0-9a-f]{64})$", re.MULTILINE)
_LFS_SIZE = re.compile(rb"^size (\d+)$", re.MULTILINE)

_NAME = re.compile(r"^[A-Za-z0-9_.-]+$")
# URL path segments after owner/repo that carry a revision GitHub's web UI
# put there; darsay takes revisions on --revision, so these are refused
# with the command that says the same thing unambiguously.
_REVISION_SEGMENTS = ("tree", "blob", "commit", "commits", "tag")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def github_token() -> str | None:
    """The token the environment offers, if any."""
    for key in ("GITHUB_TOKEN", "GH_TOKEN"):
        value = (os.environ.get(key) or "").strip()
        if value:
            return value
    return None


def parse_lfs_pointer(data: bytes) -> dict | None:
    """``{"sha256", "size"}`` from Git LFS pointer bytes, else ``None``."""
    if not data.startswith(LFS_POINTER_HEAD) or len(data) > LFS_POINTER_MAX_BYTES:
        return None
    oid = _LFS_OID.search(data)
    size = _LFS_SIZE.search(data)
    if not oid or not size:
        return None
    return {"sha256": oid.group(1).decode("ascii"), "size": int(size.group(1))}


def lfs_patterns(gitattributes: str) -> list[str]:
    """The path patterns a ``.gitattributes`` file routes through Git LFS."""
    out: list[str] = []
    for raw in gitattributes.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        if any(attr == "filter=lfs" for attr in parts[1:]):
            out.append(parts[0])
    return out


def matches_lfs_pattern(path: str, pattern: str) -> bool:
    """gitattributes matching, the two rules that matter: a pattern with a
    slash is anchored at its ``.gitattributes`` directory; one without
    matches the file name at any depth."""
    pattern = pattern.strip()
    if pattern.startswith("/"):
        return fnmatch(path, pattern[1:])
    if "/" in pattern:
        return fnmatch(path, pattern)
    return fnmatch(PurePosixPath(path).name, pattern)


def _json_safe(value):
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


class GitHubProvider(SourceProvider):
    name = "github"
    label = "GitHub"
    aliases = ("gh",)
    url_hosts = ("github.com",)
    # The repository's default branch, whatever it is called. GitHub
    # resolves ``HEAD`` to it; darsay never assumes ``main``.
    default_revision = "HEAD"

    def __init__(self, opener=None) -> None:
        # ``opener(request, timeout=...)`` → a response; tests inject one.
        self._opener = opener or urlopen
        self._chunk = CHUNK_BYTES

    # ------------------------------------------------------------ addresses

    def parse(self, locator: str, *, from_url: bool = False) -> SourceRef:
        s = locator.strip()
        if s.lower().startswith(("https://", "http://")):
            parsed = urlparse(s)
            host = parsed.netloc.lower().split("@")[-1].removeprefix("www.")
            if host not in self.url_hosts:
                raise SystemExit(f"error: not a {self.label} URL: {locator!r}")
            s = parsed.path.lstrip("/")
            from_url = True
        s = s.split("?", 1)[0].split("#", 1)[0].strip("/")
        parts = [p for p in s.split("/") if p]
        if from_url and len(parts) > 2:
            owner, repo = parts[0], parts[1].removesuffix(".git")
            ref = None
            if parts[2] in _REVISION_SEGMENTS and len(parts) > 3:
                ref = parts[3]
            elif parts[2] == "releases" and len(parts) > 4 and parts[3] == "tag":
                ref = parts[4]
            if ref:
                raise SystemExit(
                    f"error: {locator!r} names a revision inside the repository "
                    "URL. Archive that revision as:\n"
                    f"  darsay archive github:{owner}/{repo} --revision {ref}"
                )
            parts = parts[:2]
        if len(parts) != 2:
            raise SystemExit(
                f"error: cannot parse source ref {locator!r} — expected "
                "github:owner/repo, gh:owner/repo, or a github.com repository URL"
            )
        owner, repo = parts[0], parts[1].removesuffix(".git")
        if not (_NAME.match(owner) and _NAME.match(repo)):
            raise SystemExit(
                f"error: cannot parse source ref {locator!r} — "
                "owner and repository names are letters, digits, '-', '_' and '.'"
            )
        repo_id = f"{owner}/{repo}"
        return SourceRef(
            provider=self.name,
            artifact_type="code",
            locator=repo_id,
            canonical=f"{self.name}:{repo_id}",
            url=f"https://github.com/{repo_id}",
            bundle_name=f"{self.name}--{owner}--{repo}".lower(),
            publisher=owner,
            name=repo,
        )

    # ------------------------------------------------------------- transport

    def _headers(self, extra: dict | None = None) -> dict:
        from .. import __version__

        headers = {"User-Agent": f"darsay/{__version__}"}
        token = github_token()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if extra:
            headers.update(extra)
        return headers

    def _open(self, url: str, *, headers: dict | None = None, timeout: int = TIMEOUT_S):
        request = Request(url, headers=self._headers(headers))
        return self._opener(request, timeout=timeout)

    def _api(self, path: str, *, what: str) -> dict | list:
        """One REST call, JSON-decoded; every failure is a CLI-ready error."""
        url = API_ROOT + path
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": API_VERSION,
        }
        try:
            with self._open(url, headers=headers) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            raise self._api_error(exc, what) from None
        except (URLError, OSError, HTTPException, ValueError) as exc:
            reason = self.transient_network_error(exc)
            if reason is not None:
                raise SourceError(
                    f"error: cannot reach {self.label} to resolve {what} — "
                    f"{reason}. Check the connection and re-run."
                ) from exc
            raise SourceError(f"error: cannot resolve {what}: {exc}") from exc

    def _api_error(self, exc: HTTPError, what: str) -> SourceError:
        code = exc.code
        headers = exc.headers or {}
        if code == 404:
            return SourceNotFoundError(
                f"error: {what} not found on {self.label} — it may be private "
                "(set GITHUB_TOKEN to a token that can read it), renamed, or "
                "removed. Nothing was archived."
            )
        if code == 401:
            return SourceGatedError(
                f"error: {self.label} rejected the token in GITHUB_TOKEN / GH_TOKEN "
                f"(HTTP 401) while resolving {what}. Nothing was archived."
            )
        if code in (403, 429) and str(headers.get("X-RateLimit-Remaining", "")) == "0":
            reset = headers.get("X-RateLimit-Reset")
            when = ""
            if reset and str(reset).isdigit():
                at = datetime.fromtimestamp(int(reset), tz=timezone.utc)
                when = f" (resets {at.isoformat(timespec='seconds')})"
            hint = (
                "authenticated requests get a far larger allowance — set GITHUB_TOKEN"
                if not github_token()
                else "wait for the reset"
            )
            return SourceError(
                f"error: {self.label} API rate limit exhausted while resolving "
                f"{what}{when}; {hint}. Nothing was archived."
            )
        if code == 403:
            return SourceGatedError(
                f"error: {what} requires authorization on {self.label} (HTTP 403) — "
                "set GITHUB_TOKEN to a token that can read it. Nothing was archived."
            )
        if code in _TRANSIENT_STATUSES:
            return SourceError(
                f"error: cannot reach {self.label} to resolve {what} — "
                f"{self.label} responded {code}. Re-run shortly."
            )
        detail = ""
        try:
            body = json.loads(exc.read().decode("utf-8"))
            if isinstance(body, dict) and body.get("message"):
                detail = f": {body['message']}"
        except Exception:
            pass
        return SourceError(f"error: cannot resolve {what} (HTTP {code}){detail}")

    def transient_network_error(self, exc: BaseException) -> str | None:
        reason = describe_network_error(exc)
        if reason is not None:
            return reason
        for node in iter_causes(exc):
            if isinstance(node, HTTPError):
                if node.code in _TRANSIENT_STATUSES:
                    return f"{self.label} responded {node.code}"
                return None
            if isinstance(node, URLError):
                inner = node.reason
                if isinstance(inner, BaseException):
                    return describe_network_error(inner) or "connection failed"
                return "connection failed"
            if isinstance(node, (IncompleteRead, RemoteDisconnected)):
                return "connection closed by the server"
            if isinstance(node, (socket.timeout, TimeoutError)):
                return "timed out"
            if isinstance(node, HTTPException):
                return "connection failed"
        return None

    # -------------------------------------------------------------------- pin

    def pin(
        self,
        source: SourceRef,
        revision: str | None,
        *,
        require_access: bool = False,
    ) -> Snapshot:
        locator = source.locator
        ref = revision or self.default_revision
        what = f"{source.canonical} @ {ref}"
        repo = self._api(f"/repos/{locator}", what=what)
        commit = self._api(f"/repos/{locator}/commits/{quote(ref, safe='')}", what=what)
        sha = commit["sha"]
        tree = self._api(f"/repos/{locator}/git/trees/{sha}?recursive=1", what=what)
        entries = list(tree.get("tree") or [])
        if tree.get("truncated"):
            raise SourceError(
                f"error: {what}: {self.label} cannot list this tree in one call "
                f"({len(entries)} entries returned, more exist), and darsay will "
                "not pin a partial inventory. Nothing was archived."
            )
        try:
            languages = self._api(f"/repos/{locator}/languages", what=what)
        except SourceError:
            languages = None

        blobs = [e for e in entries if e.get("type") == "blob"]
        submodules = [
            {"path": e["path"], "revision": e.get("sha")}
            for e in entries
            if e.get("type") == "commit"
        ]
        symlinks = [e["path"] for e in blobs if e.get("mode") == "120000"]
        lfs = self._lfs_objects(source, sha, blobs)

        files = []
        for entry in blobs:
            path = entry["path"]
            pointer = lfs.get(path)
            if pointer is not None:
                files.append(
                    FileSpec(path=path, size=pointer["size"], sha256=pointer["sha256"])
                )
            else:
                files.append(
                    FileSpec(
                        path=path, size=entry.get("size"), git_sha1=entry.get("sha")
                    )
                )
        files.sort(key=lambda item: item.path)

        committed = ((commit.get("commit") or {}).get("committer") or {}).get("date")
        license_key = (repo.get("license") or {}).get("key")
        parent = repo.get("parent") or {}
        metadata = {
            "card_data": {"license": license_key},
            "tags": list(repo.get("topics") or []),
            "gated": "private" if repo.get("private") else False,
            "created_at": repo.get("created_at"),
            "last_modified": committed,
            "downloads": None,
            # Stars are GitHub's likes; recorded under the same key.
            "likes": repo.get("stargazers_count"),
            "repository": _json_safe(
                {
                    "description": repo.get("description"),
                    "homepage": repo.get("homepage") or None,
                    "topics": list(repo.get("topics") or []) or None,
                    "languages": languages if isinstance(languages, dict) else None,
                    "default_branch": repo.get("default_branch"),
                    "archived_upstream": repo.get("archived"),
                    "fork": bool(repo.get("fork")),
                    "parent": parent.get("full_name") or None,
                    "forks_count": repo.get("forks_count"),
                    "stars": repo.get("stargazers_count"),
                    "pushed_at": repo.get("pushed_at"),
                    "submodules": submodules or None,
                    "symlinks": symlinks or None,
                    "lfs_file_count": len(lfs),
                }
            ),
        }
        return Snapshot(
            source=source,
            revision=sha,
            revision_ref=ref,
            files=files,
            metadata=metadata,
            parameters=None,
            pipeline_tag=None,
            license_id=license_key,
            last_modified=committed,
        )

    def _lfs_objects(self, source: SourceRef, sha: str, blobs: list[dict]) -> dict:
        """``{path: {"sha256", "size"}}`` for every blob ``.gitattributes``
        routes through LFS. A pointer that cannot be read refuses the pin
        rather than leaving a knowingly wrong size in the inventory."""
        attribute_files = [
            e["path"]
            for e in blobs
            if PurePosixPath(e["path"]).name == ".gitattributes"
        ]
        if not attribute_files:
            return {}
        rules: list[tuple[str, list[str]]] = []
        for attr_path in attribute_files:
            text = self._fetch_small(source, sha, attr_path).decode("utf-8", "replace")
            patterns = lfs_patterns(text)
            if patterns:
                prefix = attr_path[: -len(".gitattributes")]
                rules.append((prefix, patterns))
        if not rules:
            return {}
        out: dict[str, dict] = {}
        for entry in blobs:
            path = entry["path"]
            if (entry.get("size") or 0) > LFS_POINTER_MAX_BYTES:
                continue
            routed = False
            for prefix, patterns in rules:
                if prefix and not path.startswith(prefix):
                    continue
                relative = path[len(prefix) :]
                if any(matches_lfs_pattern(relative, p) for p in patterns):
                    routed = True
                    break
            if not routed:
                continue
            pointer = parse_lfs_pointer(self._fetch_small(source, sha, path))
            if pointer is None:
                raise SourceError(
                    f"error: {path} in {source.canonical} @ {sha[:12]} is routed "
                    "through Git LFS by .gitattributes but is not a readable LFS "
                    "pointer, so its size and digest cannot be established. "
                    "Nothing was archived."
                )
            out[path] = pointer
        return out

    def _fetch_small(self, source: SourceRef, revision: str, relative: str) -> bytes:
        url = self._raw_url(source, revision, relative)
        try:
            with self._open(url) as response:
                return response.read()
        except HTTPError as exc:
            raise self._download_error(exc, source, relative) from None
        except (URLError, OSError, HTTPException) as exc:
            reason = self.transient_network_error(exc) or str(exc)
            raise SourceError(
                f"error: cannot read {relative} from {source.canonical}: {reason}"
            ) from exc

    # ------------------------------------------------------------- transfer

    @staticmethod
    def _raw_url(source: SourceRef, revision: str, relative: str) -> str:
        return f"{RAW_ROOT}/{source.locator}/{revision}/{quote(relative)}"

    @staticmethod
    def _media_url(source: SourceRef, revision: str, relative: str) -> str:
        return f"{MEDIA_ROOT}/{source.locator}/{revision}/{quote(relative)}"

    @staticmethod
    def _incomplete_path(payload_dir: Path, relative: str) -> Path:
        parts = PurePosixPath(relative).parts
        return payload_dir.joinpath(".cache", "github", *parts[:-1]) / (
            f"{parts[-1]}.incomplete"
        )

    def _download_error(self, exc: HTTPError, source: SourceRef, relative: str):
        if exc.code == 404:
            return SourceNotFoundError(
                f"error: {relative} is not at {source.canonical} on {self.label} "
                "(HTTP 404) — the pinned revision may have been rewritten upstream."
            )
        if exc.code in (401, 403):
            return SourceGatedError(self.access_denied_message(source, partial=True))
        if exc.code in _TRANSIENT_STATUSES:
            # The transfer loop waits these out and resumes the partial.
            return exc
        return SourceError(
            f"error: {self.label} refused {relative} from {source.canonical} "
            f"(HTTP {exc.code})."
        )

    def download_file(
        self,
        source: SourceRef,
        revision: str,
        relative: str,
        payload_dir: Path,
        *,
        force: bool,
        tqdm_class,
    ) -> None:
        dest = payload_dir.joinpath(*PurePosixPath(relative).parts)
        if dest.exists() and not force:
            return
        incomplete = self._incomplete_path(payload_dir, relative)
        if force:
            incomplete.unlink(missing_ok=True)
        incomplete.parent.mkdir(parents=True, exist_ok=True)
        url = self._raw_url(source, revision, relative)
        try:
            self._stream(url, source, revision, relative, incomplete, tqdm_class)
        except HTTPError as exc:
            raise self._download_error(exc, source, relative) from None
        dest.parent.mkdir(parents=True, exist_ok=True)
        incomplete.replace(dest)

    def _stream(
        self,
        url: str,
        source: SourceRef,
        revision: str,
        relative: str,
        incomplete: Path,
        tqdm_class,
        *,
        pointer_check: bool = True,
    ) -> None:
        """Bytes of ``url`` onto ``incomplete``, resuming what is there.

        A ``206`` appends; a ``200`` to a Range request means the host does
        not resume this file, so the partial is replaced rather than
        corrupted. A small body that is a Git LFS pointer is fetched again
        from the LFS media host — the archive holds objects, never pointers.
        """
        resume = incomplete.stat().st_size if incomplete.exists() else 0
        headers = {"Range": f"bytes={resume}-"} if resume else {}
        try:
            response = self._open(url, headers=headers)
        except HTTPError as exc:
            if exc.code == 416 and resume:
                # Nothing past what is banked: the partial is the whole file.
                return
            raise
        with response:
            status = getattr(response, "status", None) or response.getcode()
            length = response.headers.get("Content-Length")
            body_len = int(length) if length and str(length).isdigit() else None
            if resume and status == 206:
                mode = "ab"
                total = resume + body_len if body_len is not None else None
            else:
                mode, resume = "wb", 0
                total = body_len
            if (
                pointer_check
                and resume == 0
                and total is not None
                and total <= LFS_POINTER_MAX_BYTES
            ):
                body = response.read()
                if parse_lfs_pointer(body) is not None and not url.startswith(
                    MEDIA_ROOT
                ):
                    media = self._media_url(source, revision, relative)
                    return self._stream(
                        media,
                        source,
                        revision,
                        relative,
                        incomplete,
                        tqdm_class,
                        pointer_check=False,
                    )
                bar = (
                    tqdm_class(total=len(body), desc=relative, initial=0)
                    if tqdm_class is not None
                    else None
                )
                try:
                    with incomplete.open("wb") as handle:
                        handle.write(body)
                    if bar is not None:
                        bar.update(len(body))
                finally:
                    if bar is not None:
                        bar.close()
                return
            bar = (
                tqdm_class(total=total, desc=relative, initial=resume)
                if tqdm_class is not None
                else None
            )
            try:
                with incomplete.open(mode) as handle:
                    while True:
                        chunk = response.read(self._chunk)
                        if not chunk:
                            break
                        # Write first, report second: a stop raised by the
                        # report leaves every reported byte on disk.
                        handle.write(chunk)
                        if bar is not None:
                            bar.update(len(chunk))
            finally:
                if bar is not None:
                    bar.close()

    @contextmanager
    def transfer_session(
        self,
        payload_dir: Path,
        *,
        max_rate: int | None = None,
        on_retry=None,
    ) -> Iterator[None]:
        previous = self._chunk
        if max_rate:
            self._chunk = throttled_chunk_size(max_rate, CHUNK_BYTES)
        try:
            yield
        finally:
            self._chunk = previous

    def progress_wrapper(self, counter, meter=None):
        class _Bar:
            """A counter the panel can read (``n`` / ``total``) that reports
            every received chunk onto the archive-level network counter."""

            def __init__(self, *args, **kwargs):
                self.n = int(kwargs.get("initial") or 0)
                self.total = kwargs.get("total")
                self.desc = kwargs.get("desc") or ""
                if meter is not None:
                    meter.attach_bar(self, self.desc)

            def update(self, n=1):
                self.n += int(n or 0)
                counter.add(n)
                if meter is not None:
                    meter.note()

            def update_transfer(self, amount=1):
                self.update(amount)

            def close(self):
                if meter is not None:
                    meter.detach_bar(self)
                return None

        return _Bar

    def partial_bytes(self, payload_dir: Path, expected: dict) -> int:
        try:
            incomplete = self._incomplete_path(payload_dir, expected["path"])
            return incomplete.stat().st_size if incomplete.is_file() else 0
        except (OSError, KeyError, IndexError):
            return 0

    def read_bytes(
        self,
        source: SourceRef,
        revision: str,
        relative: str,
        start: int,
        length: int,
    ) -> bytes:
        if length <= 0:
            return b""
        url = self._raw_url(source, revision, relative)
        headers = {"Range": f"bytes={start}-{start + length - 1}"}
        try:
            with self._open(url, headers=headers) as response:
                status = getattr(response, "status", None) or response.getcode()
                if status != 206 and start:
                    raise SourceError(
                        f"error: {self.label} ignored a Range request for "
                        f"{relative} — cannot read {source.locator} remotely"
                    )
                return response.read(length)
        except HTTPError as exc:
            if exc.code == 416:
                return b""
            if exc.code in (401, 403):
                raise SourceGatedError(
                    f"error: {relative} on {self.label} requires authorization "
                    f"to read (HTTP {exc.code}) — set GITHUB_TOKEN."
                ) from exc
            raise SourceError(
                f"error: cannot read bytes {start}-{start + length - 1} of "
                f"{relative} from {source.canonical}: HTTP {exc.code}"
            ) from exc
        except SourceError:
            raise
        except (URLError, OSError, HTTPException) as exc:
            reason = self.transient_network_error(exc) or str(exc)
            raise SourceError(
                f"error: cannot read bytes {start}-{start + length - 1} of "
                f"{relative} from {source.canonical}: {reason}"
            ) from exc

    # ------------------------------------------------------------- the record

    def exists(self, source: SourceRef) -> bool | None:
        try:
            self._api(f"/repos/{source.locator}", what=source.canonical)
        except SourceNotFoundError:
            return False
        except SourceError:
            return None
        return True

    def declared_parents(self, source: SourceRef, metadata: dict) -> list[dict] | None:
        repo = metadata.get("repository") or {}
        parent = repo.get("parent")
        if repo.get("fork") and isinstance(parent, str) and "/" in parent:
            return [
                {
                    "source": f"{self.name}:{parent}",
                    "relation": "fork",
                    "declared_by": "api",
                }
            ]
        return None

    def lineage(self, source: SourceRef, metadata: dict) -> dict:
        repo = metadata.get("repository") or {}
        return {
            "parents": self.declared_parents(source, metadata),
            "descendants": {"forks_count": repo.get("forks_count")},
            "successors": None,
            "related": None,
            "as_of": _utc_now(),
            "query_limit": None,
        }

    def access_record(self, metadata: dict) -> dict:
        gated = metadata.get("gated") or False
        notes = None
        if gated:
            notes = (
                "Upstream repository is private; the archiving account had read "
                "access. Re-fetching from upstream needs GITHUB_TOKEN with the same."
            )
        return {"gated": gated, "notes": notes}

    def access_denied_message(self, source: SourceRef, *, partial: bool = False) -> str:
        closing = (
            "The partial archive was kept and resumes if access returns."
            if partial
            else "Nothing was archived."
        )
        return (
            f"error: {source.artifact_type} {source.locator} on {self.label} requires "
            "authorization this environment does not carry. Set GITHUB_TOKEN "
            "(or GH_TOKEN) to a token that can read the repository, then re-run.\n"
            f"{closing}"
        )

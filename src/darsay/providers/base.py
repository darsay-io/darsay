"""Provider-neutral acquisition types.

A source provider is one hosting service that can pin a revision, list files,
and fetch bytes. Hugging Face is the first implementation; adding another is
a new class in this package plus one line in ``sources._ensure_providers``.
The public CLI (``darsay archive <source>``) does not change.
"""

from __future__ import annotations

import errno
import socket
from abc import ABC, abstractmethod
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse


class SourceError(Exception):
    """Acquisition failed. The message is CLI-ready (includes the ``error:`` prefix)."""


class SourceGatedError(SourceError):
    """The source requires authorization this account does not have."""


class SourceNotFoundError(SourceError):
    """The locator does not exist, or is private and unauthenticated."""


# errno values that mean "the network went away", in the words a person
# would use. Anything else on an OSError (ENOSPC, EACCES) is a real error.
_ERRNO_REASONS = {
    errno.ENETDOWN: "network is down",
    errno.ENETUNREACH: "network unreachable",
    errno.EHOSTUNREACH: "host unreachable",
    errno.EHOSTDOWN: "host is down",
    errno.ECONNREFUSED: "connection refused",
    errno.ECONNRESET: "connection reset",
    errno.ECONNABORTED: "connection aborted",
    errno.EPIPE: "connection closed",
    errno.ETIMEDOUT: "timed out",
}


def iter_causes(exc: BaseException) -> Iterator[BaseException]:
    """``exc`` and then each exception it was raised from, outermost first.

    Follows ``__context__`` even when a ``raise ... from None`` hid it from
    the traceback: transport libraries translate socket errors that way,
    and the socket error is the one that says what happened.
    """
    seen: set[int] = set()
    node: BaseException | None = exc
    while node is not None and id(node) not in seen:
        seen.add(id(node))
        yield node
        node = node.__cause__ or node.__context__


def _describe_os_error(exc: BaseException) -> str | None:
    if isinstance(exc, (socket.gaierror, socket.herror)):
        return "DNS lookup failed"
    if isinstance(exc, ConnectionRefusedError):
        return "connection refused"
    if isinstance(exc, ConnectionResetError):
        return "connection reset"
    if isinstance(exc, BrokenPipeError):
        return "connection closed"
    if isinstance(exc, ConnectionAbortedError):
        return "connection aborted"
    if isinstance(exc, ConnectionError):
        return "connection failed"
    if isinstance(exc, TimeoutError):
        return "timed out"
    if isinstance(exc, OSError):
        return _ERRNO_REASONS.get(exc.errno)
    return None


def describe_network_error(exc: BaseException) -> str | None:
    """A short reason when ``exc`` or anything it was raised from is the
    operating system saying the network is unreachable; ``None`` otherwise.

    Provider-neutral: transport libraries wrap socket errors in their own
    exception types, so the cause chain is what carries the truth.
    """
    for node in iter_causes(exc):
        reason = _describe_os_error(node)
        if reason:
            return reason
    return None


@dataclass(frozen=True)
class SourceRef:
    """A fully resolved source address. Provider-native locators stay in ``locator``."""

    provider: str
    artifact_type: str
    locator: str
    canonical: str
    url: str
    bundle_name: str
    publisher: str
    name: str


@dataclass
class FileSpec:
    """One file in a pinned snapshot.

    ``sha256`` is the expected payload digest when the provider has one (Hub
    LFS SHA-256 today). ``git_sha1`` is a git-blob SHA-1 when the provider is
    git-backed. Either may be null — verify records that as no upstream
    expectation, never a fabricated match.
    """

    path: str
    size: int | None
    sha256: str | None = None
    git_sha1: str | None = None


@dataclass
class Snapshot:
    """Immutable pin: revision id, file inventory, and JSON-safe metadata for the ledger."""

    source: SourceRef
    revision: str
    revision_ref: str
    files: list[FileSpec]
    metadata: dict
    parameters: dict | None = None
    pipeline_tag: str | None = None
    license_id: str | None = None
    last_modified: str | None = field(default=None)


class SourceProvider(ABC):
    """One acquisition backend."""

    name: str
    label: str
    aliases: tuple[str, ...] = ()
    url_hosts: tuple[str, ...] = ()
    default_revision: str = "main"

    @abstractmethod
    def parse(self, locator: str, *, from_url: bool = False) -> SourceRef:
        """Parse a provider-native locator (no scheme) into a SourceRef."""

    def parse_url(self, url: str) -> SourceRef:
        parsed = urlparse(url)
        return self.parse(parsed.path.lstrip("/"), from_url=True)

    @abstractmethod
    def pin(
        self,
        source: SourceRef,
        revision: str | None,
        *,
        require_access: bool = False,
    ) -> Snapshot:
        """Resolve a moving ref to an immutable snapshot. Does not download payload bytes."""

    @abstractmethod
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
        """Fetch one file into ``payload_dir / relative``. Provider owns transport."""

    @contextmanager
    def transfer_session(
        self,
        payload_dir: Path,
        *,
        max_rate: int | None = None,
        on_retry=None,
    ) -> Iterator[None]:
        """Wrap a transfer run (resume semantics, provider caches). Default is a no-op.

        ``max_rate`` is the operator's bytes-per-second cap when one is set;
        a provider may tune its transport (chunk sizes) to pace smoothly.
        ``on_retry()`` is for a transport that retries on its own before
        failing a ``download_file``: calling it per attempt lets the panel
        show ``retrying`` instead of a silent stall.
        """
        yield

    def transient_network_error(self, exc: BaseException) -> str | None:
        """Classify a ``download_file`` failure worth waiting out.

        Returns a short reason (``"DNS lookup failed"``, ``"connection
        reset"``) when the network went away and the same call would succeed
        once it is back; ``None`` for everything else, which propagates.
        Providers extend this for their transport library's own exceptions.
        """
        return describe_network_error(exc)

    def progress_wrapper(self, counter, meter=None):
        """Optional tqdm subclass that reports network bytes onto ``counter``.

        ``meter`` is a ``TransferMeter`` when the CLI is drawing archive-level
        progress; wrappers should feed it and hide the default per-file bar.
        """
        return None

    def variants(self, source: SourceRef, progress) -> dict | None:
        """``estimate --variants`` payload, or None when the provider has no such listing."""
        return None

    def lineage(self, source: SourceRef, metadata: dict) -> dict:
        """The manifest ``lineage`` section: parents as upstream declares them
        and a best-effort snapshot of descendants at archive time. Unknown
        stays null; a query cap is recorded, never silently applied."""
        return {
            "parents": None,
            "descendants": None,
            "successors": None,
            "related": None,
            "as_of": None,
            "query_limit": None,
        }

    def access_record(self, metadata: dict) -> dict:
        gated = metadata.get("gated") or False
        return {"gated": gated, "notes": None}

    def downloader_versions(self) -> dict:
        """Extra version keys recorded on ``source.downloader`` (client libraries)."""
        return {}

    def partial_bytes(self, payload_dir: Path, expected: dict) -> int:
        """Best-effort size of an in-progress partial for ``expected`` (0 if unknown)."""
        return 0

    def read_bytes(
        self,
        source: SourceRef,
        revision: str,
        relative: str,
        start: int,
        length: int,
    ) -> bytes:
        """Read ``length`` bytes of one payload file at offset ``start``.

        A bounded range read for classification-time header inspection —
        never a whole-file download. Returns exactly the requested range,
        shorter only when the file ends inside it. Raises ``SourceError``
        when the provider has no range transport (this default) or the
        read fails; callers degrade — record the reason, treat the file
        as unreadable — and never crash.
        """
        raise SourceError(
            f"error: {self.label} does not support remote byte-range reads"
        )

    def access_denied_message(self, source: SourceRef, *, partial: bool = False) -> str:
        closing = (
            "The partial archive was kept and resumes if access returns."
            if partial
            else "Nothing was archived."
        )
        return (
            f"error: {source.artifact_type} {source.locator} requires authorization "
            f"on {self.label} and this account has not been granted access.\n{closing}"
        )

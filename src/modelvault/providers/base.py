"""Provider-neutral acquisition types.

A source provider is one hosting service that can pin a revision, list files,
and fetch bytes. Hugging Face is the first implementation; adding another is
a new class in this package plus one line in ``sources._ensure_providers``.
The public CLI (``modelvault archive <source>``) does not change.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator
from urllib.parse import urlparse


class SourceError(Exception):
    """Acquisition failed. The message is CLI-ready (includes the ``error:`` prefix)."""


class SourceGatedError(SourceError):
    """The source requires authorization this account does not have."""


class SourceNotFoundError(SourceError):
    """The locator does not exist, or is private and unauthenticated."""


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
    def transfer_session(self, payload_dir: Path) -> Iterator[None]:
        """Wrap a transfer run (resume semantics, provider caches). Default is a no-op."""
        yield

    def progress_wrapper(self, counter):
        """Optional tqdm subclass that reports network bytes onto ``counter``."""
        return None

    def variants(self, source: SourceRef, progress) -> dict | None:
        """``estimate --variants`` payload, or None when the provider has no such listing."""
        return None

    def relationships(self, source: SourceRef, metadata: dict) -> dict:
        """Best-effort ecosystem snapshot for the manifest. Unknown stays null."""
        return {
            "ecosystem_snapshot_as_of": None,
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

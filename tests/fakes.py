"""In-process acquisition provider for hermetic integration tests.

Registering this exercises the SourceProvider plugin boundary: archive,
estimate, and transfer talk to it the same way they talk to Hugging Face,
with no network.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from darsay.providers.base import (
    FileSpec,
    Snapshot,
    SourceGatedError,
    SourceNotFoundError,
    SourceProvider,
    SourceRef,
)


@dataclass
class PinnedRepo:
    files: dict[str, bytes]
    revision: str = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    revision_ref: str = "main"
    metadata: dict = field(default_factory=dict)
    parameters: dict | None = None
    pipeline_tag: str | None = "text-generation"
    license_id: str | None = "apache-2.0"
    last_modified: str | None = "2026-01-01T00:00:00+00:00"
    gated: bool = False
    missing: bool = False
    access_denied: bool = False


class TestProvider(SourceProvider):
    """Serves bytes from an in-memory catalog keyed by locator + revision ref."""

    name = "test"
    label = "Test Source"
    aliases = ("fake",)
    url_hosts = ("test.invalid",)
    default_revision = "main"

    def __init__(self) -> None:
        self.repos: dict[tuple[str, str], PinnedRepo] = {}
        self.downloads: list[str] = []

    def add_repo(self, locator: str, files: dict[str, bytes], **kwargs) -> PinnedRepo:
        kwargs.setdefault(
            "metadata",
            {
                "card_data": {"license": kwargs.get("license_id", "apache-2.0")},
                "tags": [],
                "gated": kwargs.get("gated", False),
                "created_at": "2026-01-01T00:00:00+00:00",
                "last_modified": kwargs.get(
                    "last_modified", "2026-01-01T00:00:00+00:00"
                ),
                "downloads": 0,
                "likes": 0,
            },
        )
        repo = PinnedRepo(files=dict(files), **kwargs)
        self.repos[(locator, repo.revision_ref)] = repo
        self.repos[(locator, repo.revision)] = repo
        return repo

    def parse(self, locator: str, *, from_url: bool = False) -> SourceRef:
        s = locator.strip()
        if s.lower().startswith(("https://", "http://")):
            parsed = urlparse(s)
            s = parsed.path.lstrip("/")
            from_url = True
        s = s.split("?", 1)[0].split("#", 1)[0].strip("/")
        parts = [p for p in s.split("/") if p]
        artifact_type = "model"
        if parts and parts[0] == "datasets":
            artifact_type = "dataset"
            parts = parts[1:]
        if from_url and len(parts) > 2:
            parts = parts[:2]
        if len(parts) != 2:
            raise SystemExit(
                f"error: cannot parse test source {locator!r} — expected owner/name "
                "or datasets/owner/name"
            )
        repo_id = "/".join(parts)
        publisher, _, name = repo_id.partition("/")
        slug = repo_id.replace("/", "--").lower()
        bundle_name = (
            f"{self.name}--datasets--{slug}"
            if artifact_type == "dataset"
            else f"{self.name}--{slug}"
        )
        path = f"datasets/{repo_id}" if artifact_type == "dataset" else repo_id
        return SourceRef(
            provider=self.name,
            artifact_type=artifact_type,
            locator=repo_id,
            canonical=f"{self.name}:{path}",
            url=f"https://test.invalid/{path}",
            bundle_name=bundle_name,
            publisher=publisher,
            name=name,
        )

    def _lookup(self, source: SourceRef, revision: str | None) -> PinnedRepo:
        requested = revision or self.default_revision
        repo = self.repos.get((source.locator, requested))
        if repo is None:
            # Prefix match so a 12-char bundle directory name (or a unique
            # catalogued revision of this locator) still pins, as Hub would.
            for (locator, _ref), candidate in list(self.repos.items()):
                if locator != source.locator:
                    continue
                if (
                    candidate.revision.startswith(requested)
                    or candidate.revision_ref == requested
                    or requested == self.default_revision
                ):
                    repo = candidate
                    break
        if repo is None or repo.missing:
            raise SourceNotFoundError(
                f"error: {source.artifact_type} {source.locator!r} not found on {self.label}."
            )
        return repo

    def pin(
        self,
        source: SourceRef,
        revision: str | None,
        *,
        require_access: bool = False,
    ) -> Snapshot:
        repo = self._lookup(source, revision)
        if repo.access_denied or (repo.gated and require_access):
            raise SourceGatedError(self.access_denied_message(source))
        files = [
            FileSpec(
                path=path,
                size=len(data),
                sha256=hashlib.sha256(data).hexdigest(),
            )
            for path, data in sorted(repo.files.items())
        ]
        return Snapshot(
            source=source,
            revision=repo.revision,
            revision_ref=revision or repo.revision_ref,
            files=files,
            metadata=dict(repo.metadata),
            parameters=repo.parameters,
            pipeline_tag=repo.pipeline_tag,
            license_id=repo.license_id,
            last_modified=repo.last_modified,
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
        repo = self._lookup(source, revision)
        if relative not in repo.files:
            raise FileNotFoundError(relative)
        dest = payload_dir.joinpath(*Path(relative).parts)
        if dest.exists() and not force:
            return
        data = repo.files[relative]
        dest.parent.mkdir(parents=True, exist_ok=True)
        if tqdm_class is not None:
            bar = tqdm_class(total=len(data), desc=relative)
            updater = getattr(bar, "update_transfer", None) or getattr(
                bar, "update", None
            )
            if updater is not None:
                updater(len(data))
            closer = getattr(bar, "close", None)
            if closer is not None:
                closer()
        dest.write_bytes(data)
        self.downloads.append(relative)

    def progress_wrapper(self, counter):
        class _Bar:
            def __init__(self, *args, **kwargs):
                pass

            def update(self, n=1):
                counter.add(n)

            def update_transfer(self, amount=1):
                counter.add(amount)

            def close(self):
                return None

        return _Bar

    def relationships(self, source: SourceRef, metadata: dict) -> dict:
        return {
            "base_models": None,
            "base_model": None,
            "base_model_relation": None,
            "finetuned_from": None,
            "training_datasets": None,
            "quantized_versions": [],
            "gguf_repos": None,
            "finetunes_count": 0,
            "adapters_count": 0,
            "related_variants": None,
            "successors": None,
            "ecosystem_snapshot_as_of": "2026-01-01T00:00:00+00:00",
            "query_limit": 100,
        }

    def variants(self, source: SourceRef, progress) -> dict | None:
        if source.artifact_type != "model":
            return None
        return {
            "as_of": "2026-01-01T00:00:00+00:00",
            "query_limit": 10,
            "detail_limit": 10,
            "count_listed": 0,
            "repos": [],
        }

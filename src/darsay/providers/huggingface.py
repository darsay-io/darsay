"""Hugging Face Hub acquisition backend.

The public CLI does not mention this module. ``darsay archive <source>``
dispatches here when the source is a Hub URL, a ``huggingface:`` / ``hf:``
ref, or the unprefixed Hub shorthand (``owner/name``, ``datasets/owner/name``).
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

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
)

# Hub lineage tags: `base_model:<repo_id>` declares a parent, and
# `base_model:<relation>:<repo_id>` labels the edge — this is what the Hub's
# "model tree" renders. Relations per the Hub card spec.
BASE_MODEL_RELATIONS = ("adapter", "finetune", "merge", "quantized")

VARIANT_QUERY_LIMIT = 100
VARIANT_DETAIL_LIMIT = 10
RELATED_QUERY_LIMIT = 100

# Hub responses that mean "try again shortly", not "this request is wrong".
_TRANSIENT_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})
# The Hub client's own retry commentary. darsay reports outages itself, so
# these would only duplicate the panel's account.
_HUB_RETRY_CHATTER = ("Error while downloading from",)
# Under a rate cap, read in chunks worth about a quarter second so pacing
# sleeps are short and the running rate stays smooth; never below this.
_MIN_THROTTLED_CHUNK = 64 * 1024

_FORMAT_TAGS = (
    "gguf",
    "mlx",
    "awq",
    "gptq",
    "fp8",
    "4-bit",
    "8-bit",
    "exl2",
    "exl3",
    "onnx",
)
_FORMAT_NAME_HINTS = (
    "nvfp4",
    "fp8",
    "awq",
    "gptq",
    "int8",
    "int4",
    "mlx",
    "gguf",
    "exl3",
    "exl2",
)


def _json_value(value):
    """Return a JSON-safe copy of Hub metadata without inventing values."""
    if isinstance(value, datetime):
        return value.isoformat(timespec="seconds")
    if isinstance(value, dict):
        return {str(k): _json_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(v) for v in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _as_list(value) -> list | None:
    if value is None:
        return None
    if isinstance(value, str):
        return [value]
    return list(value) or None


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _license_from_tags(tags: list[str] | None) -> str | None:
    for tag in tags or []:
        if tag.startswith("license:"):
            return tag.split(":", 1)[1]
    return None


def _safetensors_params(info) -> dict | None:
    st = getattr(info, "safetensors", None)
    if st is None:
        return None
    by_dtype = dict(getattr(st, "parameters", None) or {})
    total = getattr(st, "total", None) or sum(by_dtype.values())
    if not total:
        return None
    dominant = max(by_dtype, key=by_dtype.get) if by_dtype else None
    return {"total": total, "by_dtype": by_dtype or None, "dominant_dtype": dominant}


def _variant_formats(model_id: str, tags: list[str] | None) -> list[str] | None:
    found = [t for t in _FORMAT_TAGS if t in (tags or [])]
    lowered = model_id.lower()
    for hint in _FORMAT_NAME_HINTS:
        if hint in lowered and hint not in found:
            found.append(hint)
    return found or None


def throttled_chunk_size(max_rate: int, default: int) -> int:
    """Read chunk for a capped transfer: about a quarter second of bytes."""
    return max(_MIN_THROTTLED_CHUNK, min(default, max_rate // 4))


class _RetryChatterFilter(logging.Filter):
    """Drop the Hub client's retry notices while darsay's transfer runs.

    Each dropped notice becomes an ``on_retry()`` call (the panel's
    ``note_retry``), so the retry shows as a panel state rather than as a
    log line or a silent stall.
    """

    def __init__(self, on_retry=None):
        super().__init__()
        self.on_retry = on_retry

    def filter(self, record: logging.LogRecord) -> bool:
        if not record.getMessage().startswith(_HUB_RETRY_CHATTER):
            return True
        if self.on_retry is not None:
            with suppress(Exception):
                self.on_retry()
        return False


def parse_base_model_tags(tags: list[str]) -> tuple[list[str], dict[str, str]]:
    """Parse `base_model[:<relation>]:<repo_id>` repo tags into
    (parent repo ids in tag order, {repo_id: relation})."""
    ids: list[str] = []
    relations: dict[str, str] = {}
    for tag in tags:
        if not tag.startswith("base_model:"):
            continue
        rest = tag[len("base_model:") :]
        for rel in BASE_MODEL_RELATIONS:
            if rest.startswith(rel + ":"):
                rest = rest[len(rel) + 1 :]
                if rest:
                    relations[rest] = rel
                break
        if rest and rest not in ids:
            ids.append(rest)
    return ids, relations


class HuggingFaceProvider(SourceProvider):
    name = "huggingface"
    label = "Hugging Face"
    aliases = ("hf",)
    url_hosts = ("huggingface.co", "hf.co")
    default_revision = "main"

    def parse(self, locator: str, *, from_url: bool = False) -> SourceRef:
        s = locator.strip()
        if s.lower().startswith(("https://", "http://")):
            parsed = urlparse(s)
            host = parsed.netloc.lower().removeprefix("www.")
            if host not in self.url_hosts:
                raise SystemExit(f"error: not a {self.label} URL: {locator!r}")
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
                f"error: cannot parse source ref {locator!r} — expected "
                "owner/name, datasets/owner/name, huggingface:owner/name, "
                "huggingface:datasets/owner/name, or a huggingface.co URL of either"
            )
        repo_id = "/".join(parts)
        publisher, _, name = repo_id.partition("/")
        slug = repo_id.replace("/", "--").lower()
        bundle_name = f"datasets--{slug}" if artifact_type == "dataset" else slug
        path = f"datasets/{repo_id}" if artifact_type == "dataset" else repo_id
        return SourceRef(
            provider=self.name,
            artifact_type=artifact_type,
            locator=repo_id,
            canonical=f"{self.name}:{path}",
            url=f"https://huggingface.co/{path}",
            bundle_name=bundle_name,
            publisher=publisher,
            name=name,
        )

    def pin(
        self,
        source: SourceRef,
        revision: str | None,
        *,
        require_access: bool = False,
    ) -> Snapshot:
        from huggingface_hub import HfApi

        api = HfApi()
        pin_revision = revision or self.default_revision
        try:
            return self._pin_typed(api, source, pin_revision, require_access)
        except SourceNotFoundError as missing:
            # Unprefixed owner/name is model shorthand. Hub model and dataset
            # namespaces can collide, so an explicit datasets/ prefix is never
            # rewritten — but a model-shaped ref that exists only as a dataset
            # is the common paste and resolves to the dataset canonical.
            if source.artifact_type != "model":
                raise
            alt = self.parse(f"datasets/{source.locator}")
            try:
                return self._pin_typed(api, alt, pin_revision, require_access)
            except SourceNotFoundError:
                raise missing from None

    def _pin_typed(
        self,
        api,
        source: SourceRef,
        pin_revision: str,
        require_access: bool,
    ) -> Snapshot:
        from huggingface_hub.errors import GatedRepoError, RepositoryNotFoundError
        from huggingface_hub.utils import HfHubHTTPError

        try:
            if source.artifact_type == "dataset":
                info = api.dataset_info(
                    source.locator, revision=pin_revision, files_metadata=True
                )
            else:
                info = api.model_info(
                    source.locator, revision=pin_revision, files_metadata=True
                )
            # Cards are publicly readable for many gated repos. Confirm actual
            # read authorization before creating a ledger so an unauthorized
            # archive stays clean.
            if require_access and getattr(info, "gated", None):
                api.auth_check(source.locator, repo_type=source.artifact_type)
        except GatedRepoError:
            raise SourceGatedError(self.access_denied_message(source)) from None
        except RepositoryNotFoundError:
            raise SourceNotFoundError(
                f"error: {source.artifact_type} {source.locator!r} not found on "
                f"{self.label} — it may be private (authenticate with "
                f"`hf auth login`), renamed, or removed. Nothing was archived."
            ) from None
        except Exception as exc:
            reason = self.transient_network_error(exc)
            if reason is not None:
                raise SourceError(
                    f"error: cannot reach {self.label} to resolve {source.canonical} "
                    f"@ {pin_revision} — {reason}. Check the connection and re-run."
                ) from exc
            if isinstance(exc, (HfHubHTTPError, OSError)):
                raise SourceError(
                    f"error: cannot resolve {source.canonical} @ {pin_revision}: {exc}"
                ) from exc
            raise
        return self._snapshot(source, pin_revision, info)

    def transient_network_error(self, exc: BaseException) -> str | None:
        reason = describe_network_error(exc)
        if reason is not None:
            return reason
        try:
            import httpx
            from huggingface_hub.utils import HfHubHTTPError
        except ImportError:  # pragma: no cover - core dependency
            return None
        for node in iter_causes(exc):
            if isinstance(node, httpx.ConnectTimeout):
                return "connection timed out"
            if isinstance(node, httpx.TimeoutException):
                return "timed out"
            if isinstance(node, httpx.ConnectError):
                return "connection failed"
            if isinstance(node, httpx.RemoteProtocolError):
                return "connection closed by the server"
            if isinstance(node, httpx.ProxyError):
                return "proxy unreachable"
            if isinstance(node, httpx.NetworkError):
                return "connection reset"
            if isinstance(node, HfHubHTTPError):
                status = getattr(getattr(node, "response", None), "status_code", None)
                if status in _TRANSIENT_STATUSES:
                    return f"{self.label} responded {status}"
            if isinstance(node, OSError) and str(node).startswith(
                "Consistency check failed"
            ):
                # The stream ended early without a transport error; the
                # banked partial resumes with a Range request.
                return "transfer cut short"
        return None

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
        from huggingface_hub import hf_hub_download
        from huggingface_hub.errors import GatedRepoError

        try:
            hf_hub_download(
                repo_id=source.locator,
                filename=relative,
                revision=revision,
                local_dir=payload_dir,
                repo_type=source.artifact_type,
                force_download=force,
                tqdm_class=tqdm_class,
            )
        except GatedRepoError:
            raise SourceGatedError(
                self.access_denied_message(source, partial=True)
            ) from None

    @contextmanager
    def transfer_session(
        self,
        payload_dir: Path,
        *,
        max_rate: int | None = None,
        on_retry=None,
    ) -> Iterator[None]:
        """Restore safe same-bundle partial resume around ``hf_hub_download``.

        huggingface_hub 1.18 switched to process-unique temporary files and
        intentionally stopped preserving cross-call partials. darsay has a
        stronger per-bundle lock, so a stable local-dir incomplete file is safe
        here. The Hub client still owns metadata, HTTP Range requests, retries,
        Xet, and the final move; this wrapper only restores its former temp-file
        lifetime while the darsay lock is held.

        Under a rate cap the client's 10 MiB read chunk is shrunk so pacing
        sleeps stay short, and the client's own retry log lines are turned
        into ``on_retry`` calls: darsay owns the outage story on the terminal.
        The client's per-file free-space warning is not restored either —
        darsay checks headroom against its floor before a file begins.
        """
        import os

        import huggingface_hub.constants as hub_constants
        import huggingface_hub.file_download as file_download

        try:
            from huggingface_hub.utils._xet import abort_xet_session
        except ImportError:

            def abort_xet_session():
                return None

        original = file_download._download_to_tmp_and_move
        original_chunk = hub_constants.DOWNLOAD_CHUNK_SIZE
        chatter = _RetryChatterFilter(on_retry)
        hub_handlers = list(logging.getLogger("huggingface_hub").handlers)
        original_xet_cache = hub_constants.HF_XET_CACHE
        old_xet_cache_env = os.environ.get("HF_XET_CACHE")
        original_xet_disabled = hub_constants.HF_HUB_DISABLE_XET
        old_xet_disabled_env = os.environ.get("HF_HUB_DISABLE_XET")
        local_xet_cache = payload_dir / ".cache" / "huggingface" / "xet"

        abort_xet_session()
        os.environ["HF_XET_CACHE"] = str(local_xet_cache)
        hub_constants.HF_XET_CACHE = local_xet_cache
        os.environ["HF_HUB_DISABLE_XET"] = "1"
        hub_constants.HF_HUB_DISABLE_XET = True

        def resumable_download(
            incomplete_path,
            destination_path,
            url_to_download,
            headers,
            expected_size,
            filename,
            force_download,
            etag,
            xet_file_data,
            tqdm_class=None,
        ):
            if destination_path.exists() and not force_download:
                return
            if incomplete_path.exists() and force_download:
                incomplete_path.unlink(missing_ok=True)
            with incomplete_path.open("ab") as handle:
                resume_size = handle.tell()
                if xet_file_data is not None and file_download.is_xet_available():
                    file_download.xet_get(
                        incomplete_path=incomplete_path,
                        xet_file_data=xet_file_data,
                        headers=headers,
                        expected_size=expected_size,
                        displayed_filename=filename,
                        tqdm_class=tqdm_class,
                    )
                else:
                    file_download.http_get(
                        url_to_download,
                        handle,
                        resume_size=resume_size,
                        headers=headers,
                        expected_size=expected_size,
                        tqdm_class=tqdm_class,
                    )
            file_download._chmod_and_move(incomplete_path, destination_path)

        file_download._download_to_tmp_and_move = resumable_download
        if max_rate:
            hub_constants.DOWNLOAD_CHUNK_SIZE = throttled_chunk_size(
                max_rate, original_chunk
            )
        for handler in hub_handlers:
            handler.addFilter(chatter)
        try:
            yield
        finally:
            for handler in hub_handlers:
                handler.removeFilter(chatter)
            hub_constants.DOWNLOAD_CHUNK_SIZE = original_chunk
            file_download._download_to_tmp_and_move = original
            abort_xet_session()
            hub_constants.HF_XET_CACHE = original_xet_cache
            hub_constants.HF_HUB_DISABLE_XET = original_xet_disabled
            if old_xet_cache_env is None:
                os.environ.pop("HF_XET_CACHE", None)
            else:
                os.environ["HF_XET_CACHE"] = old_xet_cache_env
            if old_xet_disabled_env is None:
                os.environ.pop("HF_HUB_DISABLE_XET", None)
            else:
                os.environ["HF_HUB_DISABLE_XET"] = old_xet_disabled_env

    def progress_wrapper(self, counter, meter=None):
        from tqdm.auto import tqdm

        class TransferTqdm(tqdm):
            def __init__(self, *args, **kwargs):
                kwargs = dict(kwargs)
                name = str(kwargs.pop("name", "") or "")
                desc = str(kwargs.get("desc") or "")
                self._darsay_xet = name.startswith("huggingface_hub.xet_get") or (
                    "reconstructing file" in desc
                )
                self._meter = meter
                if meter is not None:
                    # Archive-level panel owns the screen; keep tqdm as a counter.
                    kwargs["disable"] = True
                super().__init__(*args, **kwargs)
                if meter is not None:
                    meter.attach_bar(self, desc)

            def update(self, n=1):
                result = super().update(n)
                if self.disable:
                    # A disabled tqdm never moves its counter; the panel's
                    # per-file line and a reconnect's resume point read it.
                    self.n = max(0, int(self.n or 0) + int(n or 0))
                if self._meter is not None:
                    self._meter.note()
                if not self._darsay_xet:
                    # A stop banked by the previous chunk's transfer callback
                    # can raise here; that chunk's bytes are already on disk.
                    counter.poll()
                return result

            def update_transfer(self, amount=1):
                counter.add(amount, defer_only=self._darsay_xet)
                if self._meter is not None:
                    self._meter.note()
                if self._darsay_xet and counter.pending_stop is not None:
                    from huggingface_hub.utils._xet import abort_xet_session

                    abort_xet_session()

            def set_transfer_postfix_str(self, *args, **kwargs):
                return None

            def close(self):
                if self._meter is not None:
                    self._meter.detach_bar(self)
                return super().close()

        return TransferTqdm

    def variants(self, source: SourceRef, progress) -> dict | None:
        if source.artifact_type != "model":
            return None
        from huggingface_hub import HfApi
        from huggingface_hub.utils import HfHubHTTPError

        api = HfApi()
        listed = list(
            api.list_models(
                filter=f"base_model:quantized:{source.locator}",
                limit=VARIANT_QUERY_LIMIT,
            )
        )
        listed.sort(key=lambda m: -(m.downloads or 0))
        rows = [
            {
                "repo_id": m.id,
                "downloads": m.downloads,
                "formats": _variant_formats(m.id, m.tags),
                "total_size_bytes": None,
            }
            for m in listed
        ]
        detailed = rows[:VARIANT_DETAIL_LIMIT]
        if detailed:
            progress(
                f"Sizing top {len(detailed)} of {len(rows)} quantized variants ..."
            )
        for row in detailed:
            try:
                vi = api.model_info(row["repo_id"], files_metadata=True)
                row["total_size_bytes"] = sum(s.size or 0 for s in vi.siblings or [])
            except (HfHubHTTPError, OSError):
                pass
        return {
            "as_of": _utc_now(),
            "query_limit": VARIANT_QUERY_LIMIT,
            "detail_limit": VARIANT_DETAIL_LIMIT,
            "count_listed": len(rows),
            "repos": rows,
        }

    def relationships(self, source: SourceRef, metadata: dict) -> dict:
        from huggingface_hub import HfApi

        api = HfApi()
        card = metadata.get("card_data") or {}
        tags = list(metadata.get("tags") or [])
        if source.artifact_type == "dataset":
            related = {
                "as_of": _utc_now(),
                "query_limit": RELATED_QUERY_LIMIT,
                "models_trained_on": None,
            }
            try:
                models = list(
                    api.list_models(
                        filter=f"dataset:{source.locator}", limit=RELATED_QUERY_LIMIT
                    )
                )
                related["models_trained_on"] = sorted(m.id for m in models)
            except Exception:
                pass
            return {
                "source_datasets": _as_list(card.get("source_datasets")),
                "models_trained_on": related["models_trained_on"],
                "ecosystem_snapshot_as_of": related["as_of"],
                "query_limit": related["query_limit"],
            }

        related = {
            "as_of": _utc_now(),
            "query_limit": RELATED_QUERY_LIMIT,
            "quantized_versions": None,
            "gguf_repos": None,
            "finetunes": None,
            "adapters": None,
        }
        kinds = {
            "quantized": "quantized_versions",
            "finetune": "finetunes",
            "adapter": "adapters",
        }
        for kind, key in kinds.items():
            try:
                models = list(
                    api.list_models(
                        filter=f"base_model:{kind}:{source.locator}",
                        limit=RELATED_QUERY_LIMIT,
                    )
                )
                related[key] = sorted(m.id for m in models)
            except Exception:
                pass
        if related["quantized_versions"]:
            ggufs = [m for m in related["quantized_versions"] if "gguf" in m.lower()]
            related["gguf_repos"] = ggufs or None

        base_models = [
            b for b in (_as_list(card.get("base_model")) or []) if isinstance(b, str)
        ]
        tag_bases, tag_relations = parse_base_model_tags(tags)
        for b in tag_bases:
            if b not in base_models:
                base_models.append(b)
        relation = card.get("base_model_relation")
        if not isinstance(relation, str):
            distinct = sorted(set(tag_relations.values()))
            relation = distinct[0] if len(distinct) == 1 else None
        primary_base = base_models[0] if base_models else None
        return {
            "base_models": base_models or None,
            "base_model": primary_base,
            "base_model_relation": relation,
            "finetuned_from": primary_base if relation == "finetune" else None,
            "training_datasets": _as_list(card.get("datasets")),
            "quantized_versions": related["quantized_versions"],
            "gguf_repos": related["gguf_repos"],
            "finetunes_count": len(related["finetunes"])
            if related["finetunes"] is not None
            else None,
            "adapters_count": len(related["adapters"])
            if related["adapters"] is not None
            else None,
            "related_variants": None,
            "successors": None,
            "ecosystem_snapshot_as_of": related["as_of"],
            "query_limit": related["query_limit"],
        }

    def access_record(self, metadata: dict) -> dict:
        gated = metadata.get("gated") or False
        notes = None
        if gated:
            notes = (
                f"Upstream repo is gated (mode: {gated}). Download required accepting "
                "the author's access agreement, which lives in Hub repo settings and "
                "is NOT part of the archived snapshot; re-fetching from upstream "
                "requires an account that has accepted it."
            )
        return {"gated": gated, "notes": notes}

    def downloader_versions(self) -> dict:
        import huggingface_hub

        return {"huggingface_hub": huggingface_hub.__version__}

    def partial_bytes(self, payload_dir: Path, expected: dict) -> int:
        """Best-effort byte count for Hub local-dir incomplete files.

        Used by transfer plan / ``darsay list``, so computing the path
        must not create Hub bookkeeping directories as
        ``get_local_download_paths`` does.
        """
        from pathlib import PurePosixPath

        etag = expected.get("lfs_sha256") or expected.get("git_sha1")
        if not etag:
            return 0
        download_root = payload_dir / ".cache" / "huggingface" / "download"
        if not download_root.is_dir():
            return 0
        try:
            from huggingface_hub._local_folder import _short_hash

            relative = PurePosixPath(expected["path"])
            metadata_path = download_root.joinpath(*relative.parts).with_name(
                f"{relative.name}.metadata"
            )
            path = (
                metadata_path.parent
                / f"{_short_hash(metadata_path.name)}.{etag}.incomplete"
            )
            return path.stat().st_size if path.is_file() else 0
        except ImportError:
            matches = list(download_root.rglob(f"*.{etag}.incomplete"))
            return matches[0].stat().st_size if len(matches) == 1 else 0
        except (OSError, ValueError):
            return 0

    def read_bytes(
        self,
        source: SourceRef,
        revision: str,
        relative: str,
        start: int,
        length: int,
    ) -> bytes:
        """Range request against the resolve URL, via the Hub client's session.

        Auth headers, retries, and CDN redirects come from the client. A
        server that answers 200 to a mid-file Range is refused rather than
        silently returning wrong-offset bytes.
        """
        from huggingface_hub import hf_hub_url
        from huggingface_hub.utils import build_hf_headers, get_session

        if length <= 0:
            return b""
        url = hf_hub_url(
            source.locator,
            relative,
            repo_type=source.artifact_type,
            revision=revision,
        )
        headers = build_hf_headers()
        headers["Range"] = f"bytes={start}-{start + length - 1}"
        try:
            with get_session().stream(
                "GET", url, headers=headers, follow_redirects=True
            ) as response:
                if response.status_code == 416:
                    # Range starts at or past EOF.
                    return b""
                response.raise_for_status()
                if response.status_code != 206 and start:
                    raise SourceError(
                        f"error: {self.label} ignored a Range request for "
                        f"{relative} — cannot read {source.locator} remotely"
                    )
                chunks: list[bytes] = []
                got = 0
                for chunk in response.iter_bytes():
                    take = chunk[: length - got]
                    chunks.append(take)
                    got += len(take)
                    if got >= length:
                        break
                return b"".join(chunks)
        except SourceError:
            raise
        except Exception as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if status in (401, 403):
                raise SourceGatedError(
                    f"error: {relative} on {self.label} requires authorization "
                    f"to read (HTTP {status})."
                ) from exc
            reason = self.transient_network_error(exc) or str(exc)
            raise SourceError(
                f"error: cannot read bytes {start}-{start + length - 1} of "
                f"{relative} from {source.canonical}: {reason}"
            ) from exc

    def access_denied_message(self, source: SourceRef, *, partial: bool = False) -> str:
        closing = (
            "The partial archive was kept and resumes if access returns."
            if partial
            else "Nothing was archived."
        )
        return (
            f"error: {source.artifact_type} {source.locator} is gated on {self.label} "
            "and this account has not been granted access. The gate is enforced "
            "server-side; darsay does not bypass it.\n"
            f"Visit {source.url} to review and accept the author's terms, "
            "authenticate with `hf auth login`, then re-run.\n"
            f"{closing}"
        )

    def _snapshot(self, source: SourceRef, revision_ref: str, info) -> Snapshot:
        files = []
        for sibling in info.siblings or []:
            files.append(
                FileSpec(
                    path=sibling.rfilename,
                    size=sibling.size,
                    sha256=sibling.lfs.sha256 if sibling.lfs else None,
                    git_sha1=sibling.blob_id if not sibling.lfs else None,
                )
            )
        files.sort(key=lambda item: item.path)
        card = info.card_data.to_dict() if info.card_data else {}
        last_modified = (
            info.last_modified.isoformat(timespec="seconds")
            if info.last_modified
            else None
        )
        return Snapshot(
            source=source,
            revision=info.sha,
            revision_ref=revision_ref,
            files=files,
            metadata={
                "card_data": _json_value(card),
                "tags": list(info.tags or []),
                "gated": getattr(info, "gated", None) or False,
                "created_at": (
                    info.created_at.isoformat(timespec="seconds")
                    if info.created_at
                    else None
                ),
                "last_modified": last_modified,
                "downloads": getattr(info, "downloads", None),
                "likes": getattr(info, "likes", None),
            },
            parameters=_safetensors_params(info),
            pipeline_tag=getattr(info, "pipeline_tag", None),
            license_id=_license_from_tags(getattr(info, "tags", None)),
            last_modified=last_modified,
        )

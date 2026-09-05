"""Source addresses and the acquisition-provider registry.

The public archive/estimate API takes a source ref, not a Hub repo id:

    huggingface:Qwen/Qwen3-0.6B
    huggingface:datasets/owner/name
    hf:Qwen/Qwen3-0.6B
    https://huggingface.co/Qwen/Qwen3-0.6B
    github:owner/repo
    gh:owner/repo
    https://github.com/owner/repo

Unprefixed ``owner/name`` and ``datasets/owner/name`` remain Hugging Face
shorthand so existing commands keep working. They are convenience, not the
canonical form — a new provider is ``<name>:<locator>`` (or that provider's
URL) and does not change this grammar.

New backends: implement ``SourceProvider`` in ``providers/`` and register it
in ``_ensure_providers``. Mirror of ``ARTIFACT_TYPES`` / ``ENGINES``.
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

from .providers.base import (
    FileSpec,
    Snapshot,
    SourceError,
    SourceGatedError,
    SourceNotFoundError,
    SourceProvider,
    SourceRef,
)

__all__ = [
    "FileSpec",
    "Snapshot",
    "SourceError",
    "SourceGatedError",
    "SourceNotFoundError",
    "SourceProvider",
    "SourceRef",
    "get_provider",
    "parse_source",
    "provider_names",
    "source_from_ledger",
]

_SCHEME = re.compile(r"^([a-z][a-z0-9+.-]*):(?://)?(.*)$", re.IGNORECASE)

_PROVIDERS: dict[str, SourceProvider] = {}
_HOSTS: dict[str, str] = {}


def register_provider(provider: SourceProvider) -> None:
    """Register an acquisition backend. Called from ``_ensure_providers``."""
    _PROVIDERS[provider.name] = provider
    for alias in provider.aliases:
        _PROVIDERS[alias] = provider
    for host in provider.url_hosts:
        _HOSTS[host.lower()] = provider.name


def _ensure_providers() -> None:
    if _PROVIDERS:
        return
    from .providers.github import GitHubProvider
    from .providers.huggingface import HuggingFaceProvider

    register_provider(HuggingFaceProvider())
    register_provider(GitHubProvider())


def get_provider(name: str) -> SourceProvider:
    _ensure_providers()
    try:
        return _PROVIDERS[name]
    except KeyError:
        known = ", ".join(provider_names())
        raise SystemExit(
            f"error: unknown source provider {name!r}. Known providers: {known}"
        ) from None


def provider_names() -> list[str]:
    _ensure_providers()
    return sorted({p.name for p in _PROVIDERS.values()})


def parse_source(ref: str) -> SourceRef:
    """Parse a source ref into a provider-qualified SourceRef.

    Accepts provider-qualified refs, provider URLs, and the Hugging Face
    unprefixed shorthand. Unknown ``scheme:`` prefixes error rather than
    being silently treated as Hugging Face locators.
    """
    _ensure_providers()
    s = (ref or "").strip()
    if not s:
        raise SystemExit("error: empty source ref")

    lowered = s.lower()
    if lowered.startswith("https://") or lowered.startswith("http://"):
        host = urlparse(s).netloc.lower().split("@")[-1]
        if host.startswith("www."):
            host = host[4:]
        name = _HOSTS.get(host)
        if name is None:
            known = ", ".join(sorted(_HOSTS)) or "(none)"
            raise SystemExit(
                f"error: no source provider for host {host!r} (known hosts: {known})"
            )
        return get_provider(name).parse_url(s)

    match = _SCHEME.match(s)
    if match:
        scheme = match.group(1).lower()
        locator = match.group(2)
        if scheme in _PROVIDERS:
            return get_provider(scheme).parse(locator)
        known = ", ".join(provider_names())
        raise SystemExit(
            f"error: unknown source provider {scheme!r}. Known providers: {known}. "
            "Use <provider>:<locator> or a provider URL."
        )

    return get_provider("huggingface").parse(s)


def source_from_ledger(ledger: dict) -> SourceRef:
    """Rebuild a SourceRef from a transfer ledger."""
    return parse_source(ledger["address"])

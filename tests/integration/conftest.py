"""Integration-layer fixtures: a disposable TestProvider on the source registry."""

from __future__ import annotations

import pytest

from tests.conftest import silent
from tests.fakes import TestProvider


@pytest.fixture
def test_provider():
    """Register a fresh TestProvider and restore the registry afterwards."""
    from darsay import sources

    sources._ensure_providers()
    providers = dict(sources._PROVIDERS)
    hosts = dict(sources._HOSTS)
    provider = TestProvider()
    sources.register_provider(provider)
    try:
        yield provider
    finally:
        sources._PROVIDERS.clear()
        sources._PROVIDERS.update(providers)
        sources._HOSTS.clear()
        sources._HOSTS.update(hosts)


@pytest.fixture(autouse=True)
def _register_test_provider(test_provider):
    return test_provider


def archive_quiet(source, *, vault, **kwargs):
    from darsay.archiver import archive

    kwargs.setdefault("progress", silent)
    kwargs.setdefault("jobs", 1)
    return archive(source, vault=vault, **kwargs)

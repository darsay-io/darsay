"""Shared pytest hooks and fixtures.

Layering (see docs/TESTING.md):

* ``unit`` — no network, no registered test provider, tmp_path only when needed.
* ``integration`` — fake ``test:`` provider, real filesystem, no network.
* ``e2e`` — live Hugging Face Hub; skipped unless ``--run-e2e`` or
  ``MODELVAULT_E2E=1``.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

# Quiet default for archive/verify/export progress callbacks.
silent = lambda *args, **kwargs: None  # noqa: E731


def pytest_addoption(parser):
    parser.addoption(
        "--run-e2e",
        action="store_true",
        default=False,
        help="run live Hugging Face Hub tests (also enabled by MODELVAULT_E2E=1)",
    )


def pytest_configure(config):
    config.addinivalue_line("markers", "unit: isolated unit tests (no network)")
    config.addinivalue_line(
        "markers", "integration: multi-module tests with a fake provider (no network)"
    )
    config.addinivalue_line("markers", "e2e: live network tests against Hugging Face Hub")


def pytest_collection_modifyitems(config, items):
    run_e2e = config.getoption("--run-e2e") or os.environ.get("MODELVAULT_E2E") == "1"
    skip_e2e = pytest.mark.skip(reason="pass --run-e2e or set MODELVAULT_E2E=1")
    for item in items:
        path = Path(str(item.fspath)).as_posix()
        if "/tests/unit/" in path:
            item.add_marker(pytest.mark.unit)
        elif "/tests/integration/" in path:
            item.add_marker(pytest.mark.integration)
        elif "/tests/e2e/" in path:
            item.add_marker(pytest.mark.e2e)
        if "e2e" in item.keywords and not run_e2e:
            item.add_marker(skip_e2e)


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    path = tmp_path / "vault"
    path.mkdir()
    return path

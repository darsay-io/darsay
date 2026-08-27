from __future__ import annotations

import re
from pathlib import Path

from darsay import SCHEMA_VERSION, __version__
from darsay.export import MVB_FORMAT_VERSION
from darsay.hydrate import ENGINES

ROOT = Path(__file__).resolve().parents[2]


def test_version_is_single_sourced():
    """pyproject must derive the version, never restate it.

    A second literal is the one thing that can silently disagree with
    ``darsay.__version__``; the build and the tag both read this one.
    """
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert 'dynamic = ["version"]' in text
    assert 'attr = "darsay.__version__"' in text
    assert re.search(r'(?m)^version = "', text) is None


def test_version_is_semver():
    parts = __version__.split(".")
    assert len(parts) == 3
    assert all(p.isdigit() for p in parts)


def test_schema_version_is_semver():
    parts = SCHEMA_VERSION.split(".")
    assert len(parts) == 3
    assert all(p.isdigit() for p in parts)


def test_mvb_format_version_has_major():
    major, _, _rest = MVB_FORMAT_VERSION.partition(".")
    assert major.isdigit()


def test_runner_scripts_are_packaged():
    import darsay

    root = Path(darsay.__file__).parent / "runners"
    missing = [
        spec["runner"]
        for spec in ENGINES.values()
        if not (root / spec["runner"]).is_file()
    ]
    assert missing == []


def test_standalone_verifier_is_packaged():
    import darsay

    path = Path(darsay.__file__).parent / "standalone_verify.py"
    assert path.is_file()
    assert path.read_bytes().startswith(b"#!/usr/bin/env python3")


def test_python_requires_matches_classifiers():
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert 'requires-python = ">=3.10"' in text
    for minor in ("3.10", "3.11", "3.12", "3.13", "3.14"):
        assert f"Programming Language :: Python :: {minor}" in text

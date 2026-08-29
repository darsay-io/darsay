"""The release gate's docs checks, run in CI as well as at release time.

``scripts/release.py`` is stdlib-only and not a package; load it by path.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def release():
    spec = importlib.util.spec_from_file_location(
        "release_script", ROOT / "scripts" / "release.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_docs_flags_match_the_cli(release):
    """No user doc describes an unshipped flag; every archive flag is mentioned."""
    release.check_docs_flags()


def test_docs_version_table_tracks_source_literals(release):
    """Between releases the landing table must already agree with the source."""
    from darsay import SCHEMA_VERSION, __version__
    from darsay.catalog import CATALOG_SCHEMA_VERSION
    from darsay.export import MVB_FORMAT_VERSION

    release.check_docs_table()
    text = (ROOT / "docs" / "README.md").read_text(encoding="utf-8")
    expected = {
        "Tool": __version__,
        "Manifest schema": SCHEMA_VERSION,
        "Catalog schema": CATALOG_SCHEMA_VERSION,
        "MVB format": MVB_FORMAT_VERSION,
    }
    for label, value in expected.items():
        assert release.docs_row(label).search(text).group(2) == value


def test_flag_token_scoping(release):
    """A flag belongs to the last program named before it on the line."""
    line = "rsync's `--link-dest`, then `darsay archive --max-gb 10`"
    owners = [
        release.PROGRAM.findall(line[: m.start()])[-1]
        for m in release.FLAG_TOKEN.finditer(line)
    ]
    assert owners == ["rsync", "darsay"]
    # Bundle and repo names carry `--` mid-word and are not flags.
    assert release.FLAG_TOKEN.findall("sshleifer--tiny-gpt2 datasets--a--b") == []


def test_docs_flag_checks_complain(release, monkeypatch, tmp_path):
    """Both directions refuse with the offending flag named."""
    docs = {
        "README.md": "darsay archive x --max-throttle 5M\n",
        "docs/GETTING-STARTED.md": "",
        "docs/CONCEPTS.md": "",
        "docs/INCREMENTAL.md": "",
        "examples/README.md": "",
    }
    for rel, text in docs.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    monkeypatch.setattr(release, "ROOT", tmp_path)
    with pytest.raises(
        SystemExit, match=r"does not ship:\s+--max-throttle  README.md:1"
    ):
        release.check_docs_flags()
    (tmp_path / "README.md").write_text("darsay archive x --max-gb 1\n")
    with pytest.raises(SystemExit, match=r"no user doc mentions: .*--rehash"):
        release.check_docs_flags()


PREAMBLE = (
    "# Changelog\n\n"
    "All notable changes to this project are documented in this file.\n\n"
)


def test_prepare_changelog_promotes_unreleased(release):
    text = (
        PREAMBLE + "## [Unreleased]\n\n### Added\n\n- a thing\n\n"
        "## [0.10.0] - 2026-08-28\n\n- old\n"
    )
    updated, log = release.prepare_changelog(text, "0.11.0", "2026-08-29")
    assert log == "## [0.11.0] - 2026-08-29 (from [Unreleased])"
    assert updated.startswith(
        PREAMBLE + "## [Unreleased]\n\n## [0.11.0] - 2026-08-29\n"
    )
    assert "### Added\n\n- a thing\n" in updated
    assert updated.count("## [Unreleased]") == 1
    assert "## [0.10.0] - 2026-08-28" in updated


def test_prepare_changelog_dates_version_heading_and_inserts_stub(release):
    text = (
        PREAMBLE + "## [0.11.0]\n\n### Added\n\n- a thing\n\n## [0.10.0] - 2026-08-28\n"
    )
    updated, log = release.prepare_changelog(text, "0.11.0", "2026-08-29")
    assert log == "## [0.11.0] - 2026-08-29"
    assert updated.startswith(
        PREAMBLE + "## [Unreleased]\n\n## [0.11.0] - 2026-08-29\n"
    )
    assert "- a thing" in updated


def test_prepare_changelog_keeps_existing_empty_unreleased(release):
    text = (
        PREAMBLE + "## [Unreleased]\n\n## [0.11.0]\n\n- a thing\n\n"
        "## [0.10.0] - 2026-08-28\n"
    )
    updated, log = release.prepare_changelog(text, "0.11.0", "2026-08-29")
    assert log == "## [0.11.0] - 2026-08-29"
    assert updated.count("## [Unreleased]") == 1
    assert "## [0.11.0] - 2026-08-29" in updated


def test_prepare_changelog_refuses_empty_and_ambiguous(release):
    with pytest.raises(SystemExit, match="no '## \\[Unreleased\\]'"):
        release.prepare_changelog(
            PREAMBLE + "## [0.10.0] - 2026-08-28\n", "0.11.0", "2026-08-29"
        )
    with pytest.raises(SystemExit, match="has no notes"):
        release.prepare_changelog(
            PREAMBLE + "## [0.11.0]\n\n### Added\n", "0.11.0", "2026-08-29"
        )
    with pytest.raises(SystemExit, match="move them to one section"):
        release.prepare_changelog(
            PREAMBLE + "## [Unreleased]\n\n- new\n\n## [0.11.0]\n\n- other\n",
            "0.11.0",
            "2026-08-29",
        )

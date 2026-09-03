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


def test_docs_links_resolve(release):
    """Every relative link in the user docs names a file that is here."""
    release.check_docs_links()


def test_docs_pages_are_all_classified(release):
    """Every page darsay.io publishes is flag-checked or exempt on purpose."""
    release.check_docs_pages_classified()
    listed = set(release.CLI_DOCS) | set(release.UNCHECKED_DOCS)
    assert listed == release.published_docs()
    assert not set(release.CLI_DOCS) & set(release.UNCHECKED_DOCS)


def test_a_new_docs_page_has_to_be_classified(release, monkeypatch, tmp_path):
    """The lists are total, so forgetting one is a refusal, not a silent gap.

    The whole point of listing rather than globbing: the flag checks read a
    page as "every darsay flag named here is live", which a page naming a
    flag to say it does not exist would fail on. That judgement is per page,
    so the release asks for it once, by name.
    """
    for rel in (
        "README.md",
        "examples/README.md",
        "docs/GETTING-STARTED.md",
        "docs/DESIGN.md",
    ):
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")
    monkeypatch.setattr(release, "ROOT", tmp_path)
    monkeypatch.setattr(
        release,
        "CLI_DOCS",
        ("README.md", "examples/README.md", "docs/GETTING-STARTED.md"),
    )
    monkeypatch.setattr(
        release, "UNCHECKED_DOCS", {"docs/DESIGN.md": "weighs flags not taken"}
    )
    release.check_docs_pages_classified()

    (tmp_path / "docs" / "SHELVES.md").write_text("# Shelves\n", encoding="utf-8")
    with pytest.raises(SystemExit, match=r"nobody classified:\s+docs/SHELVES\.md"):
        release.check_docs_pages_classified()

    monkeypatch.setitem(release.UNCHECKED_DOCS, "docs/SHELVES.md", "a proposal page")
    release.check_docs_pages_classified()

    # And the rot in the other direction: a page the lists still name.
    (tmp_path / "docs" / "SHELVES.md").unlink()
    with pytest.raises(SystemExit, match=r"but not here: docs/SHELVES\.md"):
        release.check_docs_pages_classified()


def test_a_docs_page_cannot_be_both_checked_and_exempt(release, monkeypatch):
    monkeypatch.setitem(release.UNCHECKED_DOCS, "docs/FAQ.md", "contradiction")
    with pytest.raises(SystemExit, match=r"CLI_DOCS and UNCHECKED_DOCS: docs/FAQ\.md"):
        release.check_docs_pages_classified()


def test_release_validation_suppresses_bytecode_caches(release):
    assert release.sys.dont_write_bytecode is True
    assert release.os.environ["PYTHONDONTWRITEBYTECODE"] == "1"


def test_docs_version_table_tracks_source_literals(release):
    """Between releases the landing table must already agree with the source."""
    from darsay import SCHEMA_VERSION, __version__
    from darsay.catalog import CATALOG_SCHEMA_VERSION
    from darsay.export import MVB_FORMAT_VERSION

    release.check_docs_versions_current()
    text = (ROOT / "docs" / "README.md").read_text(encoding="utf-8")
    expected = {
        "Tool": __version__,
        "Manifest schema": SCHEMA_VERSION,
        "Catalog schema": CATALOG_SCHEMA_VERSION,
        "MVB format": MVB_FORMAT_VERSION,
    }
    for label, value in expected.items():
        assert release.docs_row(label).search(text).group(2) == value


def test_prepared_release_check_is_read_only(release, monkeypatch, tmp_path):
    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text("prepared\n", encoding="utf-8")
    calls = []
    monkeypatch.setattr(release, "CHANGELOG", changelog)
    monkeypatch.setattr(release, "read_current_version", lambda: "1.2.3")
    monkeypatch.setattr(
        release, "check_prepared_changelog", lambda version: calls.append("changelog")
    )
    monkeypatch.setattr(
        release, "check_docs_versions_current", lambda: calls.append("docs")
    )
    monkeypatch.setattr(
        release, "check_docs_pages_classified", lambda: calls.append("pages")
    )
    monkeypatch.setattr(release, "check_docs_flags", lambda: calls.append("flags"))
    monkeypatch.setattr(release, "check_docs_links", lambda: calls.append("links"))
    monkeypatch.setattr(
        release,
        "run_gate",
        lambda version, skip_build: calls.append((version, skip_build)),
    )

    release.check_prepared_release("1.2.3", "2026-08-29", True)

    assert changelog.read_text(encoding="utf-8") == "prepared\n"
    assert calls == ["changelog", "docs", "pages", "flags", "links", ("1.2.3", True)]


def test_prepared_changelog_keeps_its_frozen_release_date(
    release, monkeypatch, tmp_path
):
    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text(
        "# Changelog\n\n## [Unreleased]\n\n## [1.2.3] - 2026-08-30\n\n- Shipped.\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(release, "CHANGELOG", changelog)

    release.check_prepared_changelog("1.2.3")


def test_prepared_release_check_refuses_wrong_version(release, monkeypatch):
    monkeypatch.setattr(release, "read_current_version", lambda: "1.2.2")
    with pytest.raises(SystemExit, match="source version is 1.2.2"):
        release.check_prepared_release("1.2.3", "2026-08-29", True)


def test_prepare_only_reuses_writers_without_owning_git(release, monkeypatch):
    calls = []
    monkeypatch.setattr(release, "read_current_version", lambda: "1.2.2")
    monkeypatch.setattr(
        release, "check_tooling", lambda skip_build: calls.append("tooling")
    )
    monkeypatch.setattr(
        release,
        "check_repo_state",
        lambda *args, **kwargs: pytest.fail("prepare-only must not inspect Git"),
    )
    monkeypatch.setattr(release, "check_changelog", lambda *args: "heading")
    monkeypatch.setattr(release, "check_docs_table", lambda: calls.append("docs"))
    monkeypatch.setattr(
        release, "check_docs_pages_classified", lambda: calls.append("pages")
    )
    monkeypatch.setattr(release, "check_docs_flags", lambda: calls.append("flags"))
    monkeypatch.setattr(release, "check_docs_links", lambda: calls.append("links"))
    monkeypatch.setattr(release, "write_version", lambda value: calls.append(value))
    monkeypatch.setattr(release, "write_docs_versions", lambda: [])
    monkeypatch.setattr(release, "write_changelog", lambda *args: "heading")
    monkeypatch.setattr(
        release,
        "run_gate",
        lambda version, skip_build: calls.append((version, skip_build)),
    )
    monkeypatch.setattr(
        release,
        "git",
        lambda *args: pytest.fail("prepare-only must not commit or tag"),
    )

    assert release.main(["1.2.3", "--prepare-only", "--skip-build"]) == 0
    assert calls == [
        "tooling",
        "docs",
        "pages",
        "flags",
        "links",
        "1.2.3",
        ("1.2.3", True),
    ]


def test_prepare_only_converges_when_source_is_already_prepared(release, monkeypatch):
    calls = []
    monkeypatch.setattr(release, "read_current_version", lambda: "1.2.3")
    monkeypatch.setattr(
        release,
        "check_tooling",
        lambda *args: pytest.fail("metadata-only must not require ambient tools"),
    )
    monkeypatch.setattr(
        release,
        "check_prepared_release",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )
    monkeypatch.setattr(
        release,
        "write_version",
        lambda *args: pytest.fail("converged preparation must not write"),
    )

    assert release.main(["1.2.3", "--prepare-only", "--metadata-only"]) == 0
    assert calls == [
        (("1.2.3", release.dt.date.today().isoformat(), False), {"metadata_only": True})
    ]


def test_metadata_only_prepare_needs_no_ambient_tooling(release, monkeypatch):
    calls = []
    monkeypatch.setattr(release, "read_current_version", lambda: "1.2.2")
    monkeypatch.setattr(
        release,
        "check_tooling",
        lambda *args: pytest.fail("metadata-only must not require ambient tools"),
    )
    monkeypatch.setattr(release, "check_changelog", lambda *args: "heading")
    monkeypatch.setattr(release, "check_docs_table", lambda: calls.append("docs"))
    monkeypatch.setattr(
        release, "check_docs_pages_classified", lambda: calls.append("pages")
    )
    monkeypatch.setattr(release, "check_docs_flags", lambda: calls.append("flags"))
    monkeypatch.setattr(release, "check_docs_links", lambda: calls.append("links"))
    monkeypatch.setattr(release, "write_version", lambda value: calls.append(value))
    monkeypatch.setattr(release, "write_docs_versions", lambda: [])
    monkeypatch.setattr(release, "write_changelog", lambda *args: "heading")
    monkeypatch.setattr(
        release,
        "run_gate",
        lambda *args: pytest.fail("metadata-only must not run the project gate"),
    )

    assert release.main(["1.2.3", "--prepare-only", "--metadata-only"]) == 0
    assert calls == ["docs", "pages", "flags", "links", "1.2.3"]


def test_metadata_only_refuses_to_weaken_native_release(release, capsys):
    with pytest.raises(SystemExit) as stopped:
        release.main(["1.2.3", "--metadata-only"])
    assert stopped.value.code == 2
    assert "--metadata-only requires" in capsys.readouterr().err


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
    # Built from CLI_DOCS itself: a page added to that list must not need an
    # edit here to keep this test honest.
    docs = dict.fromkeys(release.CLI_DOCS, "")
    docs["README.md"] = "darsay archive x --max-throttle 5M\n"
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


def test_docs_links_complain_about_what_is_missing(release, monkeypatch, tmp_path):
    """A link with nothing behind it is named, with the file and the line."""
    tree = {
        "README.md": "See [the docs](docs/GETTING-STARTED.md).\n",
        "CONTRIBUTING.md": "",
        "examples/README.md": "Back to [concepts](../docs/CONCEPTS.md).\n",
        "docs/GETTING-STARTED.md": (
            'Nav: <a href="CONCEPTS.md">Concepts</a>\n'
            "\nThen [the missing page](MISSING.md) and [an anchor](#here).\n"
        ),
        "docs/CONCEPTS.md": "",
    }
    for rel, text in tree.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    monkeypatch.setattr(release, "ROOT", tmp_path)

    with pytest.raises(SystemExit, match=r"MISSING\.md  docs/GETTING-STARTED\.md:3"):
        release.check_docs_links()

    # A new docs page is checked the moment it lands: nothing lists the pages.
    (tmp_path / "docs" / "GETTING-STARTED.md").write_text(
        "Then [the new page](NEW-PAGE.md).\n", encoding="utf-8"
    )
    with pytest.raises(SystemExit, match=r"NEW-PAGE\.md"):
        release.check_docs_links()
    (tmp_path / "docs" / "NEW-PAGE.md").write_text(
        "Out of bounds: [away](../../elsewhere.md)\n", encoding="utf-8"
    )
    with pytest.raises(SystemExit, match=r"leaves the repository"):
        release.check_docs_links()

    (tmp_path / "docs" / "NEW-PAGE.md").write_text(
        "Home: [README](../README.md)\n", encoding="utf-8"
    )
    release.check_docs_links()


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

from __future__ import annotations

import json
from pathlib import Path

import pytest

from darsay.cli import main
from tests.integration.conftest import archive_quiet
from tests.payloads import model_files


def test_two_vault_overlay_sharing(tmp_path, test_provider, capsys):
    test_provider.add_repo("acme/toy", model_files())
    empty = tmp_path / "empty"
    owned = tmp_path / "owned"
    empty.mkdir()
    owned.mkdir()
    fixture = tmp_path / "catalog.json"
    fixture.write_text(
        json.dumps({
            "catalog_schema_version": "1.0.0",
            "kind": "darsay.catalog",
            "id": "summer",
            "title": "Summer",
            "curator": "Alex",
            "created": "2026-01-01T00:00:00+00:00",
            "updated": "2026-01-01T00:00:00+00:00",
            "entries": [{
                "source": "test:acme/toy",
                "revision": None,
                "include": None,
                "desire": 9,
                "note": "the one",
                "added": "2026-01-01T00:00:00+00:00",
                "estimate": None,
            }],
        }),
        encoding="utf-8",
    )
    assert main(["--vault", str(empty), "list", str(fixture)]) == 0
    empty_out = capsys.readouterr().out
    assert "want" in empty_out
    assert "test:acme/toy" in empty_out

    archive_quiet("test:acme/toy", vault=owned)
    capsys.readouterr()
    assert main(["--vault", str(owned), "list", str(fixture)]) == 0
    owned_out = capsys.readouterr().out
    assert "have" in owned_out
    assert "test:acme/toy" in owned_out

    assert main(["--vault", str(empty), "list", str(fixture), "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["stats"]["want"] == 1
    assert data["stats"]["have"] == 0
    assert data["entries"][0]["status"] == "want"


def test_catalog_new_add_drop_list(vault, test_provider, capsys):
    test_provider.add_repo("acme/toy", model_files())
    assert main(["--vault", str(vault), "catalog", "new", "Summer", "--title", "Summer 2026"]) == 0
    out = capsys.readouterr().out
    assert "catalogs/summer" in out.replace("\\", "/")
    assert main(["--vault", str(vault), "catalog", "add", "SUMMER", "test:acme/toy", "--desire", "8"]) == 0
    added = capsys.readouterr().out
    assert "Added test:acme/toy" in added
    assert "no estimate yet" in added
    assert main(["--vault", str(vault), "list", "summer"]) == 0
    listed = capsys.readouterr().out
    assert "want" in listed
    assert "test:acme/toy" in listed
    archive_quiet("test:acme/toy", vault=vault)
    capsys.readouterr()
    assert main(["--vault", str(vault), "list", "summer"]) == 0
    after = capsys.readouterr().out
    assert "have" in after
    assert main(["--vault", str(vault), "catalog", "drop", "summer", "test:acme/toy"]) == 0
    assert "Dropped" in capsys.readouterr().out


def test_estimate_catalog_and_path_readonly(vault, tmp_path, test_provider, capsys):
    test_provider.add_repo("acme/toy", model_files())
    assert main(["--vault", str(vault), "catalog", "new", "summer"]) == 0
    capsys.readouterr()
    assert main(["--vault", str(vault), "catalog", "add", "summer", "test:acme/toy", "--desire", "5"]) == 0
    capsys.readouterr()
    assert main(["--vault", str(vault), "estimate", "summer"]) == 0
    out = capsys.readouterr().out
    assert "Updated" in out
    catalog = json.loads((vault / "catalogs" / "summer" / "catalog.json").read_text())
    digest = catalog["entries"][0]["estimate"]
    assert "payload_bytes" in digest
    assert "checked_path" not in digest
    assert "precision" not in digest

    friend = tmp_path / "friend" / "catalog.json"
    friend.parent.mkdir()
    friend.write_text((vault / "catalogs" / "summer" / "catalog.json").read_text())
    with pytest.raises(SystemExit, match="read-only"):
        main(["--vault", str(vault), "estimate", str(friend)])


def test_archive_next_resumes_non_main_partial(vault, test_provider, capsys):
    test_provider.add_repo(
        "acme/toy",
        model_files(),
        revision="bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        revision_ref="other",
    )
    assert (
        main(
            [
                "--vault", str(vault), "archive", "test:acme/toy",
                "--revision", "other", "--max-bytes", "1", "--jobs", "1",
            ]
        )
        == 10
    )
    capsys.readouterr()
    fixture = vault / "want.json"
    fixture.write_text(
        json.dumps({
            "catalog_schema_version": "1.0.0",
            "kind": "darsay.catalog",
            "id": "summer",
            "title": "summer",
            "created": "2026-01-01T00:00:00+00:00",
            "updated": "2026-01-01T00:00:00+00:00",
            "entries": [{
                "source": "test:acme/toy",
                "revision": None,
                "include": None,
                "desire": 9,
                "note": None,
                "added": "2026-01-01T00:00:00+00:00",
                "estimate": None,
            }],
        }),
        encoding="utf-8",
    )
    # --next on a partial should resume the matched pin (not pin main).
    rc = main(["--vault", str(vault), "archive", "--next", str(fixture), "--jobs", "1"])
    out = capsys.readouterr().out
    assert "Resuming from catalog" in out or rc in (0, 10)
    # Completing should register at the bbbb revision, not a new main pin.
    if rc == 10:
        rc = main(["--vault", str(vault), "archive", "--next", str(fixture), "--jobs", "1"])
        capsys.readouterr()
    assert rc == 0
    dirs = list((vault / "test--acme--toy").iterdir())
    assert any(d.name.startswith("bbbbbbbbbbbb") for d in dirs if d.is_dir())
    assert not (vault / "test--acme--toy" / "aaaaaaaaaaaa").exists()


def test_archive_next_errors(vault, test_provider, capsys):
    test_provider.add_repo("acme/toy", model_files())
    assert main(["--vault", str(vault), "catalog", "new", "summer"]) == 0
    capsys.readouterr()
    with pytest.raises(SystemExit, match="is a catalog, not a source"):
        main(["--vault", str(vault), "archive", "summer"])
    with pytest.raises(SystemExit, match="already applies"):
        main(["--vault", str(vault), "archive", "--next", "summer", "--include", "*Q4*"])
    assert main(["--vault", str(vault), "archive", "--next", "summer"]) == 0
    assert "nothing to archive" in capsys.readouterr().out


def test_catalog_adopt_and_regen(vault, tmp_path, capsys):
    other = tmp_path / "friend"
    other.mkdir()
    (other / "catalog.json").write_text(
        json.dumps({
            "catalog_schema_version": "1.0.0",
            "kind": "darsay.catalog",
            "id": "summer",
            "title": "Summer",
            "curator": "Alex",
            "created": "2026-01-01T00:00:00+00:00",
            "updated": "2026-01-01T00:00:00+00:00",
            "entries": [{
                "source": "huggingface:acme/toy",
                "revision": None,
                "include": None,
                "desire": 9,
                "note": "keep",
                "added": "2026-01-01T00:00:00+00:00",
                "estimate": None,
            }],
        }),
        encoding="utf-8",
    )
    assert main(["--vault", str(vault), "catalog", "new", "reading", "--curator", "Sam"]) == 0
    capsys.readouterr()
    curation = vault / "catalogs" / "reading" / "curation.md"
    original = curation.read_text()
    assert main(["--vault", str(vault), "catalog", "adopt", "reading", str(other)]) == 0
    out = capsys.readouterr().out
    assert "Adopted 1" in out
    assert curation.read_text() == original
    assert main(["--vault", str(vault), "catalog", "regen", "reading"]) == 0
    readme = (vault / "catalogs" / "reading" / "README.md").read_text()
    assert "huggingface:acme/toy" in readme
    assert str(vault) not in readme
    assert curation.read_text() == original


def test_list_next_and_ids(vault, test_provider, capsys):
    test_provider.add_repo("acme/toy", model_files())
    assert main(["--vault", str(vault), "catalog", "new", "summer"]) == 0
    capsys.readouterr()
    assert main(["--vault", str(vault), "catalog", "add", "summer", "test:acme/toy", "--desire", "7", "--include", "*config.json"]) == 0
    capsys.readouterr()
    assert main(["--vault", str(vault), "list", "summer", "--ids"]) == 0
    captured = capsys.readouterr()
    assert "test:acme/toy" in captured.out
    assert "subset or pinned" in captured.err
    assert main(["--vault", str(vault), "list", "summer", "--next"]) == 0
    assert capsys.readouterr().out.strip() == "test:acme/toy"
    assert main(["--vault", str(vault), "list", "--sort", "name"]) == 0
    capsys.readouterr()
    with pytest.raises(SystemExit, match="requires a catalog"):
        main(["--vault", str(vault), "list", "--sort", "desire"])

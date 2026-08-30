from __future__ import annotations

import json
from contextlib import suppress

import pytest

from darsay.cli import main
from tests.integration.conftest import archive_quiet
from tests.payloads import dataset_files, model_files


def test_two_vault_overlay_sharing(tmp_path, test_provider, capsys):
    test_provider.add_repo("acme/toy", model_files())
    empty = tmp_path / "empty"
    owned = tmp_path / "owned"
    empty.mkdir()
    owned.mkdir()
    fixture = tmp_path / "catalog.json"
    fixture.write_text(
        json.dumps(
            {
                "catalog_schema_version": "1.0.0",
                "kind": "darsay.catalog",
                "id": "summer",
                "title": "Summer",
                "curator": "Alex",
                "created": "2026-01-01T00:00:00+00:00",
                "updated": "2026-01-01T00:00:00+00:00",
                "entries": [
                    {
                        "source": "test:acme/toy",
                        "revision": None,
                        "include": None,
                        "desire": 9,
                        "note": "the one",
                        "added": "2026-01-01T00:00:00+00:00",
                        "estimate": None,
                    }
                ],
            }
        ),
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
    assert (
        main(
            [
                "--vault",
                str(vault),
                "catalog",
                "new",
                "Summer",
                "--title",
                "Summer 2026",
            ]
        )
        == 0
    )
    out = capsys.readouterr().out
    assert "catalogs/summer" in out.replace("\\", "/")
    assert (
        main(
            [
                "--vault",
                str(vault),
                "catalog",
                "add",
                "SUMMER",
                "test:acme/toy",
                "--desire",
                "8",
            ]
        )
        == 0
    )
    added = capsys.readouterr().out
    assert "Added test:acme/toy" in added
    assert "no estimate yet" in added
    assert main(["--vault", str(vault), "list", "summer"]) == 0
    listed = capsys.readouterr().out
    assert "want" in listed
    assert "test:acme/toy" in listed
    assert "DESIRE" in listed
    archive_quiet("test:acme/toy", vault=vault)
    capsys.readouterr()
    assert main(["--vault", str(vault), "list", "summer"]) == 0
    after = capsys.readouterr().out
    assert "have" in after
    assert (
        main(["--vault", str(vault), "catalog", "drop", "summer", "test:acme/toy"]) == 0
    )
    assert "Dropped" in capsys.readouterr().out


def test_estimate_catalog_and_path_readonly(vault, tmp_path, test_provider, capsys):
    test_provider.add_repo("acme/toy", model_files())
    assert main(["--vault", str(vault), "catalog", "new", "summer"]) == 0
    capsys.readouterr()
    assert (
        main(
            [
                "--vault",
                str(vault),
                "catalog",
                "add",
                "summer",
                "test:acme/toy",
                "--desire",
                "5",
            ]
        )
        == 0
    )
    capsys.readouterr()
    assert main(["--vault", str(vault), "estimate", "summer"]) == 0
    out = capsys.readouterr().out
    assert "Updated" in out
    catalog = json.loads((vault / "catalogs" / "summer" / "catalog.json").read_text())
    digest = catalog["entries"][0]["estimate"]
    assert "payload_bytes" in digest
    assert "checked_path" not in digest
    assert "precision" not in digest
    readme = (vault / "catalogs" / "summer" / "README.md").read_text()
    assert "(as of" in readme

    friend = tmp_path / "friend" / "catalog.json"
    friend.parent.mkdir()
    friend.write_text((vault / "catalogs" / "summer" / "catalog.json").read_text())
    with pytest.raises(SystemExit, match="read-only"):
        main(["--vault", str(vault), "estimate", str(friend)])


def test_estimate_catalog_rewrites_unprefixed_dataset_source(
    vault, test_provider, capsys
):
    test_provider.add_repo(
        "acme/reviews",
        dataset_files(),
        artifact_type="dataset",
        pipeline_tag=None,
        license_id="mit",
        metadata={"card_data": {"license": "mit"}, "tags": [], "gated": False},
    )
    assert main(["--vault", str(vault), "catalog", "new", "summer"]) == 0
    capsys.readouterr()
    assert (
        main(
            [
                "--vault",
                str(vault),
                "catalog",
                "add",
                "summer",
                "test:acme/reviews",
            ]
        )
        == 0
    )
    catalog = json.loads((vault / "catalogs" / "summer" / "catalog.json").read_text())
    assert catalog["entries"][0]["source"] == "test:acme/reviews"
    capsys.readouterr()
    assert main(["--vault", str(vault), "estimate", "summer"]) == 0
    catalog = json.loads((vault / "catalogs" / "summer" / "catalog.json").read_text())
    assert catalog["entries"][0]["source"] == "test:datasets/acme/reviews"
    assert catalog["entries"][0]["estimate"]["artifact_type"] == "dataset"


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
                "--vault",
                str(vault),
                "archive",
                "test:acme/toy",
                "--revision",
                "other",
                "--max-bytes",
                "1",
                "--jobs",
                "1",
            ]
        )
        == 10
    )
    capsys.readouterr()
    fixture = vault / "want.json"
    fixture.write_text(
        json.dumps(
            {
                "catalog_schema_version": "1.0.0",
                "kind": "darsay.catalog",
                "id": "summer",
                "title": "summer",
                "created": "2026-01-01T00:00:00+00:00",
                "updated": "2026-01-01T00:00:00+00:00",
                "entries": [
                    {
                        "source": "test:acme/toy",
                        "revision": None,
                        "include": None,
                        "desire": 9,
                        "note": None,
                        "added": "2026-01-01T00:00:00+00:00",
                        "estimate": None,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    # --next on a partial should resume the matched pin (not pin main).
    rc = main(["--vault", str(vault), "archive", "--next", str(fixture), "--jobs", "1"])
    out = capsys.readouterr().out
    assert "Resuming from catalog" in out
    assert rc in (0, 10)
    # Completing should register at the bbbb revision, not a new main pin.
    if rc == 10:
        rc = main(
            ["--vault", str(vault), "archive", "--next", str(fixture), "--jobs", "1"]
        )
        capsys.readouterr()
    assert rc == 0
    dirs = list((vault / "test--acme--toy").iterdir())
    assert any(d.name.startswith("bbbbbbbbbbbb") for d in dirs if d.is_dir())
    assert not (vault / "test--acme--toy" / "aaaaaaaaaaaa").exists()


def test_archive_next_full_repo_does_not_resume_subset_pin(vault, test_provider):
    from darsay.archiver import archive
    from darsay.transfer import PartialTransfer
    from tests.conftest import silent

    files = model_files(
        extra={
            "quant.Q4_K_M.gguf": b"q4" * 80,
            "quant.Q8_0.gguf": b"q8" * 80,
        }
    )
    test_provider.add_repo("acme/toy", files)
    with suppress(PartialTransfer):
        archive(
            "test:acme/toy",
            vault=vault,
            include=["*Q4_K_M*"],
            max_bytes=1,
            jobs=1,
            progress=silent,
        )
    assert main(["--vault", str(vault), "catalog", "new", "summer"]) == 0
    assert (
        main(
            [
                "--vault",
                str(vault),
                "catalog",
                "add",
                "summer",
                "test:acme/toy",
                "--desire",
                "9",
            ]
        )
        == 0
    )
    with pytest.raises(SystemExit, match="this pin is a subset|already exists"):
        main(["--vault", str(vault), "archive", "--next", "summer"])


def test_archive_next_errors(vault, test_provider, capsys):
    test_provider.add_repo("acme/toy", model_files())
    assert main(["--vault", str(vault), "catalog", "new", "summer"]) == 0
    capsys.readouterr()
    with pytest.raises(SystemExit, match="is a catalog, not a source"):
        main(["--vault", str(vault), "archive", "summer"])
    with pytest.raises(SystemExit, match="already applies"):
        main(
            ["--vault", str(vault), "archive", "--next", "summer", "--include", "*Q4*"]
        )
    with pytest.raises(SystemExit, match="catalog summer is empty"):
        main(["--vault", str(vault), "archive", "--next", "summer"])


def test_catalog_adopt_and_regen(vault, tmp_path, capsys):
    other = tmp_path / "friend"
    other.mkdir()
    (other / "catalog.json").write_text(
        json.dumps(
            {
                "catalog_schema_version": "1.0.0",
                "kind": "darsay.catalog",
                "id": "summer",
                "title": "Summer",
                "curator": "Alex",
                "created": "2026-01-01T00:00:00+00:00",
                "updated": "2026-01-01T00:00:00+00:00",
                "entries": [
                    {
                        "source": "huggingface:acme/toy",
                        "revision": None,
                        "include": None,
                        "desire": 9,
                        "note": "keep",
                        "added": "2026-01-01T00:00:00+00:00",
                        "estimate": None,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    assert (
        main(["--vault", str(vault), "catalog", "new", "reading", "--curator", "Sam"])
        == 0
    )
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
    assert (
        main(
            [
                "--vault",
                str(vault),
                "catalog",
                "add",
                "summer",
                "test:acme/toy",
                "--desire",
                "7",
                "--include",
                "*config.json",
            ]
        )
        == 0
    )
    capsys.readouterr()
    assert main(["--vault", str(vault), "list", "summer", "--ids"]) == 0
    captured = capsys.readouterr()
    assert "test:acme/toy" in captured.out
    assert "subset or pinned" in captured.err
    assert main(["--vault", str(vault), "list", "summer", "--next"]) == 0
    nxt = capsys.readouterr().out.strip()
    assert "archive" in nxt
    assert "test:acme/toy" in nxt
    assert "--include" in nxt
    assert main(["--vault", str(vault), "list", "--sort", "name"]) == 0
    capsys.readouterr()
    with pytest.raises(SystemExit, match="requires a catalog"):
        main(["--vault", str(vault), "list", "--sort", "desire"])


def test_add_after_unknown_provider_row(vault, test_provider, capsys):
    test_provider.add_repo("acme/toy", model_files())
    assert main(["--vault", str(vault), "catalog", "new", "summer"]) == 0
    capsys.readouterr()
    path = vault / "catalogs" / "summer" / "catalog.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["entries"].append(
        {
            "source": "other:foo/bar",
            "revision": None,
            "include": None,
            "desire": 9,
            "note": None,
            "added": "2026-01-01T00:00:00+00:00",
            "estimate": None,
        }
    )
    path.write_text(json.dumps(data), encoding="utf-8")
    assert (
        main(
            [
                "--vault",
                str(vault),
                "catalog",
                "add",
                "summer",
                "test:acme/toy",
                "--desire",
                "8",
            ]
        )
        == 0
    )
    out = capsys.readouterr().out
    assert "Added test:acme/toy" in out
    sources = [e["source"] for e in json.loads(path.read_text())["entries"]]
    assert "other:foo/bar" in sources
    assert "test:acme/toy" in sources


def test_list_want_and_next_when_complete(vault, test_provider, capsys):
    test_provider.add_repo("acme/toy", model_files())
    archive_quiet("test:acme/toy", vault=vault)
    assert main(["--vault", str(vault), "catalog", "new", "summer"]) == 0
    capsys.readouterr()
    assert (
        main(
            [
                "--vault",
                str(vault),
                "catalog",
                "add",
                "summer",
                "test:acme/toy",
                "--desire",
                "8",
            ]
        )
        == 0
    )
    capsys.readouterr()
    assert main(["--vault", str(vault), "list", "--want"]) == 0
    vault_want = capsys.readouterr().out
    assert "No bundles" not in vault_want
    assert "nothing in progress" in vault_want
    assert main(["--vault", str(vault), "list", "summer", "--want"]) == 0
    cat_want = capsys.readouterr().out
    assert "nothing missing" in cat_want
    assert "1 have" in cat_want
    assert main(["--vault", str(vault), "list", "summer", "--next"]) == 0
    captured = capsys.readouterr()
    assert captured.out.strip() == ""
    assert "nothing missing" in captured.err
    assert main(["--vault", str(vault), "archive", "--next", "summer"]) == 0
    captured = capsys.readouterr()
    assert "error:" not in captured.out
    assert "nothing missing" in captured.err


def test_path_catalog_write_hint_and_bundle_miss(
    vault, tmp_path, test_provider, capsys
):
    test_provider.add_repo("acme/toy", model_files())
    archive_quiet("test:acme/toy", vault=vault)
    friend = tmp_path / "friend"
    friend.mkdir()
    (friend / "catalog.json").write_text(
        json.dumps(
            {
                "catalog_schema_version": "1.0.0",
                "kind": "darsay.catalog",
                "id": "summer",
                "title": "Summer",
                "created": "2026-01-01T00:00:00+00:00",
                "updated": "2026-01-01T00:00:00+00:00",
                "entries": [],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(SystemExit, match=r"catalog adopt <name>") as exc:
        main(["--vault", str(vault), "catalog", "add", str(friend), "test:acme/toy"])
    assert "adopt reading" not in str(exc.value)
    with pytest.raises(SystemExit, match="is a bundle") as bundle_exc:
        main(["--vault", str(vault), "list", "test--acme--toy"])
    assert "darsay info" in str(bundle_exc.value)


def test_estimate_catalog_writes_hints_and_list_shows_them(
    vault, test_provider, capsys
):
    test_provider.add_repo("acme/toy", model_files())
    test_provider.add_repo("acme/locked", model_files(), gated=True)
    test_provider.add_repo(
        "acme/fp8",
        model_files(),
        parameters={
            "total": 16,
            "by_dtype": {"F8_E4M3": 16},
            "dominant_dtype": "F8_E4M3",
        },
    )
    pack = model_files()
    del pack["model.safetensors"]
    pack["toy-Q4_K_M.gguf"] = b"g" * 64
    pack["toy-Q8_0.gguf"] = b"g" * 128
    test_provider.add_repo("acme/pack", pack)
    v = ["--vault", str(vault)]
    assert main([*v, "catalog", "new", "summer"]) == 0
    assert main([*v, "catalog", "add", "summer", "test:acme/toy"]) == 0
    assert main([*v, "catalog", "add", "summer", "test:acme/locked"]) == 0
    assert main([*v, "catalog", "add", "summer", "test:acme/fp8"]) == 0
    assert main([*v, "catalog", "add", "summer", "test:acme/pack"]) == 0
    assert (
        main(
            [*v, "catalog", "add", "summer", "test:acme/pack", "--include", "*Q4_K_M*"]
        )
        == 0
    )
    capsys.readouterr()
    cat_path = vault / "catalogs" / "summer" / "catalog.json"
    assert json.loads(cat_path.read_text())["catalog_schema_version"] == "1.1.0"

    assert main([*v, "list", "summer"]) == 0
    out = capsys.readouterr().out
    assert "HINTS" not in out  # nothing estimated yet: nothing to say

    assert main([*v, "estimate", "summer"]) == 0
    out = capsys.readouterr().out
    assert "test:acme/locked  " in out and "gated" in out
    assert "GATED" not in out
    catalog = json.loads(cat_path.read_text())
    by_key = {
        (e["source"], tuple(e["include"] or ())): e["estimate"]["hints"]
        for e in catalog["entries"]
    }
    assert by_key[("test:acme/toy", ())] == []
    assert by_key[("test:acme/locked", ())] == ["gated"]
    assert by_key[("test:acme/fp8", ())] == ["quant"]
    assert by_key[("test:acme/pack", ())] == ["quant"]
    assert by_key[("test:acme/pack", ("*Q4_K_M*",))] == ["quant", "subset"]

    assert main([*v, "list", "summer"]) == 0
    out = capsys.readouterr().out
    assert "HINTS" in out
    assert "quant, subset" in out
    assert "GATED" not in out
    readme = (vault / "catalogs" / "summer" / "README.md").read_text()
    assert "| Hints |" in readme
    assert "| quant, subset |" in readme

    assert main([*v, "list", "summer", "--json"]) == 0
    rows = json.loads(capsys.readouterr().out)["entries"]
    assert sorted(h for r in rows for h in r["hints"]) == [
        "gated",
        "quant",
        "quant",
        "quant",
        "subset",
    ]


def test_list_derives_hints_for_a_1_0_catalog(vault, test_provider, tmp_path, capsys):
    friend = tmp_path / "friend" / "catalog.json"
    friend.parent.mkdir()
    friend.write_text(
        json.dumps(
            {
                "catalog_schema_version": "1.0.0",
                "kind": "darsay.catalog",
                "id": "theirs",
                "title": "Theirs",
                "curator": None,
                "note": None,
                "created": "2026-01-01T00:00:00+00:00",
                "updated": "2026-01-01T00:00:00+00:00",
                "entries": [
                    {
                        "source": "test:acme/big",
                        "revision": None,
                        "include": ["*Q4_K_M*"],
                        "desire": 8,
                        "note": None,
                        "added": "2026-01-01T00:00:00+00:00",
                        "estimate": {
                            "as_of": "2026-01-01T00:00:00+00:00",
                            "artifact_type": "model",
                            "revision": "a" * 40,
                            "revision_ref": "main",
                            "payload_bytes": 40 * 1024**3,
                            "file_count": 3,
                            "license": "apache-2.0",
                            "gated": True,
                            "parameters": None,
                            "dominant_dtype": None,
                            "unknown_size_count": 0,
                        },
                    }
                ],
            }
        )
    )
    assert main(["--vault", str(vault), "list", str(friend)]) == 0
    out = capsys.readouterr().out
    assert "HINTS" in out
    assert "gated, large, subset" in out
    # Read-only: listing never rewrote the file or its version.
    assert json.loads(friend.read_text())["catalog_schema_version"] == "1.0.0"

from __future__ import annotations

import json
from pathlib import Path

import pytest

from darsay.vault import (
    bundle_id_for,
    default_vault,
    iter_bundle_dirs,
    prune_empty_parent,
    resolve_bundle,
    using_implicit_vault,
)


def _write_bundle(vault, name: str, rev: str, bundle_id: str | None = None) -> None:
    bundle = vault / name / rev
    bundle.mkdir(parents=True)
    bid = bundle_id or f"{name}@{rev}"
    (bundle / "manifest.json").write_text(
        json.dumps({"bundle_id": bid, "licensing": {"spdx_id": "mit"}}),
        encoding="utf-8",
    )


def test_default_vault_home_and_env(monkeypatch, tmp_path):
    monkeypatch.delenv("DARSAY_HOME", raising=False)
    assert default_vault() == Path.home() / "darsay"
    assert using_implicit_vault(None) is True
    monkeypatch.setenv("DARSAY_HOME", str(tmp_path / "env-vault"))
    assert default_vault() == tmp_path / "env-vault"
    assert using_implicit_vault(None) is False
    assert using_implicit_vault(str(tmp_path)) is False


def test_iter_bundle_dirs_finds_manifest_and_ledger(tmp_path):
    _write_bundle(tmp_path, "acme--toy", "aaaaaaaaaaaa")
    partial = tmp_path / "acme--other" / "bbbbbbbbbbbb"
    partial.mkdir(parents=True)
    (partial / "transfer.json").write_text("{}", encoding="utf-8")
    dirs = iter_bundle_dirs(tmp_path)
    assert {d.name for d in dirs} == {"aaaaaaaaaaaa", "bbbbbbbbbbbb"}


def test_bundle_id_for_manifest_and_partial(tmp_path):
    _write_bundle(tmp_path, "acme--toy", "aaaaaaaaaaaa", "acme--toy@aaaaaaaaaaaa")
    assert (
        bundle_id_for(tmp_path / "acme--toy" / "aaaaaaaaaaaa")
        == "acme--toy@aaaaaaaaaaaa"
    )
    orphan = tmp_path / "acme--other" / "bbbbbbbbbbbb"
    orphan.mkdir(parents=True)
    assert bundle_id_for(orphan) == "acme--other@bbbbbbbbbbbb"


def test_resolve_by_path_id_and_unique_prefix(tmp_path):
    _write_bundle(tmp_path, "acme--toy", "aaaaaaaaaaaa")
    bundle = tmp_path / "acme--toy" / "aaaaaaaaaaaa"
    assert resolve_bundle(tmp_path, str(bundle)) == bundle
    assert resolve_bundle(tmp_path, "acme--toy@aaaaaaaaaaaa") == bundle
    assert resolve_bundle(tmp_path, "acme--toy") == bundle
    assert resolve_bundle(tmp_path, "toy@aaaa") == bundle
    assert resolve_bundle(tmp_path, "aaaaaaaaaaaa") == bundle
    assert resolve_bundle(tmp_path, "acme--toy/aaaaaaaaaaaa") == bundle


def test_resolve_ambiguous_and_missing(tmp_path):
    _write_bundle(tmp_path, "acme--toy", "aaaaaaaaaaaa")
    _write_bundle(tmp_path, "acme--other", "bbbbbbbbbbbb")
    with pytest.raises(SystemExit, match="matches 2 bundles"):
        resolve_bundle(tmp_path, "acme")
    with pytest.raises(SystemExit, match="no bundle matching"):
        resolve_bundle(tmp_path, "missing-model")
    with pytest.raises(SystemExit, match="no bundle matching"):
        resolve_bundle(tmp_path, "x")  # too short to substring-match


def test_resolve_path_without_manifest_errors(tmp_path):
    empty = tmp_path / "not-a-bundle"
    empty.mkdir()
    with pytest.raises(SystemExit, match="no manifest.json"):
        resolve_bundle(tmp_path, str(empty))
    with pytest.raises(SystemExit, match="no manifest.json"):
        resolve_bundle(tmp_path, str(tmp_path / "does" / "not" / "exist"))
    with pytest.raises(SystemExit, match="no bundle matching"):
        resolve_bundle(tmp_path, "does/not/exist")


def test_resolve_empty_spec(tmp_path):
    with pytest.raises(SystemExit, match="empty bundle spec"):
        resolve_bundle(tmp_path, "  ")


def test_resolve_partial_requires_flag(tmp_path):
    partial = tmp_path / "acme--toy" / "aaaaaaaaaaaa"
    partial.mkdir(parents=True)
    (partial / "transfer.json").write_text("{}", encoding="utf-8")
    with pytest.raises(SystemExit, match="no manifest.json"):
        resolve_bundle(tmp_path, "acme--toy")
    assert resolve_bundle(tmp_path, "acme--toy", require_manifest=False) == partial


def test_iter_bundle_dirs_skips_reserved(tmp_path):
    _write_bundle(tmp_path, "acme--toy", "aaaaaaaaaaaa")
    planted = tmp_path / "catalogs" / "x"
    planted.mkdir(parents=True)
    (planted / "manifest.json").write_text("{}", encoding="utf-8")
    (planted / "transfer.json").write_text("{}", encoding="utf-8")
    runtime = tmp_path / ".runtime" / "envs"
    runtime.mkdir(parents=True)
    (runtime / "manifest.json").write_text("{}", encoding="utf-8")
    dirs = iter_bundle_dirs(tmp_path)
    assert {d.parent.name for d in dirs} == {"acme--toy"}


def test_bundle_records_source_address_and_remaining(tmp_path):
    from darsay.vault import bundle_records

    bundle = tmp_path / "acme--toy" / "aaaaaaaaaaaa"
    bundle.mkdir(parents=True)
    (bundle / "manifest.json").write_text(
        json.dumps(
            {
                "bundle_id": "acme--toy@aaaaaaaaaaaa",
                "artifact_type": "model",
                "licensing": {"spdx_id": "mit"},
                "inventory": {"total_size_bytes": 10},
                "security": {"integrity_status": "verified-against-upstream"},
                "archive": {"date_archived": "2026-08-01T00:00:00+00:00"},
                "source": {
                    "origin": "huggingface",
                    "provider": "huggingface",
                    "address": "huggingface:acme/toy",
                    "repo_id": "acme/toy",
                    "revision": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                    "revision_ref": "main",
                },
            }
        ),
        encoding="utf-8",
    )
    rows = bundle_records(tmp_path)
    assert rows[0]["status"] == "have"
    assert rows[0]["source_address"] == "huggingface:acme/toy"
    assert rows[0]["remaining_bytes"] == 0
    assert rows[0]["handed_off_bytes"] == 0
    assert rows[0]["revision"].startswith("aaaa")


def test_bundle_records_skeleton_row_reports_moved(tmp_path):
    """A partial with handed-off files reads as a skeleton: percent counts what
    exists anywhere, and the row says how much was handed off."""
    import json

    from darsay.vault import bundle_records

    bundle = tmp_path / "test--acme--toy" / "bbbbbbbbbbbb"
    (bundle / "model").mkdir(parents=True)
    (bundle / "transfer.json").write_text(
        json.dumps(
            {
                "transfer_version": 1,
                "provider": "test",
                "address": "test:acme/toy",
                "repo_id": "acme/toy",
                "repo_type": "model",
                "revision": "b" * 40,
                "revision_ref": "main",
                "pinned_at": "2026-08-30T00:00:00+00:00",
                "expected": [
                    {"path": "a.bin", "size": 40},
                    {"path": "b.bin", "size": 60},
                ],
                "files": {
                    "a.bin": {"status": "verified", "size": 40},
                    "b.bin": {
                        "status": "handed_off",
                        "size": 60,
                        "handed_off_at": "2026-08-30",
                    },
                },
                "metadata": {},
                "sessions": [],
                "events": [],
            }
        ),
        encoding="utf-8",
    )
    (row,) = bundle_records(tmp_path)
    assert row["status"] == "partial"
    assert row["handed_off_bytes"] == 60
    # 40 verified here + 60 handed off = 100 of 100 bytes exist somewhere.
    assert row["percent"] == 100
    # Nothing left to fetch here.
    assert row["remaining_bytes"] == 0
    assert "60 B handed off" in row["integrity"]


def test_resolve_miss_hints_catalog(tmp_path):
    cat = tmp_path / "catalogs" / "summer"
    cat.mkdir(parents=True)
    (cat / "catalog.json").write_text(
        json.dumps(
            {
                "catalog_schema_version": "2.0.0",
                "kind": "darsay.catalog",
                "id": "summer",
                "title": "summer",
                "entries": [],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(SystemExit, match="darsay list summer") as exc:
        resolve_bundle(tmp_path, "summer")
    assert "darsay archive --next summer" in str(exc.value)
    assert "catalog regen" not in str(exc.value)


def test_prune_empty_parent_drops_only_an_empty_name(tmp_path):
    """A removed bundle takes its now-empty ``<vault>/<name>/`` shell with it."""
    gone = tmp_path / "acme--big" / "abc123"
    gone.mkdir(parents=True)
    gone.rmdir()  # what a removal leaves behind
    prune_empty_parent(gone)
    assert not gone.parent.exists()

    # A name that still holds another revision keeps its directory.
    kept = tmp_path / "acme--small" / "abc123"
    kept.mkdir(parents=True)
    sibling = kept.parent / "def456"
    sibling.mkdir()
    kept.rmdir()
    prune_empty_parent(kept)
    assert sibling.is_dir()

    # A parent that is already gone is not an error.
    prune_empty_parent(tmp_path / "never--was" / "abc123")

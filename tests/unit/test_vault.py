from __future__ import annotations

import json
from pathlib import Path

import pytest

from darsay.vault import (
    bundle_id_for,
    default_vault,
    iter_bundle_dirs,
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
    assert bundle_id_for(tmp_path / "acme--toy" / "aaaaaaaaaaaa") == "acme--toy@aaaaaaaaaaaa"
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


def test_resolve_ambiguous_and_missing(tmp_path):
    _write_bundle(tmp_path, "acme--toy", "aaaaaaaaaaaa")
    _write_bundle(tmp_path, "acme--other", "bbbbbbbbbbbb")
    with pytest.raises(SystemExit, match="matches 2 bundles"):
        resolve_bundle(tmp_path, "acme")
    with pytest.raises(SystemExit, match="no bundle matching"):
        resolve_bundle(tmp_path, "missing-model")


def test_resolve_path_without_manifest_errors(tmp_path):
    empty = tmp_path / "not-a-bundle"
    empty.mkdir()
    with pytest.raises(SystemExit, match="no manifest.json"):
        resolve_bundle(tmp_path, str(empty))
    with pytest.raises(SystemExit, match="no manifest.json"):
        resolve_bundle(tmp_path, str(tmp_path / "does" / "not" / "exist"))


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

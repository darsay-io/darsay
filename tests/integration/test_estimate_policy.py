"""Estimate prices the default acquisition; catalogs and the overlay follow."""

from __future__ import annotations

import json

from darsay.cli import main
from tests.integration.conftest import archive_quiet
from tests.payloads import make_gguf, make_safetensors, model_files


def _repo_with_print(test_provider, locator="acme/toy"):
    files = model_files(extra={"Q4_K_M.gguf": make_gguf({"general.file_type": 15})})
    test_provider.add_repo(locator, files)
    return files


def test_estimate_prices_the_policy_selection(vault, test_provider, capsys):
    files = _repo_with_print(test_provider)
    assert main(["--vault", str(vault), "estimate", "test:acme/toy", "--json"]) == 0
    out = capsys.readouterr().out
    est = json.loads(out[out.index("{") :])
    assert est["subset"]["policy"] == "masters"
    assert est["subset"]["include"] == ["model.safetensors"]
    assert est["payload"]["file_count"] < len(files)
    full_paths = {f["path"] for f in est["subset"]["full_files"]}
    assert "Q4_K_M.gguf" in full_paths  # recorded as deliberately omitted

    assert main(["--vault", str(vault), "estimate", "test:acme/toy"]) == 0
    human = capsys.readouterr().out
    assert "masters-first:" in human
    assert "To archive (masters-first is the default):" in human


def test_estimate_full_prices_everything(vault, test_provider, capsys):
    files = _repo_with_print(test_provider)
    assert (
        main(["--vault", str(vault), "estimate", "test:acme/toy", "--full", "--json"])
        == 0
    )
    out = capsys.readouterr().out
    est = json.loads(out[out.index("{") :])
    assert est["subset"] is None
    assert est["payload"]["file_count"] == len(files)


def test_estimate_reflects_an_existing_pin_without_reclassifying(
    vault, test_provider, capsys
):
    _repo_with_print(test_provider)
    assert (
        main(
            [
                "--vault",
                str(vault),
                "archive",
                "test:acme/toy",
                "--max-bytes",
                "1",
                "--jobs",
                "1",
            ]
        )
        == 10
    )
    capsys.readouterr()
    reads = len(test_provider.reads)
    assert main(["--vault", str(vault), "estimate", "test:acme/toy"]) == 0
    out = capsys.readouterr().out
    assert len(test_provider.reads) == reads  # the pin already decided
    assert "banked" in out or "still to fetch" in out


def test_estimate_redundancy_note(vault, test_provider, capsys):
    blob = make_safetensors({"w": ("F32", [2, 4])})
    test_provider.add_repo(
        "acme/dupes",
        model_files(extra={"model-extra.safetensors": blob}),
        parameters={"total": 8, "by_dtype": {"F32": 8}, "dominant_dtype": "F32"},
    )
    assert main(["--vault", str(vault), "estimate", "test:acme/dupes", "--full"]) == 0
    out = capsys.readouterr().out
    assert "several weight sets likely" in out
    assert "darsay classify" in out
    assert (
        main(["--vault", str(vault), "estimate", "test:acme/dupes", "--full", "--json"])
        == 0
    )
    out = capsys.readouterr().out
    est = json.loads(out[out.index("{") :])
    from darsay.catalog import hints_for

    assert "redundant" in hints_for(est)


def test_overlay_null_include_matched_by_policy_bundle(vault, test_provider):
    _repo_with_print(test_provider)
    archive_quiet("test:acme/toy", vault=vault)
    from darsay.catalog import overlay
    from darsay.vault import bundle_records

    catalog = {
        "id": "t",
        "entries": [
            {
                "source": "test:acme/toy",
                "revision": None,
                "include": None,
                "desire": None,
                "note": None,
                "added": None,
                "estimate": None,
            }
        ],
    }
    rows = overlay(catalog, bundle_records(vault))
    assert rows[0]["status"] == "have"
    # An explicit-include entry still demands its exact subset.
    catalog["entries"][0]["include"] = ["*Q8*"]
    rows = overlay(catalog, bundle_records(vault))
    assert rows[0]["status"] == "want"


def test_catalog_refresh_respects_read_budget(
    vault, test_provider, capsys, monkeypatch
):
    _repo_with_print(test_provider, "acme/one")
    _repo_with_print(test_provider, "acme/two")
    v = ["--vault", str(vault)]
    assert main([*v, "catalog", "new", "summer"]) == 0
    assert main([*v, "catalog", "add", "summer", "test:acme/one"]) == 0
    assert main([*v, "catalog", "add", "summer", "test:acme/two"]) == 0
    capsys.readouterr()
    monkeypatch.setattr("darsay.cli.REFRESH_READ_BUDGET_REQUESTS", 1)
    assert main([*v, "estimate", "summer"]) == 0
    err = capsys.readouterr().err
    assert "read budget reached" in err
    cat_path = vault / "catalogs" / "summer" / "catalog.json"
    entries = {
        e["source"]: e["estimate"] for e in json.loads(cat_path.read_text())["entries"]
    }
    assert entries["test:acme/one"]["policy"] == "masters"
    assert entries["test:acme/two"]["policy"] is None

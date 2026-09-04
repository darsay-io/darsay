"""Estimate prices the default acquisition; catalogs and the overlay follow."""

from __future__ import annotations

import json

from darsay.cli import main
from tests.integration.conftest import archive_quiet
from tests.payloads import make_gguf, make_safetensors, model_files


def _repo_with_print(test_provider, locator="acme/toy"):
    files = model_files()
    files["mirror/model.safetensors"] = files["model.safetensors"]
    test_provider.add_repo(locator, files)
    return files


def test_estimate_prices_the_policy_selection(vault, test_provider, capsys):
    files = _repo_with_print(test_provider)
    assert main(["--vault", str(vault), "estimate", "test:acme/toy", "--json"]) == 0
    out = capsys.readouterr().out
    est = json.loads(out[out.index("{") :])
    assert est["subset"]["policy"] == "negatives"
    assert est["subset"]["include"] == ["/model.safetensors"]
    assert est["payload"]["file_count"] < len(files)
    full_paths = {f["path"] for f in est["subset"]["full_files"]}
    assert "mirror/model.safetensors" in full_paths  # recorded as deliberately omitted

    assert main(["--vault", str(vault), "estimate", "test:acme/toy"]) == 0
    human = capsys.readouterr().out
    assert "archive:" in human
    assert "recorded omissions:" in human
    assert "To archive: darsay archive test:acme/toy" in human


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


def test_estimate_retains_gguf_without_regeneration_proof(vault, test_provider, capsys):
    files = model_files(extra={"Q4_K_M.gguf": make_gguf({"general.file_type": 15})})
    test_provider.add_repo("acme/gguf", files)
    assert main(["--vault", str(vault), "estimate", "test:acme/gguf", "--json"]) == 0
    out = capsys.readouterr().out
    est = json.loads(out[out.index("{") :])
    assert est["subset"] is None
    assert est["payload"]["total_size_bytes"] == sum(map(len, files.values()))
    assert est["classification"]["skipped_bytes"] == 0
    assert est["classification"]["verdicts"]["unknown"]["sets"] == 1


def test_estimate_explains_retained_upstream_match(vault, test_provider, capsys):
    base = model_files(param_shape=[2, 4])
    test_provider.add_repo("acme/base", base)
    archive_quiet("test:acme/base", vault=vault, full=True)
    test_provider.add_repo(
        "acme/copy",
        model_files(
            param_shape=[3, 4],
            extra={"copy/model.safetensors": base["model.safetensors"]},
        ),
        metadata={"tags": ["base_model:acme/base"], "card_data": {}, "gated": False},
    )
    assert main(["--vault", str(vault), "estimate", "test:acme/copy"]) == 0
    human = capsys.readouterr().out
    assert (
        "prints retained; exact recovery from this bundle is not established" in human
    )
    assert "source is not archived here" not in human


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
    # The pin already decided: no header reads. config.json is read once for
    # the precision facts, which every estimate reports.
    later = [r for r in test_provider.reads[reads:] if r[0] != "config.json"]
    assert later == []
    assert "banked" in out or "still to fetch" in out
    from darsay.catalog import estimate_digest
    from darsay.estimate import estimate

    priced = estimate("test:acme/toy", vault=vault, progress=lambda _: None)
    assert priced["payload"]["total_size_bytes"] == priced["transfer"]["bytes"]["total"]
    assert priced["payload"]["total_size_bytes"] < priced["repository_bytes"]
    digest = estimate_digest(priced)
    assert digest["size_basis"] == "archive"
    assert digest["classification"]["skipped_bytes"] > 0


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
    assert entries["test:acme/one"]["size_basis"] == "archive"
    assert entries["test:acme/two"]["size_basis"] == "repository"

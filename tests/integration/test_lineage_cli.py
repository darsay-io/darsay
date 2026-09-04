"""Closed works, the family view, and the precision facts on the CLI surface."""

from __future__ import annotations

import json

import pytest

from darsay.cli import main
from tests.payloads import model_files

HOME = "https://www.qwencloud.com/models/qwen3.8-max-0902"


def _add(vault, *args):
    return main(["--vault", str(vault), "catalog", "add", "summer", *args])


def test_a_home_url_is_a_closed_work_that_holds_its_place(vault, test_provider, capsys):
    test_provider.add_repo("Qwen/Qwen3.8-27B", model_files())
    assert main(["--vault", str(vault), "catalog", "new", "summer"]) == 0
    assert _add(vault, "test:Qwen/Qwen3.8-27B", "--desire", "5") == 0
    capsys.readouterr()
    assert _add(vault, HOME, "--desire", "9", "--note", "API only, for now") == 0
    out = capsys.readouterr().out
    assert "closed — a home page, not a source" in out
    assert "when weights are published" in out

    # The file records the home verbatim; it is a work, not a fetchable ref.
    catalog = json.loads((vault / "catalogs" / "summer" / "catalog.json").read_text())
    sources = [e["source"] for e in catalog["entries"]]
    assert HOME in sources
    assert catalog["catalog_schema_version"] == "3.0.0"

    # The overlay lists it as closed, priced as closed, in the same family.
    assert main(["--vault", str(vault), "list", "summer", "--json"]) == 0
    view = json.loads(capsys.readouterr().out)
    rows = {r["source"]: r for r in view["entries"]}
    closed = rows[HOME]
    assert closed["status"] == "closed"
    assert closed["home"] == HOME
    assert closed["lineage"]["family"] == "qwen"
    assert closed["lineage"]["generation"] == "3.8"
    assert closed["lineage"]["member"] == "max-0902"
    assert closed["lineage"]["read_from"] == "name"
    open_row = rows["test:Qwen/Qwen3.8-27B"]
    assert open_row["lineage"]["family"] == "Qwen"
    assert open_row["lineage"]["size"] == {"total": 27e9, "active": None}
    assert view["stats"]["closed"] == 1
    assert view["stats"]["want"] == 1

    # The table shows a FAMILY column and prices the closed row as closed.
    assert main(["--vault", str(vault), "list", "summer"]) == 0
    table = capsys.readouterr().out
    assert "FAMILY" in table
    assert "closed" in table
    assert "Qwen 3.8" in table
    # One spelling per family across the table: the closed row's lowercase
    # URL name takes the family's majority spelling.
    assert "qwen 3.8" not in table

    # --next never picks a closed row; with only closed rows left it says so.
    assert main(["--vault", str(vault), "list", "summer", "--next"]) == 0
    assert "test:Qwen/Qwen3.8-27B" in capsys.readouterr().out
    assert (
        main(
            [
                "--vault",
                str(vault),
                "catalog",
                "drop",
                "summer",
                "test:Qwen/Qwen3.8-27B",
            ]
        )
        == 0
    )
    capsys.readouterr()
    with pytest.raises(SystemExit, match="closed"):
        main(["--vault", str(vault), "list", "summer", "--next"])

    # A closed work cannot carry a pin or an include: there is nothing to fetch.
    try:
        _add(vault, HOME, "--revision", "v1")
    except SystemExit as exc:
        assert "nothing to pin" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("a closed work accepted --revision")


def test_estimate_refresh_leaves_closed_rows_in_place(vault, test_provider, capsys):
    test_provider.add_repo(
        "Qwen/Qwen3.8-27B",
        model_files(),
        parameters={"total": 8, "by_dtype": {"F32": 8}, "dominant_dtype": "F32"},
    )
    assert main(["--vault", str(vault), "catalog", "new", "summer"]) == 0
    assert _add(vault, "test:Qwen/Qwen3.8-27B") == 0
    assert _add(vault, HOME) == 0
    capsys.readouterr()
    assert main(["--vault", str(vault), "estimate", "summer"]) == 0
    out = capsys.readouterr()
    assert "1 closed row holds its place" in out.out
    assert "warning" not in out.err
    catalog = json.loads((vault / "catalogs" / "summer" / "catalog.json").read_text())
    by_source = {e["source"]: e for e in catalog["entries"]}
    assert by_source[HOME]["estimate"] is None
    digest = by_source["test:Qwen/Qwen3.8-27B"]["estimate"]
    # Classified by the CLI, nothing to skip: still priced as the negative set.
    assert digest["size_basis"] == "archive"
    assert digest["precision"] == "F32"
    assert digest["bytes_per_param"] is not None
    assert digest["architecture"] == "testlm"
    assert digest["parents"] is None
    readme = (vault / "catalogs" / "summer" / "README.md").read_text()
    assert "## Families" in readme
    assert "### Qwen" in readme
    assert "closed" in readme


def test_list_sort_family_reads_the_tree(vault, test_provider, capsys):
    for locator in (
        "Qwen/Qwen3-8B-Base",
        "Qwen/Qwen3.8-2.4T-A95B",
        "moonshotai/Kimi-K3",
        "Qwen/Qwen3.5-397B-A17B",
    ):
        test_provider.add_repo(locator, model_files())
    assert main(["--vault", str(vault), "catalog", "new", "summer"]) == 0
    for locator in (
        "test:moonshotai/Kimi-K3",
        "test:Qwen/Qwen3.8-2.4T-A95B",
        "test:Qwen/Qwen3-8B-Base",
        "test:Qwen/Qwen3.5-397B-A17B",
    ):
        assert _add(vault, locator) == 0
    capsys.readouterr()
    assert (
        main(["--vault", str(vault), "list", "summer", "--sort", "family", "--json"])
        == 0
    )
    view = json.loads(capsys.readouterr().out)
    assert [r["source"] for r in view["entries"]] == [
        "test:moonshotai/Kimi-K3",
        "test:Qwen/Qwen3-8B-Base",
        "test:Qwen/Qwen3.5-397B-A17B",
        "test:Qwen/Qwen3.8-2.4T-A95B",
    ]


def test_estimate_reports_precision_and_lineage(vault, test_provider, capsys):
    test_provider.add_repo(
        "OBLITERATUS/Qwen3.8-27B-OBLITERATED",
        model_files(),
        parameters={"total": 8, "by_dtype": {"F32": 8}, "dominant_dtype": "F32"},
        metadata={
            "card_data": {"license": "apache-2.0", "base_model": "Qwen/Qwen3.8-27B"},
            "tags": ["base_model:finetune:Qwen/Qwen3.8-27B"],
            "gated": False,
            "created_at": "2026-01-01T00:00:00+00:00",
            "last_modified": "2026-01-01T00:00:00+00:00",
            "downloads": 0,
            "likes": 0,
        },
    )
    assert (
        main(
            [
                "--vault",
                str(vault),
                "estimate",
                "test:OBLITERATUS/Qwen3.8-27B-OBLITERATED",
                "--json",
            ]
        )
        == 0
    )
    est = json.loads(capsys.readouterr().out)
    assert est["precision"]["label"] == "F32"
    # The fixture's safetensors header outweighs its eight F32 weights; the
    # measured figure is honest about that, and it is a positive number.
    assert est["precision"]["bytes_per_param"] > 4.0
    assert est["precision"]["quantized"] is False
    lin = est["lineage"]
    assert (lin["family"], lin["generation"], lin["member"]) == ("Qwen", "3.8", "27B")
    assert lin["variants"] == ["abliterated"]
    assert lin["architecture"] == "testlm"
    assert lin["parents"] == [
        {
            "source": "test:Qwen/Qwen3.8-27B",
            "relation": "finetune",
            "declared_by": "card",
        }
    ]
    assert est["classification"]["verdicts"]["negative"]["sets"] == 1

    assert (
        main(
            [
                "--vault",
                str(vault),
                "estimate",
                "test:OBLITERATUS/Qwen3.8-27B-OBLITERATED",
            ]
        )
        == 0
    )
    human = capsys.readouterr().out
    assert "precision:    F32 — " in human
    assert "B/param" in human
    assert (
        "family:       Qwen · generation 3.8 · member 27B · abliterated  [read from the name]"
        in human
    )
    assert (
        "lineage:      finetune of test:Qwen/Qwen3.8-27B  [declared upstream]" in human
    )
    assert "archive:      the whole repo — 1 negative set" in human
    assert "nothing here is a print" in human

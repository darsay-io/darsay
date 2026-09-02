"""The name grammar: family, generation, member, variants, formats, size.

The fixture table is shared with darsay.io (``website/src/lib``), so a
board and the CLI read every name the same way.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from darsay.lineage import (
    Lineage,
    display_generation,
    generation_sort_key,
    group_by_family,
    lineage_of_source,
    name_of_source,
    parse_name,
)

FIXTURES = json.loads(
    (
        Path(__file__).resolve().parents[1] / "fixtures" / "lineage-names.json"
    ).read_text()
)


@pytest.mark.parametrize(
    "row", FIXTURES, ids=[r["name"] or "(empty)" for r in FIXTURES]
)
def test_grammar_matches_the_shared_fixtures(row):
    got = parse_name(row["name"])
    assert got.family == row["family"]
    assert got.generation == row["generation"]
    assert got.member == row["member"]
    assert list(got.variants) == row["variants"]
    assert list(got.formats) == row["formats"]
    assert got.size_total == row["size_total"]
    assert got.size_active == row["size_active"]


def test_family_key_folds_case_so_a_closed_work_meets_its_open_siblings():
    open_row = parse_name("Qwen3.8-2.4T-A95B")
    closed_row = parse_name("qwen3.8-max-0902")
    assert open_row.family_key == closed_row.family_key == "qwen"
    assert open_row.generation == closed_row.generation == "3.8"


def test_generation_ordering_is_numeric_within_a_family():
    gens = ["K3", "K2", "K2.5"]
    assert sorted(gens, key=generation_sort_key) == ["K2", "K2.5", "K3"]
    assert sorted(["3.8", "3", "3.5", "4"], key=generation_sort_key) == [
        "3",
        "3.5",
        "3.8",
        "4",
    ]
    assert generation_sort_key(None) < generation_sort_key("1")


def test_name_of_source_reads_refs_and_home_urls():
    assert name_of_source("huggingface:Qwen/Qwen3.8-27B") == "Qwen3.8-27B"
    assert (
        name_of_source("huggingface:datasets/saidutta69/fable-5-premium")
        == "fable-5-premium"
    )
    assert (
        name_of_source("https://www.qwencloud.com/models/qwen3.8-max-0902")
        == "qwen3.8-max-0902"
    )
    assert (
        name_of_source("https://www.qwencloud.com/models/qwen3.8-max-0902/?tab=1#x")
        == "qwen3.8-max-0902"
    )
    assert name_of_source("test:acme/toy") == "toy"
    assert name_of_source("") == ""


def test_as_dict_labels_its_provenance():
    d = lineage_of_source("huggingface:Qwen/Qwen3.8-2.4T-A95B").as_dict()
    assert d["read_from"] == "name"
    assert d["size"] == {"total": 2.4e12, "active": 95e9}
    assert lineage_of_source("huggingface:moonshotai/Kimi-K3").as_dict()["size"] is None
    assert display_generation("Qwen", "3.8") == "Qwen 3.8"
    assert display_generation("Kimi", None) == "Kimi"
    assert display_generation(None, None) == "—"


def test_group_by_family_builds_the_tree_oldest_generation_first():
    rows = [
        {"source": "huggingface:Qwen/Qwen3.8-2.4T-A95B"},
        {"source": "huggingface:Qwen/Qwen3-8B-Base"},
        {"source": "https://www.qwencloud.com/models/qwen3.8-max-0902"},
        {"source": "huggingface:OBLITERATUS/Qwen3.8-27B-OBLITERATED"},
        {"source": "huggingface:Qwen/Qwen3.5-397B-A17B"},
        {"source": "huggingface:moonshotai/Kimi-K3"},
        {"source": "huggingface:moonshotai/Kimi-K2-Base"},
        {"source": "huggingface:Uniboshi/Kimi-K3-Abliterated-V1"},
        {"source": "huggingface:datasets/saidutta69/fable-5-premium"},
    ]
    tree = group_by_family(rows)
    assert [f["family"] for f in tree] == ["Qwen", "Kimi", "fable"]
    qwen = tree[0]
    assert qwen["home_publisher"] == "Qwen"
    assert qwen["count"] == 5
    assert [g["generation"] for g in qwen["generations"]] == ["3", "3.5", "3.8"]
    gen38 = qwen["generations"][-1]["rows"]
    # Sized members sort by size; unsized (the closed Max) first.
    assert [r["source"] for r in gen38][-1] == "huggingface:Qwen/Qwen3.8-2.4T-A95B"
    kimi = tree[1]
    assert kimi["home_publisher"] == "moonshotai"
    assert [g["generation"] for g in kimi["generations"]] == ["K2", "K3"]


def test_lineage_is_a_frozen_value():
    a = parse_name("Qwen3-0.6B")
    b = parse_name("Qwen3-0.6B")
    assert a == b
    assert isinstance(a, Lineage)
    with pytest.raises(AttributeError):
        a.family = "x"  # type: ignore[misc]

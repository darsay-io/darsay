from __future__ import annotations

import json
from pathlib import Path

import pytest

from darsay.collection import bit_family, family, selection_totals, starting_selection
from darsay.collection_tui import CollectionState, clipped, safe_text, wrapped
from darsay.weight_variants import gguf_variants


@pytest.fixture
def inventory():
    fixture = json.loads(
        (Path(__file__).parents[1] / "fixtures/glm-5.3-flash-gguf.json").read_text()
    )
    return {**fixture, "variants": gguf_variants(fixture["files"]), "companions": []}


@pytest.mark.parametrize(
    "precision,bits,group",
    [
        ("UD-Q4_K_XL", 4, "middle"),
        ("IQ4_XS", 4, "middle"),
        ("Q8_0", 8, "wide"),
        ("Q2_K", 2, "compact"),
        ("BF16", None, "float"),
        ("new-format", None, "unknown"),
    ],
)
def test_encoding_families(precision, bits, group):
    assert bit_family(precision) == bits
    assert family(precision) == group


def test_starting_points_and_actual_scope(inventory):
    state = CollectionState(inventory)
    assert state.include == []
    state.key("1")
    assert [v["precision"] for v in state.groups if state.selected(v)] == ["UD-IQ4_XS"]
    state.key("2")
    assert sorted(
        bit_family(v["precision"]) for v in state.groups if state.selected(v)
    ) == [4, 8]
    state.key("3")
    assert selection_totals(inventory["files"], state.include) == {
        "bytes": 2_545_636_747_545,
        "files": 87,
        "unknown": 0,
    }
    assert state.key("\n") is None
    assert state.page == "review"
    assert state.key("\n") == "confirm"


def test_q4_counts_all_shards_and_support_once(inventory):
    q4 = next(v for v in inventory["variants"] if v["precision"] == "UD-Q4_K_XL")
    q8 = next(v for v in inventory["variants"] if v["precision"] == "Q8_0")
    assert selection_totals(inventory["files"], q4["include"]) == {
        "bytes": 199_707_329_724,
        "files": 7,
        "unknown": 0,
    }
    assert (
        selection_totals(inventory["files"], q4["include"] + q8["include"])["bytes"]
        == q4["size_bytes"] + q8["size_bytes"] + 8377
    )


def test_review_refinement_empty_selection_and_cancel(inventory):
    state = CollectionState(inventory)
    assert state.key("\n") is None
    assert state.page == "choose"
    state.key("1")
    draft = state.include.copy()
    state.key("\n")
    assert "PINNED REVISION" in state.review_lines()
    assert state.key("\x1b") is None
    assert state.include == draft
    assert state.key("?") is None
    assert state.page == "guide"
    assert any("unverified" in line.lower() for line in state.field_notes())
    state.key("\x1b")
    assert state.key("\x1b") == "cancel"
    assert state.key("q") == "cancel"


def test_incomplete_and_unknown_families_are_not_preset_guesses(inventory):
    invalid = [
        {
            **v,
            "complete": i % 2 == 0,
            "size_bytes": None if i % 2 == 0 else v["size_bytes"],
        }
        for i, v in enumerate(inventory["variants"])
    ]
    assert starting_selection(invalid, "single") == []
    state = CollectionState({**inventory, "variants": invalid})
    state.cursor = 1
    state.key(" ")
    assert not state.include
    assert "Incomplete" in state.message
    state.key("3")
    assert state.include == ["/*"]


def test_terminal_text_has_no_control_sequences_and_clips_wide_names():
    assert "\x1b" not in safe_text("bad\x1b[2Jname\n")
    assert "\u202e" not in safe_text("bad\u202ename")
    assert clipped("模型-Q4", 5) == "模型-"
    assert clipped("hi", 0) == ""
    assert "".join(wrapped(["模型" * 12], 8)) == "模型" * 12


def test_unknown_sizes_are_a_lower_bound():
    assert selection_totals(
        [{"path": "a.gguf", "size": None}, {"path": "README.md", "size": 7}],
        ["/a.gguf"],
    ) == {"bytes": 7, "files": 2, "unknown": 1}

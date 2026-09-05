from __future__ import annotations

import json
from pathlib import Path

import pytest

from darsay.collection import bit_family, family, selection_totals, starting_selection
from darsay.collection_tui import (
    CollectionState,
    clipped,
    opening_state,
    safe_text,
    wrapped,
)
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
    review = " ".join(state.review_lines())
    assert "no include selectors" in review and "/*" not in review
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


def test_a_forced_repin_opens_on_the_existing_pin(inventory):
    q4 = next(v for v in inventory["variants"] if v["precision"] == "UD-Q4_K_XL")
    assert opening_state(inventory, None).include == []
    state = opening_state(
        inventory, {"include": q4["include"], "verified": 7, "verified_bytes": 2**31}
    )
    assert state.selected(q4)
    assert state.groups[state.cursor] is q4
    assert "Pinned on disk: 7 verified files, 2.0 GiB" in state.message
    whole = opening_state(
        inventory, {"include": None, "verified": 87, "verified_bytes": 0}
    )
    assert whole.include == ["/*"]


class FakeScreen:
    """A curses window that records what each refresh drew, row by row."""

    def __init__(self, height, width, keys):
        self.height, self.width, self.keys = height, width, list(keys)
        self.rows: dict[int, list] = {}
        self.frames: list[dict[int, list]] = []

    def getmaxyx(self):
        return self.height, self.width

    def keypad(self, _flag):
        pass

    def erase(self):
        self.rows = {}

    def addstr(self, y, x, text, attr=0):
        self.rows.setdefault(y, []).append((x, text, attr))

    def refresh(self):
        self.frames.append({y: sorted(parts) for y, parts in self.rows.items()})

    def getkey(self):
        return self.keys.pop(0)


def _text(frame):
    return {y: "".join(text for _, text, _ in parts) for y, parts in frame.items()}


def test_the_room_fits_an_ordinary_terminal(inventory):
    import curses

    from darsay.collection_tui import _room

    screen = FakeScreen(24, 80, ["1", "KEY_NPAGE", "\n", "\n"])
    q4 = next(v for v in inventory["variants"] if v["precision"] == "UD-IQ4_XS")
    assert _room(screen, CollectionState(inventory)) == q4["include"]
    opened, chosen, paged, review = (_text(f) for f in screen.frames[:4])
    # Eight of the twelve groups at a time, and a count of what lies beyond.
    assert sum("[" in opened.get(y, "") for y in range(8, 16)) == 8
    assert "4 more below" in opened[16]
    assert "4 above" in paged[16] and "more below" not in paged[16]
    # A long status message wraps onto its second row instead of clipping.
    assert "Smallest complete" in chosen[18] and "guarantee" in chosen[19]
    assert "01 choose" in opened[1] and "PINNED REVISION" in " ".join(review.values())
    # The step being taken is the lit one.
    lit = {text: attr for _, text, attr in screen.frames[3][1]}
    assert lit["02 review"] == curses.A_BOLD and lit["01 choose"] == curses.A_NORMAL


def test_the_room_at_its_minimum_and_beside_its_guide(inventory):
    from darsay.collection_tui import _room

    small = FakeScreen(20, 60, ["q"])
    with pytest.raises(KeyboardInterrupt):
        _room(small, CollectionState(inventory))
    frame = _text(small.frames[0])
    assert sum("[" in frame.get(y, "") for y in range(8, 12)) == 4
    assert "Q cancel" in frame[18]

    wide = FakeScreen(30, 110, ["q"])
    with pytest.raises(KeyboardInterrupt):
        _room(wide, CollectionState(inventory))
    frame = _text(wide.frames[0])
    assert any("? opens the full guide" in line for line in frame.values())


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

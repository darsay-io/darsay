"""Board URL grammar and entry matching — no network."""

from __future__ import annotations

from darsay.board import Board, entry_id_for, parse_board_url

BOARD_ID = "3b8cb153111534e3c468907ded2a50f7"


def test_parse_board_url_forms():
    expect = Board(origin="https://darsay.io", id=BOARD_ID)
    assert parse_board_url(f"https://darsay.io/b/{BOARD_ID}") == expect
    assert parse_board_url(f"https://darsay.io/b/{BOARD_ID}/") == expect
    assert parse_board_url(f"https://darsay.io/api/boards/{BOARD_ID}") == expect
    assert (
        parse_board_url(f"https://darsay.io/api/boards/{BOARD_ID}/catalog.json")
        == expect
    )
    assert parse_board_url(f"http://localhost:4321/b/{BOARD_ID}") == Board(
        origin="http://localhost:4321", id=BOARD_ID
    )


def test_parse_board_url_rejects_non_boards():
    assert parse_board_url("summer-2026-heater") is None
    assert parse_board_url("./catalog.json") is None
    assert parse_board_url("https://darsay.io/boards") is None
    assert parse_board_url("https://darsay.io/b/notahexid") is None
    assert parse_board_url(f"https://darsay.io/b/{BOARD_ID}/entries") is None
    assert parse_board_url("") is None


def test_board_urls():
    board = Board(origin="https://darsay.io", id=BOARD_ID)
    assert board.page_url == f"https://darsay.io/b/{BOARD_ID}"
    assert board.catalog_url == f"https://darsay.io/api/boards/{BOARD_ID}/catalog.json"
    assert (
        board.claim_url(4) == f"https://darsay.io/api/boards/{BOARD_ID}/entries/4/claim"
    )


def test_entry_id_for_matches_identity():
    entries = [
        {"id": 4, "source": "test:acme/toy", "revision": None, "include": None},
        {"id": 5, "source": "test:acme/toy", "revision": None, "include": ["*.gguf"]},
        {"id": 6, "source": "test:acme/other", "revision": "abc", "include": None},
    ]
    assert entry_id_for(entries, "test:acme/toy", None, None) == 4
    assert entry_id_for(entries, "test:acme/toy", None, ["*.gguf"]) == 5
    assert entry_id_for(entries, "test:acme/other", "abc", None) == 6
    assert entry_id_for(entries, "test:acme/other", None, None) is None
    assert entry_id_for(entries, "test:acme/missing", None, None) is None

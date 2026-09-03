"""Board URL grammar and entry matching — no network."""

from __future__ import annotations

from darsay.board import Board, entry_id_for, parse_board_url

BOARD_ID = "3b8cb153111534e3c468907ded2a50f7"


def test_parse_board_url_forms():
    expect = Board(origin="https://darsay.io", id=BOARD_ID)
    assert parse_board_url(f"https://darsay.io/b/{BOARD_ID}") == expect
    assert parse_board_url(f"https://darsay.io/b/{BOARD_ID}/") == expect
    # The page as a document — the address the site hands a program.
    assert parse_board_url(f"https://darsay.io/b/{BOARD_ID}.json") == expect
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
    # A key-addressed board names no id: nothing for the CLI to claim on.
    assert parse_board_url("https://darsay.io/api/board") is None
    assert parse_board_url("https://darsay.io/api/board.json") is None
    assert parse_board_url(f"https://darsay.io/api/boards/{BOARD_ID}.json/x") is None
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


def test_entry_for_returns_the_row_with_its_board_side_fields():
    entries = [
        {"id": 4, "source": "test:acme/toy", "revision": None, "status": "have"},
    ]
    from darsay.board import entry_for

    row = entry_for(entries, "test:acme/toy", None, None)
    assert row is not None and row["status"] == "have" and row["id"] == 4
    assert entry_for(entries, "test:acme/missing", None, None) is None


def test_client_id_defaults_to_a_pseudonym_never_the_hostname(monkeypatch, tmp_path):
    """The board URL travels; the machine's hostname must not travel with it."""
    import re
    import socket

    from darsay import board as board_mod

    monkeypatch.setattr(socket, "gethostname", lambda: "JEREMYS-MBP-C7456D33")
    monkeypatch.setenv("USER", "jeremy")
    got = board_mod.client_id(tmp_path)
    assert "JEREMYS" not in got and "C7456D33".lower() not in got.lower()
    assert re.fullmatch(r"[a-z]+-[a-z]+-[0-9a-f]{2}", got)
    assert board_mod.client_id(tmp_path) == got  # stable across calls
    monkeypatch.setattr(socket, "gethostname", lambda: "another-machine")
    assert board_mod.client_id(tmp_path) != got  # but distinct per machine


def test_client_id_config_still_wins(monkeypatch, tmp_path):
    from darsay import board as board_mod

    (tmp_path / "config.toml").write_text('[board]\nclient = "jeremy-mbp"\n')
    assert board_mod.client_id(tmp_path) == "jeremy-mbp"


def test_http_identifies_the_client(monkeypatch):
    """Cloudflare bans the bare Python-urllib signature (error 1010)."""
    from darsay import __version__
    from darsay import board as board_mod

    captured = {}

    class _Resp:
        status = 200

        def read(self, n):
            return b"{}"

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def fake_urlopen(request, timeout=None):
        captured["ua"] = request.get_header("User-agent")
        captured["content_type"] = request.get_header("Content-type")
        return _Resp()

    monkeypatch.setattr(board_mod._urlrequest, "urlopen", fake_urlopen)
    status, _ = board_mod._http("GET", "https://darsay.io/api/boards/x")
    assert status == 200
    assert captured["ua"] == f"darsay/{__version__} (+https://darsay.io)"
    assert captured["content_type"] is None
    board_mod._http("POST", "https://darsay.io/api/boards/x/catalog.json", b"{}")
    assert captured["content_type"] == "application/json"


def test_push_catalog_detects_a_pre_import_worker(monkeypatch, tmp_path):
    """An old worker answers POST catalog.json with the download itself."""
    import json

    import pytest

    from darsay import board as board_mod

    doc = tmp_path / "catalog.json"
    doc.write_text("{}")
    old_worker = json.dumps({"kind": "darsay.catalog", "entries": []}).encode()
    monkeypatch.setattr(board_mod, "_http", lambda m, u, d=None: (200, old_worker))
    board = board_mod.Board(origin="https://darsay.io", id="a" * 32)
    with pytest.raises(SystemExit, match="does not support catalog import"):
        board_mod.push_catalog(board, doc)

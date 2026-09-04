"""Boards as remote catalogs: fetch, refresh, push, claim — hermetic."""

from __future__ import annotations

import json

import pytest

from darsay.cli import main
from tests.payloads import make_gguf, model_files

BOARD_ID = "3b8cb153111534e3c468907ded2a50f7"
BOARD_URL = f"https://darsay.io/b/{BOARD_ID}"


class FakeBoardServer:
    """The worker's contract, in memory, behind the darsay.board._http seam."""

    def __init__(self, entries: list[dict], catalog_id: str = "summer"):
        self.catalog_id = catalog_id
        self.entries = entries
        self.pushed: list[dict] = []
        self.claims: list[dict] = []
        self.claimed_by: dict[int, dict] = {}
        self.releases: list[int] = []

    def catalog(self) -> dict:
        return {
            "catalog_schema_version": "3.0.0",
            "kind": "darsay.catalog",
            "id": self.catalog_id,
            "title": self.catalog_id,
            "curator": None,
            "note": None,
            "created": "2026-08-28T18:09:16+00:00",
            "updated": "2026-08-31T12:58:45+00:00",
            "entries": [
                {
                    "source": e["source"],
                    "revision": e.get("revision"),
                    "include": e.get("include"),
                    "desire": e.get("desire"),
                    "note": None,
                    "added": "2026-08-28T18:13:49+00:00",
                    "estimate": e.get("estimate"),
                }
                for e in self.entries
            ],
        }

    def __call__(self, method: str, url: str, data: bytes | None = None):
        if url.endswith("/catalog.json") and method == "GET":
            return 200, json.dumps(self.catalog()).encode()
        if url.endswith("/catalog.json") and method == "POST":
            doc = json.loads(data)
            self.pushed.append(doc)
            return 200, json.dumps(
                {
                    "ok": True,
                    "updated": len(doc.get("entries", [])),
                    "added": 0,
                    "removed": 0,
                }
            ).encode()
        if "/claim" in url:
            entry_id = int(url.split("/entries/")[1].split("/")[0])
            body = json.loads(data or b"{}")
            if method == "DELETE":
                self.releases.append(entry_id)
                self.claimed_by.pop(entry_id, None)
                return 200, b"{}"
            current = self.claimed_by.get(entry_id)
            if (
                current
                and current.get("client") != body.get("client")
                and body.get("force") is not True
                and current.get("state") != "done"
            ):
                return 409, json.dumps({"error": "claimed", "claim": current}).encode()
            entry = next((e for e in self.entries if e.get("id") == entry_id), {})
            if (
                entry.get("status") == "have"
                and body.get("refetch") is not True
                and body.get("force") is not True
                and not (current and current.get("client") == body.get("client"))
            ):
                return 409, json.dumps({"error": "have", "claim": current}).encode()
            self.claimed_by[entry_id] = dict(body)
            self.claims.append({"entry_id": entry_id, **body})
            return 200, json.dumps({"id": entry_id, "claim": body}).encode()
        if method == "GET":
            return 200, json.dumps({"id": BOARD_ID, "entries": self.entries}).encode()
        return 404, b"{}"


@pytest.fixture
def board_server(monkeypatch):
    def install(entries):
        server = FakeBoardServer(entries)
        monkeypatch.setattr("darsay.board._http", server)
        return server

    return install


def test_estimate_round_trip_pushes_classified_digests(
    vault, test_provider, board_server, capsys
):
    test_provider.add_repo(
        "acme/toy",
        model_files(extra={"Q4_K_M.gguf": make_gguf({"general.file_type": 15})}),
    )
    server = board_server([{"id": 4, "source": "test:acme/toy"}])
    assert main(["--vault", str(vault), "estimate", BOARD_URL]) == 0
    out = capsys.readouterr().out
    assert "Fetched board catalog" in out
    assert f"Pushed to {BOARD_URL}" in out
    assert len(server.pushed) == 1
    entry = server.pushed[0]["entries"][0]
    assert entry["estimate"]["size_basis"] == "archive"
    assert entry["estimate"]["payload_bytes"] > 0
    assert "hints" in entry["estimate"]


def test_estimate_dry_run_does_not_push(vault, test_provider, board_server, capsys):
    test_provider.add_repo("acme/toy", model_files())
    server = board_server([{"id": 4, "source": "test:acme/toy"}])
    assert main(["--vault", str(vault), "estimate", BOARD_URL, "--dry-run"]) == 0
    assert server.pushed == []


def test_archive_next_claims_reports_and_finishes(
    vault, test_provider, board_server, capsys
):
    test_provider.add_repo("acme/toy", model_files())
    server = board_server([{"id": 4, "source": "test:acme/toy", "desire": 6}])
    assert (
        main(["--vault", str(vault), "archive", "--next", BOARD_URL, "--jobs", "1"])
        == 0
    )
    out = capsys.readouterr().out
    assert "[claimed as" in out
    assert "Board" in out and "done" in out
    states = [c["state"] for c in server.claims if c["entry_id"] == 4]
    assert states[0] == "archiving"
    assert states[-1] == "done"
    assert server.claims[-1]["percent"] == 100
    assert list(vault.glob("*/*/manifest.json"))


def test_archive_next_skips_rows_claimed_by_others(
    vault, test_provider, board_server, capsys
):
    test_provider.add_repo("acme/one", model_files())
    test_provider.add_repo("acme/two", model_files())
    server = board_server(
        [
            {"id": 1, "source": "test:acme/one", "desire": 9},
            {"id": 2, "source": "test:acme/two", "desire": 5},
        ]
    )
    server.claimed_by[1] = {
        "client": "usb-carrier",
        "state": "archiving",
        "percent": 40,
    }
    assert (
        main(["--vault", str(vault), "archive", "--next", BOARD_URL, "--jobs", "1"])
        == 0
    )
    out = capsys.readouterr().out
    assert "claimed by usb-carrier (40%)" in out
    done = [c for c in server.claims if c["state"] == "done"]
    assert len(done) == 1 and done[0]["entry_id"] == 2


def test_archive_next_skips_rows_the_board_marks_have(
    vault, test_provider, board_server, capsys
):
    """The board's checkmark wins even when this vault has nothing.

    Board status never enters catalog.json, so the local overlay alone
    would re-fetch a row someone else finished or checked off.
    """
    test_provider.add_repo("acme/one", model_files())
    test_provider.add_repo("acme/two", model_files())
    server = board_server(
        [
            {"id": 1, "source": "test:acme/one", "desire": 9, "status": "have"},
            {"id": 2, "source": "test:acme/two", "desire": 5},
        ]
    )
    assert (
        main(["--vault", str(vault), "archive", "--next", BOARD_URL, "--jobs", "1"])
        == 0
    )
    done = [c for c in server.claims if c["state"] == "done"]
    assert len(done) == 1 and done[0]["entry_id"] == 2
    assert not any(c["entry_id"] == 1 for c in server.claims)


def test_archive_next_idles_when_rows_are_had_or_held(
    vault, test_provider, board_server, capsys
):
    test_provider.add_repo("acme/one", model_files())
    test_provider.add_repo("acme/two", model_files())
    server = board_server(
        [
            {"id": 1, "source": "test:acme/one", "status": "have"},
            {"id": 2, "source": "test:acme/two"},
        ]
    )
    server.claimed_by[2] = {
        "client": "usb-carrier",
        "state": "archiving",
        "percent": 40,
    }
    assert main(["--vault", str(vault), "archive", "--next", BOARD_URL]) == 0
    err = capsys.readouterr().err
    assert "1 checked off as have on the board" in err
    assert "1 claimed by another client" in err
    done = [c for c in server.claims if c["state"] == "done"]
    assert done == []
    assert not list(vault.glob("*/*/manifest.json"))


def test_archive_next_dry_run_releases_the_claim(
    vault, test_provider, board_server, capsys
):
    test_provider.add_repo("acme/toy", model_files())
    server = board_server([{"id": 4, "source": "test:acme/toy"}])
    assert (
        main(["--vault", str(vault), "archive", "--next", BOARD_URL, "--dry-run"]) == 0
    )
    assert server.releases == [4]
    assert 4 not in server.claimed_by


def test_catalog_add_and_drop_push_back(vault, test_provider, board_server, capsys):
    test_provider.add_repo("acme/toy", model_files())
    server = board_server([{"id": 4, "source": "test:acme/toy"}])
    assert (
        main(
            [
                "--vault",
                str(vault),
                "catalog",
                "add",
                BOARD_URL,
                "test:acme/extra",
                "--desire",
                "5",
            ]
        )
        == 0
    )
    out = capsys.readouterr().out
    assert f"Pushed to {BOARD_URL}" in out
    sources = {e["source"] for e in server.pushed[-1]["entries"]}
    assert "test:acme/extra" in sources

    assert (
        main(["--vault", str(vault), "catalog", "drop", BOARD_URL, "test:acme/toy"])
        == 0
    )
    sources = {e["source"] for e in server.pushed[-1]["entries"]}
    assert "test:acme/toy" not in sources


def test_list_overlays_a_board_read_only(vault, test_provider, board_server, capsys):
    test_provider.add_repo("acme/toy", model_files())
    server = board_server([{"id": 4, "source": "test:acme/toy", "desire": 6}])
    assert main(["--vault", str(vault), "list", BOARD_URL]) == 0
    out = capsys.readouterr().out
    assert "test:acme/toy" in out
    assert server.pushed == []  # read-only


def test_unwired_verbs_explain_board_urls(vault, test_provider):
    with pytest.raises(SystemExit, match="board URL works with"):
        main(["--vault", str(vault), "catalog", "regen", BOARD_URL])


def test_archive_next_releases_claim_when_archive_refuses(
    vault, test_provider, board_server, capsys
):
    """A pre-transfer refusal (gated repo) must hand the claim back."""
    test_provider.add_repo("acme/locked", model_files(), access_denied=True)
    server = board_server([{"id": 9, "source": "test:acme/locked", "desire": 7}])
    with pytest.raises(SystemExit):
        main(["--vault", str(vault), "archive", "--next", BOARD_URL, "--jobs", "1"])
    assert server.releases == [9]
    assert 9 not in server.claimed_by


def test_archive_board_flag_claims_the_chosen_source(
    vault, test_provider, board_server, capsys
):
    test_provider.add_repo("acme/one", model_files())
    test_provider.add_repo("acme/two", model_files())
    server = board_server(
        [
            {"id": 1, "source": "test:acme/one", "desire": 9},
            {"id": 2, "source": "test:acme/two", "desire": 5},
        ]
    )
    # The chosen source wins over the board's priority row.
    assert (
        main(
            [
                "--vault",
                str(vault),
                "archive",
                "test:acme/two",
                "--board",
                BOARD_URL,
                "--jobs",
                "1",
            ]
        )
        == 0
    )
    out = capsys.readouterr().out
    assert "Claimed test:acme/two" in out
    states = [c["state"] for c in server.claims if c["entry_id"] == 2]
    assert states[0] == "archiving" and states[-1] == "done"
    assert not any(c["entry_id"] == 1 for c in server.claims)


def test_archive_board_flag_warns_off_board_and_refuses_taken_rows(
    vault, test_provider, board_server, capsys
):
    test_provider.add_repo("acme/offboard", model_files())
    test_provider.add_repo("acme/one", model_files())
    server = board_server([{"id": 1, "source": "test:acme/one"}])
    assert (
        main(
            [
                "--vault",
                str(vault),
                "archive",
                "test:acme/offboard",
                "--board",
                BOARD_URL,
                "--jobs",
                "1",
            ]
        )
        == 0
    )
    err = capsys.readouterr().err
    assert "not a row on" in err
    assert server.claims == []

    server.claimed_by[1] = {
        "client": "usb-carrier",
        "state": "archiving",
        "percent": 10,
    }
    with pytest.raises(SystemExit, match="claimed by usb-carrier"):
        main(
            [
                "--vault",
                str(vault),
                "archive",
                "test:acme/one",
                "--board",
                BOARD_URL,
                "--jobs",
                "1",
            ]
        )


def test_archive_board_flag_refetches_a_have_row_deliberately(
    vault, test_provider, board_server, capsys
):
    """Naming the source is the deliberate act: the claim carries refetch
    and goes through even on a row the board checks off as have."""
    test_provider.add_repo("acme/one", model_files())
    server = board_server([{"id": 1, "source": "test:acme/one", "status": "have"}])
    assert (
        main(
            [
                "--vault",
                str(vault),
                "archive",
                "test:acme/one",
                "--board",
                BOARD_URL,
                "--jobs",
                "1",
            ]
        )
        == 0
    )
    err = capsys.readouterr().err
    assert "re-fetching deliberately" in err
    states = [c["state"] for c in server.claims if c["entry_id"] == 1]
    assert states[0] == "archiving" and states[-1] == "done"
    assert all(c.get("refetch") for c in server.claims if c["state"] == "archiving")


def test_archive_board_flag_dry_run_releases(vault, test_provider, board_server):
    test_provider.add_repo("acme/one", model_files())
    server = board_server([{"id": 1, "source": "test:acme/one"}])
    assert (
        main(
            [
                "--vault",
                str(vault),
                "archive",
                "test:acme/one",
                "--board",
                BOARD_URL,
                "--dry-run",
            ]
        )
        == 0
    )
    assert server.releases == [1]

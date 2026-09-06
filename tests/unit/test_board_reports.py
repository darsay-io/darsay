"""The panel, posted to a claimed board row — no network, a fake clock."""

from __future__ import annotations

import pytest

from darsay import __version__
from darsay.board import (
    Board,
    ProgressReporter,
    panel_phase,
    report_from_snapshot,
)
from darsay.progress import TransferMeter
from tests.fakes import Clock

BOARD = Board(origin="https://darsay.io", id="3b8cb153111534e3c468907ded2a50f7")
GiB = 1024**3


def _snapshot(**over) -> dict:
    snap = {
        "fraction": 0.62,
        "done_bytes": 398 * GiB,
        "total_bytes": 642 * GiB,
        "remaining_bytes": 244 * GiB,
        "rate": 88_300_000.7,
        "rate_history": [float(60e6 + i * 1e6) for i in range(30)],
        "eta_seconds": 2960.4,
        "stalled": False,
        "verifying": False,
        "hash_rate": None,
        "files_done": 91,
        "files_total": 163,
        "elapsed": 4812.9,
        "session_bytes": 401 * GiB,
        "budget_bytes": None,
        "budget_used": 0,
        "interrupted": False,
        "link": None,
        "retry": None,
        "max_rate": None,
        "disk_free": None,
        "disk_floor": 0,
        "disk_short": False,
        "current": [
            {
                "path": "model-00001-of-00163.safetensors",
                "n": 10,
                "total": 100,
                "phase": "download",
            },
            {
                "path": "model-00042-of-00163.safetensors",
                "n": 612,
                "total": 1000,
                "phase": "download",
            },
        ],
    }
    snap.update(over)
    return snap


class FakeMeter:
    def __init__(self, snap: dict):
        self.snap = snap
        self.reads = 0

    def snapshot(self) -> dict:
        self.reads += 1
        return dict(self.snap)


class FakePost:
    """Stands in for ``board.claim``: records reports, can refuse or fail."""

    def __init__(self):
        self.calls: list[dict] = []
        self.answer: tuple[bool, dict] = (True, {})
        self.raise_next: Exception | None = None

    def __call__(self, board, entry_id, client, **kwargs):
        if self.raise_next is not None:
            exc, self.raise_next = self.raise_next, None
            raise exc
        self.calls.append(
            {"board": board, "entry_id": entry_id, "client": client, **kwargs}
        )
        return self.answer


def test_panel_phase_mirrors_the_panels_status_word():
    assert panel_phase(_snapshot()) == "downloading"
    assert panel_phase(_snapshot(link={"state": "offline"})) == "offline"
    assert panel_phase(_snapshot(retry={"count": 2, "since": 9})) == "retrying"
    assert panel_phase(_snapshot(verifying=True)) == "verifying"
    assert panel_phase(_snapshot(stalled=True)) == "stalled"
    assert panel_phase(_snapshot(eta_seconds=None, rate=None)) == "starting"
    assert panel_phase(_snapshot(eta_seconds=None, rate=0.0)) == "starting"
    # An ETA held from earlier keeps a quiet moment from reading as a fresh start.
    assert panel_phase(_snapshot(rate=0.0)) == "downloading"


def test_report_from_snapshot_carries_the_panels_figures_as_whole_numbers():
    report = report_from_snapshot(_snapshot())
    assert report["phase"] == "downloading"
    assert report["percent"] == 61
    assert report["banked_bytes"] == 398 * GiB
    assert report["total_bytes"] == 642 * GiB
    assert report["rate_bps"] == 88_300_000
    assert report["eta_seconds"] == 2960
    assert report["files_done"] == 91
    assert report["files_total"] == 163
    assert report["elapsed_seconds"] == 4812
    assert report["session_bytes"] == 401 * GiB
    assert report["agent"] == f"darsay {__version__}"
    # The sparkline history is capped to what the panel keeps, newest last.
    assert len(report["rates"]) == 24
    assert report["rates"][-1] == int(60e6 + 29e6)
    # The file in flight is the largest of several.
    assert report["current"] == {
        "path": "model-00042-of-00163.safetensors",
        "done": 612,
        "total": 1000,
    }
    assert "state" not in report  # the poster says the state


def test_report_from_snapshot_leaves_out_what_the_panel_does_not_know():
    report = report_from_snapshot(
        _snapshot(
            total_bytes=0,
            done_bytes=0,
            rate=None,
            eta_seconds=None,
            rate_history=[],
            current=[],
        )
    )
    assert "percent" not in report
    assert "rate_bps" not in report
    assert "eta_seconds" not in report
    assert "rates" not in report
    assert "current" not in report
    assert report["phase"] == "starting"


def test_report_from_a_real_meter_is_well_formed():
    meter = TransferMeter(
        total_bytes=1000,
        total_files=4,
        verified_bytes=250,
        verified_files=1,
        partial_bytes=0,
        session={"bytes_network": 0, "bytes_local_sources": 0, "files_completed": 0},
        clock=Clock(),
    )
    report = report_from_snapshot(meter.snapshot())
    assert report["percent"] == 25
    assert report["files_done"] == 1
    assert report["files_total"] == 4
    assert report["phase"] == "starting"


def test_reporter_posts_at_once_then_only_when_something_moved():
    clock = Clock(100.0)
    post = FakePost()
    meter = FakeMeter(_snapshot())
    reporter = ProgressReporter(
        BOARD, 7, "amber-heron-3f", interval=60, heartbeat=300, post=post, clock=clock
    )

    assert reporter.tick(meter) is True  # the first read goes straight out
    assert post.calls[-1]["entry_id"] == 7
    assert post.calls[-1]["client"] == "amber-heron-3f"
    assert post.calls[-1]["state"] == "archiving"
    assert post.calls[-1]["facts"]["percent"] == 61

    clock.t += 60
    assert reporter.tick(meter) is False  # same percent, same word: nothing new
    meter.snap["done_bytes"] = 410 * GiB
    clock.t += 60
    assert reporter.tick(meter) is True  # a whole percent passed
    assert post.calls[-1]["facts"]["percent"] == 63
    meter.snap["stalled"] = True
    clock.t += 60
    assert reporter.tick(meter) is True  # the panel's word changed
    assert post.calls[-1]["facts"]["phase"] == "stalled"
    assert reporter.sent == 3


def test_reporter_heartbeats_when_nothing_moves():
    clock = Clock(0.0)
    post = FakePost()
    meter = FakeMeter(_snapshot())
    reporter = ProgressReporter(
        BOARD, 7, "c", interval=60, heartbeat=300, post=post, clock=clock
    )
    assert reporter.tick(meter) is True
    for _ in range(4):
        clock.t += 60
        assert reporter.tick(meter) is False
    clock.t += 60  # five minutes of silence would say we were gone
    assert reporter.tick(meter) is True
    assert reporter.sent == 2


def test_reporter_never_fails_the_archive_and_warns_once():
    clock = Clock(0.0)
    post = FakePost()
    meter = FakeMeter(_snapshot())
    notes: list[str] = []
    reporter = ProgressReporter(
        BOARD, 7, "c", interval=60, heartbeat=300, post=post, clock=clock
    )
    post.raise_next = SystemExit("error: cannot reach the board")
    assert reporter.tick(meter, notes.append) is False
    assert len(notes) == 1
    assert "could not be updated" in notes[0]
    assert "the archive continues" in notes[0]
    post.raise_next = OSError("still down")
    clock.t += 60
    assert reporter.tick(meter, notes.append) is False
    assert len(notes) == 1  # said once
    clock.t += 60
    assert reporter.tick(meter, notes.append) is True  # back; reported, no fuss
    assert len(notes) == 1


def test_reporter_stops_when_the_board_says_the_row_is_someone_elses():
    clock = Clock(0.0)
    post = FakePost()
    post.answer = (False, {"client": "usb-carrier"})
    meter = FakeMeter(_snapshot())
    notes: list[str] = []
    reporter = ProgressReporter(
        BOARD, 7, "c", interval=60, heartbeat=300, post=post, clock=clock
    )
    assert reporter.tick(meter, notes.append) is False
    assert "another client holds this row" in notes[0]
    post.answer = (True, {})
    clock.t += 600
    # Stopped: a later tick reads nothing and posts nothing.
    assert reporter.tick(meter, notes.append) is False
    assert len(post.calls) == 1


def test_reporter_watch_runs_a_thread_and_stop_joins_it():
    post = FakePost()
    meter = FakeMeter(_snapshot())
    reporter = ProgressReporter(BOARD, 7, "c", interval=0.01, heartbeat=0.02, post=post)
    release = reporter.watch(meter, lambda *_: None)
    deadline = 200
    while len(post.calls) < 2 and deadline:
        import time

        time.sleep(0.005)
        deadline -= 1
    release()
    assert len(post.calls) >= 2
    seen = len(post.calls)
    import time

    time.sleep(0.03)
    assert len(post.calls) == seen  # nothing after stop


def test_reporter_with_zero_interval_never_starts():
    post = FakePost()
    meter = FakeMeter(_snapshot())
    reporter = ProgressReporter(BOARD, 7, "c", interval=0, post=post)
    release = reporter.watch(meter)
    release()
    assert post.calls == []
    assert meter.reads == 0


@pytest.mark.parametrize("interval", [60, 5.0])
def test_reporter_heartbeat_never_shorter_than_interval(interval):
    reporter = ProgressReporter(BOARD, 1, "c", interval=interval, heartbeat=1)
    assert reporter.heartbeat == interval

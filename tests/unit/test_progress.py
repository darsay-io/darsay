from __future__ import annotations

import io
import sys
import types

import pytest

from darsay.progress import (
    TransferDisplay,
    TransferMeter,
    _LineProxy,
    _size_field_width,
    color_enabled,
    format_percent,
    human_duration,
    human_eta,
    human_rate,
    meter_from_plan,
    progress_disabled,
    progress_setting,
    render_bar,
    render_sparkline,
    snapshot_lines,
    snapshot_log_line,
    styled_bar,
)
from tests.fakes import Clock


def _meter(**kwargs) -> TransferMeter:
    session = kwargs.pop("session", None) or {
        "bytes_network": 0,
        "bytes_local_sources": 0,
        "files_completed": 0,
    }
    clock = kwargs.pop("clock", None) or Clock()
    return TransferMeter(
        total_bytes=kwargs.get("total_bytes", 1000),
        total_files=kwargs.get("total_files", 4),
        verified_bytes=kwargs.get("verified_bytes", 0),
        verified_files=kwargs.get("verified_files", 0),
        partial_bytes=kwargs.get("partial_bytes", 0),
        session=session,
        files_completed_base=kwargs.get("files_completed_base", 0),
        budget_bytes=kwargs.get("budget_bytes"),
        clock=clock,
    )


def test_human_duration_and_eta_are_conversational():
    assert human_duration(8) == "8s"
    assert human_duration(90) == "1 min 30s"
    assert human_duration(12 * 60) == "12 min"
    assert human_duration(4 * 3600 + 12 * 60) == "4h 12 min"
    assert human_duration(3 * 86400) == "3d"
    assert human_eta(None) == "starting"
    assert human_eta(0.4) == "almost done"
    assert human_eta(45).endswith("left")
    assert human_eta(99, stalled=True) == "stalled"


def test_human_rate_and_percent():
    assert human_rate(None) == "-"
    assert human_rate(0) == "0 B/s"
    assert human_rate(12.4 * 1024 * 1024).endswith("/s")
    assert "MiB" in human_rate(12.4 * 1024 * 1024)
    assert format_percent(None) == "  --.-%"
    assert format_percent(0.421).strip() == "42.1%"
    assert format_percent(1.0).strip() == "100.0%"


def test_render_bar_bounds_and_partial_cell():
    assert render_bar(None, 10) == "░" * 10
    assert render_bar(0, 10) == "░" * 10
    assert render_bar(1, 10) == "█" * 10
    mid = render_bar(0.5, 10)
    assert len(mid) == 10
    assert mid.startswith("█")
    assert mid.endswith("░")
    assert render_bar(0.42, 0) == ""


def test_styled_bar_matches_render_bar_and_paints_fill():
    assert styled_bar(0.5, 10, color=False) == render_bar(0.5, 10)
    colored = styled_bar(0.5, 10, color=True)
    assert "\033[" in colored
    stripped = colored
    for code in ("\033[38;2;34;211;238m", "\033[96m", "\033[2m", "\033[0m"):
        stripped = stripped.replace(code, "")
    assert stripped == render_bar(0.5, 10)


def test_sparkline_pads_and_scales():
    assert render_sparkline([], 8) == "·" * 8
    line = render_sparkline([1, 2, 4, 8], 8)
    assert len(line) == 8
    assert line.startswith("·")
    assert line[-1] == "█"


def test_snapshot_lines_are_three_glanceable_rows():
    snap = {
        "fraction": 0.421,
        "done_bytes": int(21.8 * 1024**3),
        "total_bytes": int(51.8 * 1024**3),
        "rate": 14.2 * 1024**2,
        "rate_history": [8e6, 10e6, 12e6, 14e6],
        "eta_seconds": 34 * 60,
        "stalled": False,
        "files_done": 8,
        "files_total": 32,
        "elapsed": 18 * 60,
        "current": [
            {
                "path": "model-00004-of-00012.safetensors",
                "n": int(2.1 * 1024**3),
                "total": int(4.6 * 1024**3),
                "phase": "download",
            }
        ],
    }
    lines = snapshot_lines(snap, width=80, color=False)
    assert len(lines) == 3
    joined = "\n".join(lines)
    assert "42.1%" in joined
    assert "GiB" in joined
    assert "MiB/s" in joined
    assert "left" in joined
    assert "8/32 files" in joined
    assert "model-00004-of-00012.safetensors" in joined
    assert "\033[" not in joined


def test_snapshot_lines_hashing_shows_intra_file_percent():
    snap = {
        "fraction": 0.4,
        "done_bytes": 400,
        "total_bytes": 1000,
        "rate": 50,
        "rate_history": [],
        "eta_seconds": 12,
        "stalled": False,
        "files_done": 1,
        "files_total": 3,
        "elapsed": 10,
        "current": [
            {
                "path": "weights.safetensors",
                "n": 400,
                "total": 1000,
                "phase": "hashing",
            }
        ],
    }
    joined = "\n".join(snapshot_lines(snap, width=80, color=False))
    assert "hashing weights.safetensors" in joined
    assert "40.0%" in joined


def test_meter_note_hash_bytes_advances_the_bar():
    session = {"bytes_network": 0, "bytes_local_sources": 0, "files_completed": 0}
    meter = _meter(session=session, total_bytes=1000, total_files=1)
    meter.begin_hash("weights.bin", 1000)
    meter.note_hash_bytes("weights.bin", 400, 1000)
    snap = meter.snapshot()
    assert snap["done_bytes"] == 400
    assert snap["current"][0]["phase"] == "hashing"
    assert snap["current"][0]["n"] == 400
    meter.finish_hash("weights.bin", 1000)
    snap = meter.snapshot()
    assert snap["done_bytes"] == 1000
    assert snap["files_done"] == 1
    assert snap["current"] == []


def test_snapshot_lines_hashing_and_budget():
    snap = {
        "fraction": 0.9,
        "done_bytes": 900,
        "total_bytes": 1000,
        "rate": 50,
        "rate_history": [],
        "eta_seconds": 2,
        "stalled": False,
        "files_done": 3,
        "files_total": 4,
        "elapsed": 10,
        "budget_bytes": 500,
        "budget_used": 200,
        "current": [
            {"path": "weights.safetensors", "n": 900, "total": 900, "phase": "hashing"}
        ],
    }
    joined = "\n".join(snapshot_lines(snap, width=80, color=False))
    assert "hashing weights.safetensors" in joined
    assert "budget" in joined
    assert "200 B / 500 B" in joined


def test_snapshot_lines_unknown_total_and_parallel_files():
    unknown = snapshot_lines(
        {
            "fraction": None,
            "done_bytes": 40,
            "total_bytes": 0,
            "rate": 10,
            "rate_history": [],
            "eta_seconds": None,
            "stalled": False,
            "files_done": 1,
            "files_total": 0,
            "elapsed": 1,
            "current": [{"path": "a.bin", "n": 40, "total": None, "phase": "download"}],
        },
        width=80,
        color=False,
    )
    assert len(unknown) == 3
    assert "downloaded" in "\n".join(unknown)
    assert "--.-%" in unknown[0]
    parallel = snapshot_lines(
        {
            "fraction": 0.1,
            "done_bytes": 10,
            "total_bytes": 100,
            "rate": 10,
            "rate_history": [],
            "eta_seconds": 9,
            "stalled": False,
            "files_done": 0,
            "files_total": 4,
            "elapsed": 1,
            "current": [
                {"path": "tiny.json", "n": 2, "total": 4, "phase": "download"},
                {
                    "path": "weights.safetensors",
                    "n": 8,
                    "total": 80,
                    "phase": "download",
                },
            ],
        },
        width=80,
        color=False,
    )
    joined = "\n".join(parallel)
    assert "2 in flight" in joined
    assert "weights.safetensors" in joined


def test_snapshot_lines_truncates_long_names():
    path = "subdir/" + ("very-long-shard-name-" * 8) + ".safetensors"
    lines = snapshot_lines(
        {
            "fraction": 0.5,
            "done_bytes": 50,
            "total_bytes": 100,
            "rate": 10,
            "rate_history": [],
            "eta_seconds": 5,
            "stalled": False,
            "files_done": 0,
            "files_total": 1,
            "elapsed": 1,
            "current": [{"path": path, "n": 50, "total": 100, "phase": "download"}],
        },
        width=60,
        color=False,
    )
    assert path not in lines[2]
    assert "…" in lines[2]
    assert lines[2].endswith("safetensors") or "safetensors" in lines[2]


def test_snapshot_log_line_is_single_line():
    line = snapshot_log_line(
        {
            "fraction": 0.5,
            "done_bytes": 50,
            "total_bytes": 100,
            "rate": 10,
            "eta_seconds": 5,
            "stalled": False,
            "files_done": 1,
            "files_total": 2,
            "current": [{"path": "a.bin", "n": 50, "total": 100, "phase": "download"}],
        }
    )
    assert "\n" not in line
    assert "50.0%" in line
    assert "a.bin" in line
    assert "1/2 files" in line


def test_meter_counts_resume_plus_session_bytes():
    session = {"bytes_network": 0, "bytes_local_sources": 0, "files_completed": 2}
    clock = Clock(10.0)
    meter = _meter(
        total_bytes=1000,
        total_files=4,
        verified_bytes=400,
        verified_files=2,
        partial_bytes=50,
        session=session,
        files_completed_base=2,
        clock=clock,
    )
    snap = meter.snapshot()
    assert snap["done_bytes"] == 450
    assert snap["files_done"] == 2
    session["bytes_network"] = 100
    clock.t = 12.0
    meter.note()
    clock.t = 14.0
    session["bytes_network"] = 250
    meter.note()
    snap = meter.snapshot()
    assert snap["done_bytes"] == 700
    assert snap["rate"] is not None and snap["rate"] > 0
    assert snap["eta_seconds"] is not None
    session["files_completed"] = 3
    session["bytes_local_sources"] = 50
    snap = meter.snapshot()
    assert snap["files_done"] == 3
    assert snap["done_bytes"] == 750


def test_meter_pairs_current_file_with_tqdm_bar():
    meter = _meter()
    meter.set_current("shard.bin", 200)

    class Bar:
        n = 80
        total = 200

    meter.attach_bar(Bar(), "truncated")
    current = meter.snapshot()["current"]
    assert current == [
        {"path": "shard.bin", "n": 80, "total": 200, "phase": "download"}
    ]
    meter.set_current("shard.bin", 200, phase="hashing")
    assert meter.snapshot()["current"][0]["phase"] == "hashing"
    meter.clear_current("shard.bin")
    assert meter.snapshot()["current"] == []


def test_meter_marks_stalled_when_bytes_stop():
    session = {"bytes_network": 10, "bytes_local_sources": 0, "files_completed": 0}
    clock = Clock(1.0)
    meter = _meter(session=session, clock=clock, total_bytes=1000)
    meter.note()
    clock.t = 20.0
    snap = meter.snapshot()
    assert snap["stalled"] is True
    assert snap["eta_seconds"] is None


def test_meter_from_plan_and_budget():
    plan = {
        "bytes": {"total": 100, "verified": 20, "partial": 5},
        "files": {"total": 3, "verified": 1},
    }
    session = {"bytes_network": 0, "bytes_local_sources": 0, "files_completed": 1}

    class Stop:
        max_bytes = 50

    meter = meter_from_plan(plan, session, Stop())
    snap = meter.snapshot()
    assert snap["done_bytes"] == 25
    assert snap["budget_bytes"] == 50


def test_display_live_paints_and_restores(monkeypatch):
    session = {"bytes_network": 250, "bytes_local_sources": 0, "files_completed": 1}
    meter = _meter(
        total_bytes=1000,
        verified_bytes=0,
        session=session,
        clock=Clock(5.0),
    )
    buf = io.StringIO()
    logs: list[str] = []
    display = TransferDisplay(
        meter, progress=logs.append, stream=buf, live=True, color=False
    )
    display.start()
    display.refresh()
    painted = buf.getvalue()
    assert "25.0%" in painted
    assert "\033[?25l" in painted  # cursor hidden
    display.stop()
    assert "\033[?25h" in buf.getvalue()  # cursor restored
    display.echo("lane 1")
    assert logs == ["lane 1"]


def test_display_non_tty_emits_log_line():
    session = {"bytes_network": 10, "bytes_local_sources": 0, "files_completed": 0}
    meter = _meter(session=session, total_bytes=100)
    logs: list[str] = []
    display = TransferDisplay(
        meter, progress=logs.append, stream=io.StringIO(), live=False, color=False
    )
    display.refresh()
    assert logs
    assert "10.0%" in logs[0]


def test_progress_env_and_color(monkeypatch):
    monkeypatch.setenv("DARSAY_PROGRESS", "0")
    assert progress_disabled() is True
    assert progress_setting() == "off"
    monkeypatch.setenv("DARSAY_PROGRESS", "line")
    assert progress_disabled() is False
    assert progress_setting() == "line"
    monkeypatch.setenv("DARSAY_PROGRESS", "")
    assert progress_setting() == "auto"
    stream = io.StringIO()
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.delenv("FORCE_COLOR", raising=False)
    assert color_enabled(stream) is False
    monkeypatch.delenv("NO_COLOR")
    monkeypatch.setenv("FORCE_COLOR", "1")
    assert color_enabled(stream) is True


class _TTY(io.StringIO):
    def isatty(self):
        return True


def test_line_mode_wins_over_tty(monkeypatch):
    monkeypatch.setenv("DARSAY_PROGRESS", "line")
    logs: list[str] = []
    display = TransferDisplay(
        _meter(), progress=logs.append, stream=_TTY(), color=False
    )
    assert display.live is False
    assert display._log is True
    display.refresh()
    assert logs and "%" in logs[0]


def test_columns_stay_fixed_across_digit_rollovers():
    base = {
        "total_bytes": int(305.8 * 1024**3),
        "rate_history": [8e6, 10e6, 12e6],
        "eta_seconds": 12 * 3600,
        "stalled": False,
        "files_done": 11,
        "files_total": 72,
        "elapsed": 43,
        "current": [
            {
                "path": "model-00061-of-00062.safetensors",
                "n": int(2.8 * 1024**3),
                "total": int(4.9 * 1024**3),
                "phase": "download",
            }
        ],
    }
    before = snapshot_lines(
        {
            **base,
            "fraction": 0.013,
            "done_bytes": int(4.0 * 1024**3),
            "rate": 9.9 * 1024**2,
        },
        width=100,
        color=False,
    )
    after = snapshot_lines(
        {
            **base,
            "fraction": 0.145,
            "done_bytes": int(44.4 * 1024**3),
            "rate": 10.0 * 1024**2,
        },
        width=100,
        color=False,
    )
    assert [len(line) for line in before] == [len(line) for line in after]
    # The rate field is fixed-width, so everything after it stays put.
    assert before[1].index("left") == after[1].index("left")
    assert before[0].index("/") == after[0].index("/")


def test_size_field_width_covers_unit_rollovers():
    total = int(305.8 * 1024**3)
    width = _size_field_width(total)
    assert width >= len("1024.0 MiB")
    assert _size_field_width(500) == len("500 B")


def test_rate_history_advances_on_a_clock_not_per_chunk():
    session = {"bytes_network": 0, "bytes_local_sources": 0, "files_completed": 0}
    clock = Clock(0.0)
    meter = _meter(session=session, clock=clock, total_bytes=10**9)
    meter.note()
    for _ in range(50):
        clock.t += 0.05
        session["bytes_network"] += 1_000_000
        meter.note()
    # 2.5s of very chatty callbacks: at most one sparkline point.
    assert len(meter.snapshot()["rate_history"]) <= 1
    clock.t = 6.0
    session["bytes_network"] += 1_000_000
    meter.note()
    assert len(meter.snapshot()["rate_history"]) == 2


def test_interrupted_snapshot_shows_stopping():
    stop = types.SimpleNamespace(interrupted=True, max_bytes=None)
    session = {"bytes_network": 100, "bytes_local_sources": 0, "files_completed": 0}
    meter = TransferMeter(
        total_bytes=1000,
        total_files=4,
        verified_bytes=0,
        verified_files=0,
        partial_bytes=0,
        session=session,
        stop_controller=stop,
        clock=Clock(3.0),
    )
    snap = meter.snapshot()
    assert snap["interrupted"] is True
    lines = snapshot_lines(snap, width=80, color=False)
    assert "stopping" in lines[1]


def test_display_announces_interrupt_once():
    stop = types.SimpleNamespace(interrupted=True, max_bytes=None)
    session = {"bytes_network": 10, "bytes_local_sources": 0, "files_completed": 0}
    meter = TransferMeter(
        total_bytes=100,
        total_files=1,
        verified_bytes=0,
        verified_files=0,
        partial_bytes=0,
        session=session,
        stop_controller=stop,
    )
    logs: list[str] = []
    display = TransferDisplay(
        meter, progress=logs.append, stream=io.StringIO(), live=False, color=False
    )
    display.refresh()
    display.refresh()
    notices = [line for line in logs if "Interrupt received" in line]
    assert len(notices) == 1


def test_emit_above_prints_line_then_repaints_panel():
    session = {"bytes_network": 250, "bytes_local_sources": 0, "files_completed": 1}
    meter = _meter(total_bytes=1000, session=session, clock=Clock(5.0))
    buf = io.StringIO()
    display = TransferDisplay(
        meter, progress=lambda *a, **k: None, stream=buf, live=True, color=False
    )
    display.start()
    display.refresh()
    display.emit_above("Reclaiming stale transfer lock")
    tail = buf.getvalue().split("Reclaiming stale transfer lock\n", 1)[1]
    assert "25.0%" in tail  # the panel came back below the message
    display.stop()


def test_line_proxy_buffers_partial_lines():
    meter = _meter()
    display = TransferDisplay(
        meter,
        progress=lambda *a, **k: None,
        stream=io.StringIO(),
        live=True,
        color=False,
    )
    real = io.StringIO()
    proxy = _LineProxy(display, real)
    proxy.write("partial")
    assert real.getvalue() == ""
    proxy.write(" line\nnext")
    assert "partial line\n" in real.getvalue()
    assert "next" not in real.getvalue()
    proxy.drain()
    assert "next\n" in real.getvalue()
    assert proxy.isatty() is True


def test_display_stop_leaves_final_record_line():
    session = {"bytes_network": 250, "bytes_local_sources": 0, "files_completed": 1}
    meter = _meter(total_bytes=1000, session=session, clock=Clock(5.0))
    buf = io.StringIO()
    display = TransferDisplay(
        meter, progress=lambda *a, **k: None, stream=buf, live=True, color=False
    )
    display.start()
    display.refresh()
    display.stop()
    after_restore = buf.getvalue().rsplit("\033[?25h", 1)[1]
    assert "elapsed" in after_restore
    assert "25.0%" in after_restore


def test_progress_off_emits_nothing(monkeypatch):
    monkeypatch.setenv("DARSAY_PROGRESS", "0")
    logs: list[str] = []
    buf = _TTY()
    display = TransferDisplay(_meter(), progress=logs.append, stream=buf, color=False)
    assert display.live is False
    assert display._log is False
    display.start()
    display.refresh()
    display.stop()
    assert logs == []
    assert buf.getvalue() == ""


def _offline_snap(**overrides) -> dict:
    snap = {
        "fraction": 0.002,
        "done_bytes": int(1.3 * 1024**3),
        "total_bytes": int(703.8 * 1024**3),
        "rate": 0.0,
        "rate_history": [14e6, 14e6, 8e6, 2e6, 0.0],
        "eta_seconds": None,
        "stalled": True,
        "files_done": 12,
        "files_total": 153,
        "elapsed": 190,
        "link": {
            "state": "offline",
            "since": 130.0,
            "retry_in": 8.0,
            "attempts": 5,
            "reason": "DNS lookup failed",
        },
        "current": [
            {
                "path": "model-00141-of-00141.safetensors",
                "n": int(1.2 * 1024**3),
                "total": int(4.4 * 1024**3),
                "phase": "download",
            }
        ],
    }
    snap.update(overrides)
    return snap


def test_offline_state_owns_the_eta_slot_and_tail():
    from darsay.progress import status_text

    lines = snapshot_lines(_offline_snap(), width=90, color=False)
    assert "offline" in lines[1]
    assert "retry in 8s · 2 min 10s offline" in lines[1]
    assert "stalled" not in lines[1]
    # The in-flight file keeps its banked bytes on screen while waiting.
    assert "1.2 GiB / 4.4 GiB" in lines[2]
    reconnecting = snapshot_lines(
        _offline_snap(
            link={
                "state": "reconnecting",
                "since": 132.0,
                "retry_in": None,
                "attempts": 6,
                "reason": "DNS lookup failed",
            }
        ),
        width=90,
        color=False,
    )
    assert "reconnecting" in reconnecting[1]
    assert "attempt 6 · 2 min 12s offline" in reconnecting[1]
    assert status_text(_offline_snap()) == "offline"
    # Interrupt wins over everything.
    assert "stopping" in status_text(_offline_snap(interrupted=True))


def test_waiting_states_are_amber_when_colored():
    colored = snapshot_lines(_offline_snap(), width=90, color=True)[1]
    assert "\033[33m" in colored or "\033[38;2;251;191;36m" in colored
    stalled = snapshot_lines(
        _offline_snap(link=None, stalled=True), width=90, color=True
    )[1]
    assert "stalled" in stalled
    assert "\033[33m" in stalled or "\033[38;2;251;191;36m" in stalled
    flowing = snapshot_lines(
        _offline_snap(link=None, stalled=False, rate=1e7, eta_seconds=600),
        width=90,
        color=True,
    )[1]
    assert "\033[33m" not in flowing and "\033[38;2;251;191;36m" not in flowing


def test_rate_cap_shows_in_the_tail_when_it_fits():
    snap = _offline_snap(link=None, stalled=False, rate=5e6, eta_seconds=600)
    snap["max_rate"] = 5 * 1024**2
    wide = snapshot_lines(snap, width=100, color=False)[1]
    assert wide.endswith("· cap 5.0 MiB/s")
    narrow = snapshot_lines(snap, width=60, color=False)[1]
    assert "cap" not in narrow
    assert len(narrow) <= 60
    # Never crowds the outage tail.
    offline = snapshot_lines({**_offline_snap(), "max_rate": 5 * 1024**2}, width=120)[1]
    assert "cap" not in offline


def test_log_line_reports_outage_and_cap():
    """The log line is the panel's fields in a row: the same words, once."""
    line = snapshot_log_line({**_offline_snap(), "max_rate": 5 * 1024**2})
    assert "offline  retry in 8s · 2 min 10s offline" in line
    assert "cap 5.0 MiB/s" in line
    assert "\n" not in line
    reconnecting = snapshot_log_line(
        _offline_snap(link={"state": "reconnecting", "since": 132.0, "attempts": 6})
    )
    assert "reconnecting  attempt 6 · 2 min 12s offline" in reconnecting


def test_meter_holds_last_eta_until_stalled():
    session = {"bytes_network": 0, "bytes_local_sources": 0, "files_completed": 0}
    clock = Clock(0.0)
    meter = _meter(session=session, clock=clock, total_bytes=10_000)
    for step in range(1, 6):
        clock.t = float(step)
        session["bytes_network"] = step * 100
        meter.note()
    flowing = meter.snapshot()
    assert flowing["eta_seconds"] is not None
    held = flowing["eta_seconds"]
    # Bytes stop: the smoothed rate decays, the ETA does not balloon.
    for tick in range(1, 12):
        clock.t = 5.0 + tick
        meter.note()
    quiet = meter.snapshot()
    assert quiet["stalled"] is False
    assert quiet["eta_seconds"] == pytest.approx(held)
    assert human_eta(quiet["eta_seconds"]) != "starting"
    clock.t = 25.0
    stalled = meter.snapshot()
    assert stalled["stalled"] is True
    assert stalled["eta_seconds"] is None


def test_meter_keeps_file_progress_across_a_reconnect():
    meter = _meter()
    meter.set_current("shard.bin", 200)

    class Bar:
        n = 80
        total = 200

    bar = Bar()
    meter.attach_bar(bar, "shard.bin")
    meter.detach_bar(bar)  # the Hub client closed its bar on the drop
    assert meter.snapshot()["current"][0]["n"] == 80
    meter.set_current("shard.bin", 200, phase="download")  # retry
    assert meter.snapshot()["current"][0]["n"] == 80


def test_meter_exposes_link_and_cap():
    class FakeLink:
        def snapshot(self):
            return {
                "state": "offline",
                "since": 3.0,
                "retry_in": 2.0,
                "attempts": 1,
                "reason": "timed out",
            }

    meter = TransferMeter(
        total_bytes=100,
        total_files=1,
        verified_bytes=0,
        verified_files=0,
        partial_bytes=0,
        session={"bytes_network": 0, "bytes_local_sources": 0, "files_completed": 0},
        link=FakeLink(),
        max_rate=4096,
    )
    snap = meter.snapshot()
    assert snap["link"]["state"] == "offline"
    assert snap["max_rate"] == 4096


def test_display_announces_each_link_transition_once():
    from darsay.transfer import Link

    session = {
        "bytes_network": 10,
        "bytes_local_sources": 0,
        "files_completed": 0,
        "reconnects": 0,
    }
    clock = Clock(0.0)
    link = Link(3600, session, clock=clock)
    meter = TransferMeter(
        total_bytes=100,
        total_files=1,
        verified_bytes=0,
        verified_files=0,
        partial_bytes=0,
        session=session,
        link=link,
        clock=clock,
    )
    logs: list[str] = []
    display = TransferDisplay(
        meter, progress=logs.append, stream=io.StringIO(), live=False, color=False
    )
    display.refresh()
    link.lost("a.bin", "DNS lookup failed", 0)
    display.refresh()
    display.refresh()
    lost = [line for line in logs if "Network unreachable" in line]
    assert len(lost) == 1
    assert "DNS lookup failed" in lost[0]
    assert "banked" in lost[0]
    clock.t = 75.0
    link.retrying("a.bin")
    link.online()
    display.refresh()
    display.refresh()
    back = [line for line in logs if line.startswith("Reconnected")]
    assert back == ["Reconnected after 1 min 15s (1 attempt)."]


def test_live_panel_reroutes_library_loggers(monkeypatch):
    """A StreamHandler bound to the real stderr before the panel started must
    print above the panel, not through it."""
    import logging

    real = _TTY()
    monkeypatch.setattr(sys, "stderr", real)
    monkeypatch.setattr(sys, "stdout", _TTY())
    logger = logging.getLogger("darsay.test.library")
    handler = logging.StreamHandler(real)
    logger.addHandler(handler)
    logger.propagate = False
    try:
        session = {"bytes_network": 250, "bytes_local_sources": 0, "files_completed": 1}
        meter = _meter(total_bytes=1000, session=session, clock=Clock(5.0))
        display = TransferDisplay(
            meter, progress=lambda *a, **k: None, stream=real, live=True, color=False
        )
        display.start()
        assert handler.stream is not real
        display.refresh()
        logger.warning("Xet Storage is enabled for this repo, but ...")
        text = real.getvalue()
        # The warning was emitted, the panel was cleared before it and
        # repainted after it.
        marker = "Xet Storage is enabled"
        assert marker in text
        after = text.split(marker, 1)[1]
        assert "25.0%" in after
        display.stop()
        assert handler.stream is real
    finally:
        logger.removeHandler(handler)


def test_human_eta_never_spells_out_an_absurd_estimate():
    from darsay.progress import _ETA_WIDTH

    assert human_eta(29 * 86400) == "29d left"
    assert human_eta(31 * 86400) == "> 30 days left"
    assert human_eta(272774 * 86400) == "> 30 days left"
    assert len("> 30 days left") <= _ETA_WIDTH


def test_meter_eta_is_paced_by_the_long_horizon():
    session = {"bytes_network": 0, "bytes_local_sources": 0, "files_completed": 0}
    clock = Clock(0.0)
    meter = _meter(session=session, clock=clock, total_bytes=10_000_000)
    # Under thirty seconds on record the short-window rate stands in.
    for step in range(1, 11):
        clock.t = float(step)
        session["bytes_network"] = step * 1000
        meter.note()
    early = meter.snapshot()
    assert early["eta_seconds"] == pytest.approx(
        early["remaining_bytes"] / early["rate"]
    )
    # A steady 1000 B/s for a minute, then one burst: the rate field
    # reports the burst, the ETA does not chase it.
    for step in range(11, 61):
        clock.t = float(step)
        session["bytes_network"] = step * 1000
        meter.note()
    clock.t = 61.0
    session["bytes_network"] = 61 * 1000 + 500_000
    meter.note()
    snap = meter.snapshot()
    assert snap["rate"] > 10_000
    naive = snap["remaining_bytes"] / snap["rate"]
    assert snap["eta_seconds"] > naive * 2
    # The horizon still reflects what actually moved, not the old pace only.
    assert snap["eta_seconds"] < snap["remaining_bytes"] / 1000


def test_meter_retry_state_shows_until_bytes_arrive(monkeypatch):
    from darsay.progress import status_text

    monkeypatch.delenv("COLORTERM", raising=False)
    session = {"bytes_network": 0, "bytes_local_sources": 0, "files_completed": 0}
    clock = Clock(0.0)
    meter = _meter(session=session, clock=clock, total_bytes=100_000)
    for step in range(1, 6):
        clock.t = float(step)
        session["bytes_network"] = step * 100
        meter.note()
    assert meter.snapshot()["retry"] is None
    clock.t = 17.0
    meter.note_retry()
    snap = meter.snapshot()
    assert snap["retry"] == {"count": 1, "since": 12.0}
    assert status_text(snap) == "retrying"
    line = snapshot_lines(snap, width=90, color=False)[1]
    assert "retrying" in line
    assert "retry 1 · 12s without bytes" in line
    assert "stalled" not in line
    colored = snapshot_lines(snap, width=90, color=True)[1]
    assert "\033[33m" in colored
    log = snapshot_log_line(snap)
    assert "retrying" in log and "retry 1 · 12s without bytes" in log
    clock.t = 28.0
    meter.note_retry()
    assert meter.snapshot()["retry"]["count"] == 2
    # The first byte back ends the retry state; the ETA slot returns.
    clock.t = 29.0
    session["bytes_network"] += 100
    meter.note()
    after = meter.snapshot()
    assert after["retry"] is None
    assert "retry" not in snapshot_lines(after, width=90, color=False)[1]
    # Never over the outage story: offline owns the slot and the tail.
    meter.note_retry()
    both = {**meter.snapshot(), "link": {"state": "offline", "since": 3.0}}
    assert status_text(both) == "offline"
    tail = snapshot_lines(both, width=90, color=False)[1]
    assert "offline" in tail and "retry" not in tail


def test_meter_shows_free_space_while_the_disk_cannot_hold_the_rest(
    monkeypatch, tmp_path
):
    from types import SimpleNamespace

    gib = 1024**3
    disk = {"free": 3 * gib, "probes": 0}

    def usage(path):
        disk["probes"] += 1
        assert path == tmp_path
        return SimpleNamespace(free=disk["free"])

    monkeypatch.setattr("darsay.progress.shutil.disk_usage", usage)
    session = {"bytes_network": 0, "bytes_local_sources": 0, "files_completed": 0}
    clock = Clock(0.0)
    meter = TransferMeter(
        total_bytes=10 * gib,
        total_files=1,
        verified_bytes=0,
        verified_files=0,
        partial_bytes=0,
        session=session,
        disk_path=tmp_path,
        disk_floor=2 * gib,
        clock=clock,
    )
    snap = meter.snapshot()
    assert snap["disk_free"] == 3 * gib
    assert snap["disk_floor"] == 2 * gib
    assert snap["disk_short"] is True
    line = snapshot_lines(snap, width=100, color=False)[1]
    assert line.endswith("· free 3.0 GiB")
    assert "free 3.0 GiB" in snapshot_log_line(snap)
    # Probed at most every couple of seconds, not per frame.
    clock.t = 1.0
    meter.snapshot()
    assert disk["probes"] == 1
    # Space freed: the note goes away.
    disk["free"] = 100 * gib
    clock.t = 3.0
    later = meter.snapshot()
    assert disk["probes"] == 2
    assert later["disk_short"] is False
    assert "free" not in snapshot_lines(later, width=100, color=False)[1]
    # No destination to watch: nothing probed, nothing shown.
    plain = _meter(clock=Clock(0.0)).snapshot()
    assert plain["disk_free"] is None and plain["disk_short"] is False


def test_free_note_yields_to_outage_and_narrow_widths():
    snap = _offline_snap(link=None, stalled=False, rate=5e6, eta_seconds=600)
    snap.update(
        {"disk_free": 381 * 1024**3, "disk_short": True, "max_rate": 5 * 1024**2}
    )
    wide = snapshot_lines(snap, width=120, color=False)[1]
    assert wide.endswith("· free 381.0 GiB · cap 5.0 MiB/s")
    narrow = snapshot_lines(snap, width=60, color=False)[1]
    assert "free" not in narrow and len(narrow) <= 60
    offline = snapshot_lines(
        {**_offline_snap(), "disk_free": 10, "disk_short": True}, width=120
    )[1]
    assert "free" not in offline


def test_meter_from_plan_watches_the_stop_controllers_disk(tmp_path):
    ctrl = types.SimpleNamespace(
        max_bytes=None, disk_path=tmp_path, min_free_bytes=5, interrupted=False
    )
    plan = {"bytes": {"total": 10}, "files": {"total": 1}}
    session = {"bytes_network": 0, "bytes_local_sources": 0, "files_completed": 0}
    meter = meter_from_plan(plan, session, ctrl)
    assert meter.disk_path == tmp_path
    assert meter.disk_floor == 5
    assert meter_from_plan(plan, session).disk_path is None


def test_display_final_line_carries_the_verdict():
    session = {"bytes_network": 250, "bytes_local_sources": 0, "files_completed": 1}
    meter = _meter(total_bytes=1000, session=session, clock=Clock(5.0))
    buf = io.StringIO()
    display = TransferDisplay(
        meter, progress=lambda *a, **k: None, stream=buf, live=True, color=False
    )
    display.start()
    display.refresh()
    display.stop(verdict="paused: disk")
    after_restore = buf.getvalue().rsplit("\033[?25h", 1)[1]
    assert after_restore.strip().endswith("elapsed · paused: disk")

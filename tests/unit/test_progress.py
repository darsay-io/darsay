from __future__ import annotations

import io

from darsay.progress import (
    TransferDisplay,
    TransferMeter,
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
)


class Clock:
    def __init__(self, t: float = 0.0):
        self.t = t

    def __call__(self) -> float:
        return self.t


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

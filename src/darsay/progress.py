"""Live transfer metrics for archive downloads.

Archives are large enough that a per-file tqdm bar is the wrong unit. This
module tracks the whole payload — percent, bytes in / total, smoothed rate,
time remaining, files done, the file now in flight — and draws a compact
three-line panel on a TTY. Piped runs emit a status line every few seconds.

The Hub client still owns HTTP Range and retries; we only consume its
tqdm callbacks and render.
"""

from __future__ import annotations

import os
import shutil
import sys
import threading
import time
from collections import deque
from collections.abc import Callable
from contextlib import suppress
from typing import TextIO

from .readme_gen import human_size

# Brand cyan from the README/PyPI badge (#22d3ee).
_CYAN = "\033[38;2;34;211;238m"
_CYAN16 = "\033[96m"
_DIM = "\033[2m"
_BOLD = "\033[1m"
_RESET = "\033[0m"
_HIDE_CURSOR = "\033[?25l"
_SHOW_CURSOR = "\033[?25h"
_CLEAR_DOWN = "\033[J"
_UP = "\033[{n}A"

_BAR_FULL = "█"
_BAR_EMPTY = "░"
_BAR_FRAC = "▏▎▍▌▋▊▉"
_SPARK = "▁▂▃▄▅▆▇█"

_SAMPLE_WINDOW_S = 8.0
_STALL_AFTER_S = 15.0
_LIVE_HZ = 0.1
_LOG_INTERVAL_S = 10.0
_SPARK_POINTS = 8
_PANEL_LINES = 3

ProgressFn = Callable[..., None]


def color_enabled(stream: TextIO | None) -> bool:
    """Honor NO_COLOR / FORCE_COLOR / TERM, then the stream's TTY bit."""
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("FORCE_COLOR"):
        return True
    if os.environ.get("TERM") == "dumb":
        return False
    return bool(stream is not None and hasattr(stream, "isatty") and stream.isatty())


def progress_setting() -> str:
    """How the transfer panel should render: ``off``, ``line``, or ``auto``."""
    value = os.environ.get("DARSAY_PROGRESS", "").strip().lower()
    if value in {"0", "false", "off", "no"}:
        return "off"
    if value in {"line", "log", "plain"}:
        return "line"
    return "auto"


def progress_disabled() -> bool:
    return progress_setting() == "off"


def _truecolor() -> bool:
    return os.environ.get("COLORTERM", "").lower() in {"truecolor", "24bit"}


def _paint(text: str, *codes: str, enabled: bool) -> str:
    if not enabled or not codes:
        return text
    return f"{''.join(codes)}{text}{_RESET}"


def human_rate(bytes_per_sec: float | None) -> str:
    if bytes_per_sec is None:
        return "-"
    if bytes_per_sec < 1:
        return "0 B/s"
    return f"{human_size(int(bytes_per_sec))}/s"


def human_duration(seconds: float | None) -> str:
    """Compact elapsed-style duration (no 'left')."""
    if seconds is None:
        return "-"
    if seconds < 0:
        seconds = 0
    total = int(round(seconds))
    if total < 60:
        return f"{total}s"
    minutes, secs = divmod(total, 60)
    if minutes < 60:
        if secs and minutes < 10:
            return f"{minutes} min {secs:02d}s"
        return f"{minutes} min"
    hours, minutes = divmod(minutes, 60)
    if hours < 48:
        return f"{hours}h {minutes} min" if minutes else f"{hours}h"
    days, hours = divmod(hours, 24)
    return f"{days}d {hours}h" if hours else f"{days}d"


def human_eta(seconds: float | None, *, stalled: bool = False) -> str:
    """Time remaining the way a person would say it."""
    if stalled:
        return "stalled"
    if seconds is None:
        return "starting"
    if seconds < 1.5:
        return "almost done"
    return f"{human_duration(seconds)} left"


def render_bar(fraction: float | None, width: int) -> str:
    """A width-cell bar with sub-character leading edge."""
    if width <= 0:
        return ""
    if fraction is None:
        return _BAR_EMPTY * width
    fraction = min(1.0, max(0.0, fraction))
    exact = fraction * width
    full = min(width, int(exact))
    if full >= width:
        return _BAR_FULL * width
    frac = exact - full
    if frac < 1 / 16:
        return _BAR_FULL * full + _BAR_EMPTY * (width - full)
    partial = _BAR_FRAC[min(len(_BAR_FRAC) - 1, int(frac * len(_BAR_FRAC)))]
    return _BAR_FULL * full + partial + _BAR_EMPTY * (width - full - 1)


def render_sparkline(rates: list[float], width: int = _SPARK_POINTS) -> str:
    if width <= 0:
        return ""
    if len(rates) < 2:
        return "·" * width
    recent = rates[-width:]
    peak = max(recent) or 1.0
    cells = []
    pad = width - len(recent)
    if pad > 0:
        cells.append("·" * pad)
    for rate in recent:
        idx = int(round((rate / peak) * (len(_SPARK) - 1)))
        cells.append(_SPARK[max(0, min(len(_SPARK) - 1, idx))])
    return "".join(cells)


def _truncate_end(text: str, width: int) -> str:
    if width <= 0:
        return ""
    if len(text) <= width:
        return text
    if width <= 1:
        return "…"[:width]
    return "…" + text[-(width - 1) :]


def format_percent(fraction: float | None) -> str:
    if fraction is None:
        return "  --.-%"
    return f"{min(100.0, max(0.0, fraction * 100.0)):5.1f}%"


def snapshot_lines(snap: dict, *, width: int = 80, color: bool = False) -> list[str]:
    """Render the three-line panel. Always returns exactly three lines."""
    width = max(40, width)
    cyan = _CYAN if _truecolor() else _CYAN16
    fraction = snap.get("fraction")
    bar_width = min(28, max(12, width - 42))
    bar = render_bar(fraction, bar_width)
    if color:
        filled_to = len(bar.rstrip(_BAR_EMPTY))
        bar = _paint(bar[:filled_to], cyan, enabled=True) + _paint(
            bar[filled_to:], _DIM, enabled=True
        )
    percent = _paint(format_percent(fraction), _BOLD, enabled=color)
    done = human_size(snap.get("done_bytes") or 0)
    total = snap.get("total_bytes") or 0
    bytes_part = f"{done} / {human_size(total)}" if total else f"{done} downloaded"
    line1 = f"  {bar}  {percent}   {bytes_part}"

    rate = human_rate(snap.get("rate"))
    spark = render_sparkline(list(snap.get("rate_history") or []), _SPARK_POINTS)
    eta = _paint(
        human_eta(snap.get("eta_seconds"), stalled=bool(snap.get("stalled"))),
        _BOLD,
        enabled=color,
    )
    budget = snap.get("budget_bytes")
    if budget:
        tail = (
            f"budget {human_size(snap.get('budget_used') or 0)} / {human_size(budget)}"
        )
    else:
        tail = f"{human_duration(snap.get('elapsed') or 0)} elapsed"
    line2 = f"  {rate}  {spark}   {eta}   {tail}"

    files_done = int(snap.get("files_done") or 0)
    files_total = int(snap.get("files_total") or 0)
    files = f"{files_done}/{files_total} files" if files_total else "files"
    current = snap.get("current") or []
    if not current:
        detail = files
    elif len(current) == 1:
        item = current[0]
        name = _truncate_end(str(item.get("path") or ""), max(12, width - 36))
        phase = item.get("phase") or "download"
        if phase == "hashing":
            detail = f"{files} · hashing {name}"
        else:
            file_total = item.get("total")
            file_n = int(item.get("n") or 0)
            if file_total:
                frac = min(1.0, file_n / file_total) if file_total else 0.0
                detail = (
                    f"{files} · {name}  {format_percent(frac).strip()}  "
                    f"{human_size(file_n)} / {human_size(file_total)}"
                )
            else:
                detail = f"{files} · {name}  {human_size(file_n)}"
    else:
        lead = max(current, key=lambda item: int(item.get("total") or 0))
        name = _truncate_end(str(lead.get("path") or ""), max(12, width - 40))
        detail = f"{files} · {len(current)} in flight · {name}"
    line3 = f"  {_paint(detail, _DIM, enabled=color)}"

    def _fit(line: str) -> str:
        # ANSI sequences must not count toward width; if over, drop to plain.
        plain = line
        for code in (_CYAN, _CYAN16, _DIM, _BOLD, _RESET):
            plain = plain.replace(code, "")
        if len(plain) <= width:
            return line
        return plain[: width - 1] + "…"

    return [_fit(line1), _fit(line2), _fit(line3)]


def snapshot_log_line(snap: dict) -> str:
    """Single-line form for logs, cron, and non-TTY runs."""
    fraction = snap.get("fraction")
    percent = format_percent(fraction).strip()
    total = snap.get("total_bytes") or 0
    bytes_part = (
        f"{human_size(snap.get('done_bytes') or 0)}/{human_size(total)}"
        if total
        else human_size(snap.get("done_bytes") or 0)
    )
    eta = human_eta(snap.get("eta_seconds"), stalled=bool(snap.get("stalled")))
    files_done = int(snap.get("files_done") or 0)
    files_total = int(snap.get("files_total") or 0)
    current = snap.get("current") or []
    name = current[0]["path"] if len(current) == 1 else ""
    if len(current) > 1:
        name = f"{len(current)} files"
    bits = [
        percent,
        bytes_part,
        human_rate(snap.get("rate")),
        eta,
        f"{files_done}/{files_total} files" if files_total else "",
        name,
    ]
    return "  ".join(bit for bit in bits if bit)


class TransferMeter:
    """Thread-safe archive-level transfer accounting."""

    def __init__(
        self,
        *,
        total_bytes: int,
        total_files: int,
        verified_bytes: int,
        verified_files: int,
        partial_bytes: int,
        session: dict,
        files_completed_base: int = 0,
        budget_bytes: int | None = None,
        clock=time.monotonic,
    ):
        self.total_bytes = max(0, int(total_bytes or 0))
        self.total_files = max(0, int(total_files or 0))
        self.verified_bytes = max(0, int(verified_bytes or 0))
        self.verified_files = max(0, int(verified_files or 0))
        self.partial_bytes = max(0, int(partial_bytes or 0))
        self.session = session
        self.files_completed_base = int(files_completed_base or 0)
        self.budget_bytes = budget_bytes
        self._clock = clock
        self.started = clock()
        self.lock = threading.Lock()
        self._tls = threading.local()
        self._inflight: dict[str, dict] = {}
        self._samples: deque[tuple[float, int]] = deque()
        self._rates: deque[float] = deque(maxlen=24)
        self._ema: float | None = None
        self._last_byte_at = self.started
        self._last_done = self.verified_bytes + self.partial_bytes

    def set_current(
        self, path: str, size: int | None = None, *, phase: str = "download"
    ) -> None:
        self._tls.path = path
        with self.lock:
            current = self._inflight.get(path) or {}
            current["path"] = path
            current["size"] = size
            current["phase"] = phase
            self._inflight[path] = current

    def attach_bar(self, bar, desc: str = "") -> None:
        path = getattr(self._tls, "path", None) or desc or f"file-{id(bar)}"
        self._tls.path = path
        with self.lock:
            current = self._inflight.get(path) or {
                "path": path,
                "size": getattr(bar, "total", None),
                "phase": "download",
            }
            current["bar"] = bar
            if current.get("size") is None:
                current["size"] = getattr(bar, "total", None)
            self._inflight[path] = current

    def detach_bar(self, bar) -> None:
        with self.lock:
            for info in self._inflight.values():
                if info.get("bar") is bar:
                    info.pop("bar", None)
                    break

    def clear_current(self, path: str | None = None) -> None:
        path = path or getattr(self._tls, "path", None)
        with self.lock:
            if path is not None:
                self._inflight.pop(path, None)
        if getattr(self._tls, "path", None) == path:
            self._tls.path = None

    def note(self) -> None:
        """Record a sample from the session's latest byte counts."""
        now = self._clock()
        done = self._done_bytes()
        with self.lock:
            if done != self._last_done:
                self._last_byte_at = now
                self._last_done = done
            self._samples.append((now, done))
            cutoff = now - _SAMPLE_WINDOW_S
            while self._samples and self._samples[0][0] < cutoff:
                self._samples.popleft()
            rate = self._window_rate_locked(now)
            if rate is not None:
                if self._ema is None:
                    self._ema = rate
                else:
                    self._ema = (0.35 * rate) + (0.65 * self._ema)
                self._rates.append(self._ema)

    def _done_bytes(self) -> int:
        network = int(self.session.get("bytes_network") or 0)
        local = int(self.session.get("bytes_local_sources") or 0)
        done = self.verified_bytes + self.partial_bytes + network + local
        if self.total_bytes:
            return min(self.total_bytes, done)
        return done

    def _window_rate_locked(self, now: float) -> float | None:
        if len(self._samples) < 2:
            return None
        t0, b0 = self._samples[0]
        t1, b1 = self._samples[-1]
        dt = t1 - t0
        if dt < 0.4:
            return None
        return max(0.0, (b1 - b0) / dt)

    def snapshot(self) -> dict:
        now = self._clock()
        self.note()
        with self.lock:
            done = self._done_bytes()
            remaining = max(0, self.total_bytes - done) if self.total_bytes else 0
            rate = self._ema
            stalled = (now - self._last_byte_at) >= _STALL_AFTER_S and remaining > 0
            eta = None
            if rate and rate > 1 and remaining and not stalled:
                eta = remaining / rate
            elif remaining == 0 and self.total_bytes:
                eta = 0.0
            files_done = self.verified_files + max(
                0,
                int(self.session.get("files_completed") or 0)
                - self.files_completed_base,
            )
            current = []
            for path, info in self._inflight.items():
                bar = info.get("bar")
                total = (
                    getattr(bar, "total", None) if bar is not None else info.get("size")
                )
                n = int(getattr(bar, "n", 0) or 0) if bar is not None else 0
                if info.get("phase") == "hashing" and total:
                    n = int(total)
                current.append(
                    {
                        "path": path,
                        "n": n,
                        "total": int(total) if total else None,
                        "phase": info.get("phase") or "download",
                    }
                )
            fraction = (done / self.total_bytes) if self.total_bytes else None
            return {
                "fraction": fraction,
                "done_bytes": done,
                "total_bytes": self.total_bytes,
                "remaining_bytes": remaining,
                "rate": rate,
                "rate_history": list(self._rates),
                "eta_seconds": eta,
                "stalled": stalled,
                "files_done": min(files_done, self.total_files or files_done),
                "files_total": self.total_files,
                "elapsed": max(0.0, now - self.started),
                "session_bytes": int(self.session.get("bytes_network") or 0)
                + int(self.session.get("bytes_local_sources") or 0),
                "budget_bytes": self.budget_bytes,
                "budget_used": int(self.session.get("bytes_network") or 0),
                "current": current,
            }


class TransferDisplay:
    """Paint a TransferMeter as a live TTY panel or periodic log lines."""

    def __init__(
        self,
        meter: TransferMeter,
        *,
        progress: ProgressFn = print,
        stream: TextIO | None = None,
        live: bool | None = None,
        color: bool | None = None,
        clock=time.monotonic,
    ):
        self.meter = meter
        self.progress = progress
        self.stream = stream if stream is not None else sys.stderr
        self.clock = clock
        setting = progress_setting()
        if live is not None:
            self.live = live
            self._log = not live
        elif setting == "off":
            self.live = False
            self._log = False
        elif setting == "line":
            self.live = False
            self._log = True
        else:
            is_tty = bool(hasattr(self.stream, "isatty") and self.stream.isatty())
            self.live = is_tty
            self._log = not is_tty
        self.color = color_enabled(self.stream) if color is None else color
        if not self.live:
            self.color = False if color is None else color
        self._io = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._drawn = 0
        self._suspended = 0
        self._last_log = 0.0
        self._cursor_hidden = False

    def start(self) -> None:
        if not self.live and not self._log:
            return
        if self.live:
            with self._io:
                self.stream.write(_HIDE_CURSOR)
                self.stream.flush()
                self._cursor_hidden = True
        self._thread = threading.Thread(
            target=self._loop, name="darsay-progress", daemon=True
        )
        self._thread.start()
        self.refresh()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
            self._thread = None
        with self._io:
            self._restore_locked()

    def echo(self, *args, **kwargs) -> None:
        """Print a log line without corrupting the live panel."""
        self.suspend()
        try:
            self.progress(*args, **kwargs)
        finally:
            self.resume()

    def suspend(self) -> None:
        with self._io:
            self._suspended += 1
            if self._suspended == 1:
                self._restore_locked()

    def resume(self) -> None:
        with self._io:
            if self._suspended:
                self._suspended -= 1
            if self._suspended == 0 and self.live and not self._stop.is_set():
                self.stream.write(_HIDE_CURSOR)
                self._cursor_hidden = True
                self._paint_locked(self.meter.snapshot())

    def refresh(self) -> None:
        snap = self.meter.snapshot()
        with self._io:
            if self._suspended or self._stop.is_set():
                return
            if self.live:
                self._paint_locked(snap)
            elif self._log:
                now = self.clock()
                if now - self._last_log >= _LOG_INTERVAL_S or self._last_log == 0:
                    self._last_log = now
                    self.progress(snapshot_log_line(snap))

    def _loop(self) -> None:
        interval = _LIVE_HZ if self.live else _LOG_INTERVAL_S
        while not self._stop.wait(interval):
            self.refresh()

    def _columns(self) -> int:
        try:
            return max(40, shutil.get_terminal_size(fallback=(80, 24)).columns)
        except OSError:
            return 80

    def _paint_locked(self, snap: dict) -> None:
        lines = snapshot_lines(snap, width=self._columns(), color=self.color)
        if self._drawn:
            self.stream.write(_UP.format(n=self._drawn))
            self.stream.write(_CLEAR_DOWN)
        self.stream.write("\n".join(lines) + "\n")
        self.stream.flush()
        self._drawn = len(lines)

    def _restore_locked(self) -> None:
        if self._drawn:
            self.stream.write(_UP.format(n=self._drawn))
            self.stream.write(_CLEAR_DOWN)
            self._drawn = 0
        if self._cursor_hidden:
            self.stream.write(_SHOW_CURSOR)
            self._cursor_hidden = False
        with suppress(OSError):
            self.stream.flush()


def meter_from_plan(
    plan: dict,
    session: dict,
    stop_controller=None,
) -> TransferMeter:
    sizes = plan.get("bytes") or {}
    files = plan.get("files") or {}
    budget = getattr(stop_controller, "max_bytes", None) if stop_controller else None
    return TransferMeter(
        total_bytes=int(sizes.get("total") or 0),
        total_files=int(files.get("total") or 0),
        verified_bytes=int(sizes.get("verified") or 0),
        verified_files=int(files.get("verified") or 0),
        partial_bytes=int(sizes.get("partial") or 0),
        session=session,
        files_completed_base=int(session.get("files_completed") or 0),
        budget_bytes=budget,
    )

"""Live transfer metrics for archive downloads.

Archives are large enough that a per-file tqdm bar is the wrong unit. This
module tracks the whole payload — percent, bytes in / total, smoothed rate,
time remaining, files done, the file now in flight — and draws a compact
three-line panel on a TTY. Piped runs emit a status line every few seconds.

Panel discipline (the terminal is shared, so the panel defends itself):

- Every numeric field renders at a fixed width, so digit rollovers
  (9.9 → 10.0 MiB/s) never shift columns.
- Frames repaint in place with per-line erases inside a synchronized-output
  block — no whole-panel clear, no flicker.
- While the panel is live, ``sys.stdout``/``sys.stderr`` writes from other
  code (Hub client warnings, logging) are captured and printed *above* the
  panel instead of tearing through it, and the terminal's ``^C`` echo is
  suppressed.
- The rate history sparkline advances one cell per ``_SPARK_INTERVAL_S``,
  so it shows minutes of trend, not the last few chunks.

The Hub client still owns HTTP Range and retries; we only consume its
tqdm callbacks and render.
"""

from __future__ import annotations

import io
import os
import re
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
_ERASE_LINE = "\033[2K"
# Cursor to column 1, n lines up — the first line of the drawn panel.
_PANEL_HOME = "\033[{n}F"
# Synchronized output: terminals that support it apply the frame atomically;
# the rest ignore the private-mode sequence.
_SYNC_ON = "\033[?2026h"
_SYNC_OFF = "\033[?2026l"

_ANSI_RE = re.compile(r"\033\[[0-9;?]*[A-Za-z]")

_BAR_FULL = "█"
_BAR_EMPTY = "░"
_BAR_FRAC = "▏▎▍▌▋▊▉"
_SPARK = "▁▂▃▄▅▆▇█"

_SAMPLE_WINDOW_S = 8.0
_STALL_AFTER_S = 15.0
_LIVE_HZ = 0.1
_LOG_INTERVAL_S = 10.0
_SPARK_POINTS = 8
# One sparkline cell per interval: 8 cells ≈ 40s of rate history.
_SPARK_INTERVAL_S = 5.0
_RATE_WIDTH = len("1023.9 MiB/s")
_ETA_WIDTH = len("12h 26 min left")

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


def _visible_len(text: str) -> int:
    return len(_ANSI_RE.sub("", text))


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


def _size_field_width(total: int) -> int:
    """Widest ``human_size`` rendering for any byte count from 0 to total.

    Right-aligning the moving counter in this field keeps the ``/ total``
    column fixed even across unit rollovers ("1024.0 KiB" → "1.0 MiB").
    """
    width = len(human_size(total))
    boundary = 1024**2
    while boundary <= total:
        width = max(width, len(human_size(boundary - 1)))
        boundary *= 1024
    return width


def snapshot_lines(snap: dict, *, width: int = 80, color: bool = False) -> list[str]:
    """Render the three-line panel. Always returns exactly three lines.

    Every numeric field has a stable width for a given transfer, so repaints
    never shift columns as digits roll over.
    """
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
    if total:
        bytes_part = f"{done:>{_size_field_width(total)}} / {human_size(total)}"
    else:
        bytes_part = f"{done} downloaded"
    line1 = f"  {bar}  {percent}   {bytes_part}"

    rate = f"{human_rate(snap.get('rate')):>{_RATE_WIDTH}}"
    spark = render_sparkline(list(snap.get("rate_history") or []), _SPARK_POINTS)
    if snap.get("interrupted"):
        eta_text = "stopping (^C aborts)"
    else:
        eta_text = human_eta(snap.get("eta_seconds"), stalled=bool(snap.get("stalled")))
    eta = _paint(f"{eta_text:<{_ETA_WIDTH}}", _BOLD, enabled=color)
    budget = snap.get("budget_bytes")
    if budget:
        used = human_size(snap.get("budget_used") or 0)
        tail = f"budget {used:>{_size_field_width(budget)}} / {human_size(budget)}"
    else:
        tail = f"{human_duration(snap.get('elapsed') or 0)} elapsed"
    line2 = f"  {rate}  {spark}   {eta}   {tail}"

    files_done = int(snap.get("files_done") or 0)
    files_total = int(snap.get("files_total") or 0)
    if files_total:
        files = f"{files_done:>{len(str(files_total))}}/{files_total} files"
    else:
        files = "files"
    current = snap.get("current") or []
    if not current:
        detail = files
    elif len(current) == 1:
        item = current[0]
        path = str(item.get("path") or "")
        phase = item.get("phase") or "download"
        name_budget = max(12, width - _visible_len(files) - 8)
        if phase == "hashing":
            detail = f"{files} · hashing {_truncate_end(path, name_budget)}"
        else:
            file_total = item.get("total")
            file_n = int(item.get("n") or 0)
            if file_total:
                file_width = _size_field_width(int(file_total))
                file_part = (
                    f"{format_percent(min(1.0, file_n / file_total))}  "
                    f"{human_size(file_n):>{file_width}} / {human_size(int(file_total))}"
                )
                name_budget = max(12, width - _visible_len(files) - len(file_part) - 9)
                detail = f"{files} · {_truncate_end(path, name_budget)}  {file_part}"
            else:
                detail = (
                    f"{files} · {_truncate_end(path, name_budget)}  "
                    f"{human_size(file_n)}"
                )
    else:
        lead = max(current, key=lambda item: int(item.get("total") or 0))
        name = _truncate_end(str(lead.get("path") or ""), max(12, width - 40))
        detail = f"{files} · {len(current)} in flight · {name}"
    line3 = f"  {_paint(detail, _DIM, enabled=color)}"

    def _fit(line: str) -> str:
        # ANSI sequences must not count toward width; if over, drop to plain.
        if _visible_len(line) <= width:
            return line
        plain = _ANSI_RE.sub("", line)
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
        stop_controller=None,
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
        self.stop_controller = stop_controller
        self._clock = clock
        self.started = clock()
        self.lock = threading.Lock()
        self._tls = threading.local()
        self._inflight: dict[str, dict] = {}
        self._samples: deque[tuple[float, int]] = deque()
        self._rates: deque[float] = deque(maxlen=24)
        self._last_spark: float | None = None
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
                # The sparkline history advances on a clock, not per chunk
                # callback, so it holds a readable window of trend.
                if (
                    self._last_spark is None
                    or now - self._last_spark >= _SPARK_INTERVAL_S
                ):
                    self._last_spark = now
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
                "interrupted": bool(
                    getattr(self.stop_controller, "interrupted", False)
                ),
                "current": current,
            }


class _LineProxy(io.TextIOBase):
    """Stand-in for a TTY stream while the panel is live.

    Buffers writes and hands complete lines to the display, which prints
    them above the panel instead of letting them tear through it.
    """

    def __init__(self, display: TransferDisplay, real: TextIO):
        self._display = display
        self._real = real
        self._buf = ""
        self._lock = threading.Lock()

    def write(self, s) -> int:
        s = str(s)
        lines: list[str]
        with self._lock:
            self._buf += s
            *lines, self._buf = self._buf.split("\n")
        for line in lines:
            self._display.emit_above(line, self._real)
        return len(s)

    def drain(self) -> None:
        with self._lock:
            rest, self._buf = self._buf, ""
        if rest:
            self._display.emit_above(rest, self._real)

    def flush(self) -> None:
        with suppress(OSError):
            self._real.flush()

    def isatty(self) -> bool:
        return True

    def writable(self) -> bool:
        return True

    def fileno(self) -> int:
        return self._real.fileno()

    @property
    def encoding(self):
        return getattr(self._real, "encoding", "utf-8")


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
        self._last_width: int | None = None
        self._suspended = 0
        self._last_log = 0.0
        self._cursor_hidden = False
        self._painted = False
        self._interrupt_announced = False
        self._captured: list[tuple[str, TextIO, _LineProxy]] = []
        self._saved_termios = None

    def start(self) -> None:
        if not self.live and not self._log:
            return
        if self.live:
            with self._io:
                self.stream.write(_HIDE_CURSOR)
                self.stream.flush()
                self._cursor_hidden = True
            self._quiet_ctrl_echo()
            self._capture_std_streams()
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
        self._release_std_streams()
        final = self._final_line() if self.live and self._painted else None
        self._painted = False
        with self._io:
            self._restore_locked()
            if final:
                self.stream.write(final + "\n")
                with suppress(OSError):
                    self.stream.flush()
        self._restore_terminal_modes()

    def echo(self, *args, **kwargs) -> None:
        """Print a log line without corrupting the live panel."""
        self.suspend()
        try:
            self.progress(*args, **kwargs)
        finally:
            self.resume()

    def emit_above(self, text: str, stream: TextIO | None = None) -> None:
        """Write one line of ordinary output above the live panel."""
        out = stream if stream is not None else self.stream
        with self._io:
            if self._drawn and not self._suspended and not self._stop.is_set():
                self.stream.write(_PANEL_HOME.format(n=self._drawn) + _CLEAR_DOWN)
                self._drawn = 0
                with suppress(OSError):
                    self.stream.flush()
                out.write(text + "\n")
                with suppress(OSError):
                    out.flush()
                self._paint_locked(self.meter.snapshot())
            else:
                out.write(text + "\n")
                with suppress(OSError):
                    out.flush()

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
        if snap.get("interrupted") and not self._interrupt_announced:
            self._interrupt_announced = True
            self._announce_interrupt()
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

    def _announce_interrupt(self) -> None:
        note = (
            "Interrupt received — stopping cleanly; verified and partial bytes "
            "are banked. Press Ctrl-C again to abort now, a third time to "
            "force-quit."
        )
        if self.live:
            self.emit_above(note)
        elif self._log:
            self.progress(note)

    def _loop(self) -> None:
        interval = _LIVE_HZ if self.live else _LOG_INTERVAL_S
        while not self._stop.wait(interval):
            self.refresh()

    def _columns(self) -> int:
        try:
            return max(40, os.get_terminal_size(self.stream.fileno()).columns)
        except (OSError, ValueError, AttributeError):
            pass
        try:
            return max(40, shutil.get_terminal_size(fallback=(80, 24)).columns)
        except OSError:
            return 80

    def _paint_locked(self, snap: dict) -> None:
        width = self._columns()
        lines = snapshot_lines(snap, width=width, color=self.color)
        frame = [_SYNC_ON]
        if self._drawn:
            frame.append(_PANEL_HOME.format(n=self._drawn))
            # A narrower terminal may have rewrapped old rows; start clean.
            if len(lines) < self._drawn or (
                self._last_width is not None and width < self._last_width
            ):
                frame.append(_CLEAR_DOWN)
        else:
            frame.append("\r")
        for line in lines:
            frame.append(_ERASE_LINE + line + "\n")
        frame.append(_SYNC_OFF)
        self.stream.write("".join(frame))
        with suppress(OSError):
            self.stream.flush()
        self._drawn = len(lines)
        self._last_width = width
        self._painted = True

    def _restore_locked(self) -> None:
        if self._drawn:
            self.stream.write(_PANEL_HOME.format(n=self._drawn))
            self.stream.write(_CLEAR_DOWN)
            self._drawn = 0
        if self._cursor_hidden:
            self.stream.write(_SHOW_CURSOR)
            self._cursor_hidden = False
        with suppress(OSError):
            self.stream.flush()

    def _final_line(self) -> str:
        """One scrollback line recording where the transfer ended."""
        snap = self.meter.snapshot()
        done = human_size(snap.get("done_bytes") or 0)
        total = snap.get("total_bytes") or 0
        if total:
            percent = format_percent(snap.get("fraction")).strip()
            bytes_part = f"{done} / {human_size(total)} ({percent})"
        else:
            bytes_part = f"{done} downloaded"
        files_total = int(snap.get("files_total") or 0)
        files_done = int(snap.get("files_done") or 0)
        files = f"{files_done}/{files_total} files" if files_total else None
        elapsed = f"{human_duration(snap.get('elapsed') or 0)} elapsed"
        bits = [bit for bit in (bytes_part, files, elapsed) if bit]
        return _paint("  " + " · ".join(bits), _DIM, enabled=self.color)

    def _capture_std_streams(self) -> None:
        """Route other writers' TTY output above the panel while it is live."""
        self._captured = []
        for name in ("stdout", "stderr"):
            stream = getattr(sys, name)
            try:
                is_tty = bool(stream.isatty())
            except (AttributeError, OSError, ValueError):
                is_tty = False
            if not is_tty or isinstance(stream, _LineProxy):
                continue
            proxy = _LineProxy(self, stream)
            setattr(sys, name, proxy)
            self._captured.append((name, stream, proxy))

    def _release_std_streams(self) -> None:
        captured, self._captured = self._captured, []
        for name, real, proxy in captured:
            if getattr(sys, name) is proxy:
                setattr(sys, name, real)
            proxy.drain()

    def _quiet_ctrl_echo(self) -> None:
        """Suppress the terminal's ``^C`` echo while the panel owns rows."""
        try:
            import termios

            fd = self.stream.fileno()
            if not os.isatty(fd):
                return
            attrs = termios.tcgetattr(fd)
            self._saved_termios = (fd, list(attrs))
            attrs[3] &= ~getattr(termios, "ECHOCTL", 0)
            termios.tcsetattr(fd, termios.TCSANOW, attrs)
        except Exception:
            self._saved_termios = None

    def _restore_terminal_modes(self) -> None:
        saved, self._saved_termios = self._saved_termios, None
        if not saved:
            return
        try:
            import termios

            fd, attrs = saved
            termios.tcsetattr(fd, termios.TCSANOW, attrs)
        except Exception:
            pass


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
        stop_controller=stop_controller,
    )

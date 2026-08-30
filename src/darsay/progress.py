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
- Library loggers that bound a ``StreamHandler`` to the terminal before the
  panel started are pointed at the same capture, so a Hub client warning
  cannot push panel rows into scrollback.
- A lost network is a panel state, not a stack trace: the time-remaining
  slot reads ``offline`` / ``reconnecting`` in amber, the tail counts down
  to the next attempt, and one scrollback line records each outage and
  each reconnect. The transport's own retries (the Hub client resumes a
  cut stream a few times before giving up) read ``retrying`` the same way.
- The rate field is the last few seconds; the time remaining is paced by
  the last five minutes, so a day-long ETA does not twitch with every
  chunk, and nothing longer than a month is ever spelled out in days.
- When the destination cannot hold the rest of the payload, the tail shows
  free space, so the number that will end the session is on screen.

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
# Amber for the waiting states — stalled, offline, reconnecting — the one
# accent besides cyan, so it reads as "attention" without shouting.
_AMBER = "\033[38;2;251;191;36m"
_AMBER16 = "\033[33m"
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
# Once bytes stop, the smoothed rate decays and any ETA computed from it
# balloons; hold the last estimate made while bytes were flowing instead,
# until the stall threshold says so plainly.
_ETA_FRESH_S = 2.0
# The ETA is paced by a long horizon — one point every _ETA_POINT_S, up to
# _ETA_POINTS of them (five minutes) — once at least _ETA_MIN_SPAN_S is on
# record; before that the short-window rate stands in.
_ETA_POINT_S = 5.0
_ETA_POINTS = 60
_ETA_MIN_SPAN_S = 30.0
# Beyond this an ETA is a statement about the link, not a time; say so.
_ETA_MAX_S = 30 * 86400.0
# Free space is probed at most this often while the panel is live.
_DISK_PROBE_S = 2.0
_LIVE_HZ = 0.1
# Log mode prints a status line every _LOG_INTERVAL_S but polls every
# second so outage / reconnect notices land promptly.
_LOG_POLL_S = 1.0
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
    if seconds > _ETA_MAX_S:
        return f"> {int(_ETA_MAX_S // 86400)} days left"
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


def styled_bar(fraction: float | None, width: int, *, color: bool) -> str:
    """A ``render_bar`` bar in the panel's styling: cyan fill, dim remainder.

    Shared by the live panel and static previews of it (``darsay estimate``),
    so both render the same brand.
    """
    bar = render_bar(fraction, width)
    if not color:
        return bar
    cyan = _CYAN if _truecolor() else _CYAN16
    filled_to = len(bar.rstrip(_BAR_EMPTY))
    return _paint(bar[:filled_to], cyan, enabled=True) + _paint(
        bar[filled_to:], _DIM, enabled=True
    )


def emphasized(text: str, *, color: bool) -> str:
    """Bold when color is on; the text unchanged otherwise."""
    return _paint(text, _BOLD, enabled=color)


def attention(text: str, *, color: bool) -> str:
    """Amber and bold when color is on: a state that wants a glance."""
    return _paint(text, _AMBER if _truecolor() else _AMBER16, _BOLD, enabled=color)


def dimmed(text: str, *, color: bool) -> str:
    """Dim when color is on; the text unchanged otherwise."""
    return _paint(text, _DIM, enabled=color)


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


def status_text(snap: dict) -> str:
    """The time-remaining slot's word: an ETA, or the state that replaces it."""
    if snap.get("interrupted"):
        return "stopping (^C aborts)"
    link = snap.get("link")
    if link:
        return str(link.get("state") or "offline")
    if snap.get("retry"):
        return "retrying"
    return human_eta(snap.get("eta_seconds"), stalled=bool(snap.get("stalled")))


def _waiting(snap: dict) -> bool:
    """States drawn in amber: the transfer is alive but no bytes are moving."""
    return bool(
        snap.get("link") or snap.get("retry") or snap.get("stalled")
    ) and not snap.get("interrupted")


def _retry_tail(retry: dict) -> str:
    since = human_duration(retry.get("since") or 0)
    return f"retry {int(retry.get('count') or 0)} · {since} without bytes"


def _free_note(snap: dict) -> str | None:
    """``free X`` when the destination cannot hold the rest of the payload."""
    free = snap.get("disk_free")
    if free is None or not snap.get("disk_short"):
        return None
    return f"free {human_size(int(free))}"


def _link_tail(link: dict) -> str:
    since = human_duration(link.get("since") or 0)
    if link.get("state") == "reconnecting":
        return f"attempt {int(link.get('attempts') or 0)} · {since} offline"
    retry_in = link.get("retry_in")
    if retry_in is None:
        return f"{since} offline"
    return f"retry in {human_duration(retry_in)} · {since} offline"


def snapshot_lines(snap: dict, *, width: int = 80, color: bool = False) -> list[str]:
    """Render the three-line panel. Always returns exactly three lines.

    Every numeric field has a stable width for a given transfer, so repaints
    never shift columns as digits roll over.
    """
    width = max(40, width)
    fraction = snap.get("fraction")
    bar_width = min(28, max(12, width - 42))
    bar = styled_bar(fraction, bar_width, color=color)
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
    eta_text = f"{status_text(snap):<{_ETA_WIDTH}}"
    if _waiting(snap):
        eta = attention(eta_text, color=color)
    else:
        eta = _paint(eta_text, _BOLD, enabled=color)
    link = snap.get("link")
    retry = snap.get("retry")
    budget = snap.get("budget_bytes")
    if link:
        tail = _link_tail(link)
    elif retry:
        tail = _retry_tail(retry)
    elif budget:
        used = human_size(snap.get("budget_used") or 0)
        tail = f"budget {used:>{_size_field_width(budget)}} / {human_size(budget)}"
    else:
        tail = f"{human_duration(snap.get('elapsed') or 0)} elapsed"
    line2 = f"  {rate}  {spark}   {eta}   {tail}"
    cap = snap.get("max_rate")
    suffixes = []
    if not link and not retry:
        free = _free_note(snap)
        if free:
            suffixes.append(f" · {free}")
        if cap:
            suffixes.append(f" · cap {human_rate(cap)}")
    for suffix in suffixes:
        if _visible_len(line2) + len(suffix) <= width:
            line2 += _paint(suffix, _DIM, enabled=color)

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
    link = snap.get("link")
    retry = snap.get("retry")
    files_done = int(snap.get("files_done") or 0)
    files_total = int(snap.get("files_total") or 0)
    current = snap.get("current") or []
    name = current[0]["path"] if len(current) == 1 else ""
    if len(current) > 1:
        name = f"{len(current)} files"
    cap = snap.get("max_rate")
    bits = [
        percent,
        bytes_part,
        human_rate(snap.get("rate")),
        f"cap {human_rate(cap)}" if cap else "",
        status_text(snap),
        _link_tail(link) if link else "",
        _retry_tail(retry) if retry and not link else "",
        _free_note(snap) or "",
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
        link=None,
        max_rate: int | None = None,
        disk_path=None,
        disk_floor: int | None = None,
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
        self.link = link
        self.max_rate = max_rate
        self.disk_path = disk_path
        self.disk_floor = max(0, int(disk_floor or 0))
        self._clock = clock
        self.started = clock()
        self._held_eta: float | None = None
        self.lock = threading.Lock()
        self._tls = threading.local()
        self._inflight: dict[str, dict] = {}
        self._samples: deque[tuple[float, int]] = deque()
        self._rates: deque[float] = deque(maxlen=24)
        self._last_spark: float | None = None
        self._ema: float | None = None
        self._eta_points: deque[tuple[float, int]] = deque(maxlen=_ETA_POINTS)
        self._last_eta_point: float | None = None
        # Transport retries since the last byte arrived.
        self._retries = 0
        self._disk_free: int | None = None
        self._disk_probed_at: float | None = None
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
        """Forget the bar but keep its count: a reconnect resumes from here."""
        with self.lock:
            for info in self._inflight.values():
                if info.get("bar") is bar:
                    info["n"] = int(getattr(bar, "n", 0) or 0)
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
                # Bytes arriving end whatever retry was under way.
                self._retries = 0
            self._samples.append((now, done))
            cutoff = now - _SAMPLE_WINDOW_S
            while self._samples and self._samples[0][0] < cutoff:
                self._samples.popleft()
            if (
                self._last_eta_point is None
                or now - self._last_eta_point >= _ETA_POINT_S
            ):
                self._last_eta_point = now
                self._eta_points.append((now, done))
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

    def note_retry(self) -> None:
        """The transport is retrying on its own (a cut stream being resumed).

        Counts up until bytes arrive again; the panel shows ``retrying``
        with the count and how long it has been since the last byte.
        """
        with self.lock:
            self._retries += 1

    def _window_rate_locked(self, now: float) -> float | None:
        if len(self._samples) < 2:
            return None
        t0, b0 = self._samples[0]
        t1, b1 = self._samples[-1]
        dt = t1 - t0
        if dt < 0.4:
            return None
        return max(0.0, (b1 - b0) / dt)

    def _horizon_rate_locked(self) -> float | None:
        """Pace over the last few minutes, for an ETA that does not twitch."""
        if len(self._eta_points) < 2:
            return None
        t0, b0 = self._eta_points[0]
        t1, b1 = self._eta_points[-1]
        if t1 - t0 < _ETA_MIN_SPAN_S:
            return None
        return max(0.0, (b1 - b0) / (t1 - t0))

    def _disk_free_locked(self, now: float) -> int | None:
        if self.disk_path is None:
            return None
        if self._disk_probed_at is None or now - self._disk_probed_at >= _DISK_PROBE_S:
            self._disk_probed_at = now
            with suppress(OSError):
                self._disk_free = shutil.disk_usage(self.disk_path).free
        return self._disk_free

    def snapshot(self) -> dict:
        now = self._clock()
        self.note()
        with self.lock:
            done = self._done_bytes()
            remaining = max(0, self.total_bytes - done) if self.total_bytes else 0
            rate = self._ema
            pace = self._horizon_rate_locked()
            if pace is None:
                pace = rate
            quiet_for = now - self._last_byte_at
            stalled = quiet_for >= _STALL_AFTER_S and remaining > 0
            eta = None
            if remaining == 0 and self.total_bytes:
                eta = 0.0
            elif stalled:
                self._held_eta = None
            elif pace and pace > 1 and remaining and quiet_for < _ETA_FRESH_S:
                eta = remaining / pace
                self._held_eta = eta
            else:
                eta = self._held_eta
            retry = None
            if self._retries and remaining > 0:
                retry = {"count": self._retries, "since": max(0.0, quiet_for)}
            disk_free = self._disk_free_locked(now)
            disk_short = disk_free is not None and remaining > max(
                0, disk_free - self.disk_floor
            )
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
                if bar is not None:
                    n = int(getattr(bar, "n", 0) or 0)
                else:
                    n = int(info.get("n") or 0)
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
                "link": self.link.snapshot() if self.link is not None else None,
                "retry": retry,
                "max_rate": self.max_rate,
                "disk_free": disk_free,
                "disk_floor": self.disk_floor,
                "disk_short": disk_short,
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
        self._link_serial = 0
        self._captured: list[tuple[str, TextIO, _LineProxy]] = []
        self._rebound: list[tuple[object, TextIO, _LineProxy]] = []
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

    def stop(self, verdict: str | None = None) -> None:
        """Take the panel down, leaving one record line ending in ``verdict``."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
            self._thread = None
        self._release_std_streams()
        final = self._final_line(verdict) if self.live and self._painted else None
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
            self._announce(
                "Interrupt received — stopping cleanly; verified and partial bytes "
                "are banked. Press Ctrl-C again to abort now, a third time to "
                "force-quit."
            )
        self._announce_link()
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

    def _announce(self, note: str) -> None:
        """One scrollback line for an event, above the panel or in the log."""
        if self.live:
            self.emit_above(note)
        elif self._log:
            self.progress(note)

    def _announce_link(self) -> None:
        """Say once, in scrollback, when the network goes and when it returns."""
        link = self.meter.link
        if link is None:
            return
        for serial, kind, info in link.transitions_after(self._link_serial):
            self._link_serial = serial
            if kind == "lost":
                self._announce(
                    f"Network unreachable ({info.get('reason')}) — waiting to "
                    "reconnect; verified and partial bytes are banked."
                )
            elif kind == "restored":
                attempts = int(info.get("attempts") or 0)
                self._announce(
                    f"Reconnected after {human_duration(info.get('seconds'))} "
                    f"({attempts} attempt{'s' if attempts != 1 else ''})."
                )

    def _loop(self) -> None:
        interval = _LIVE_HZ if self.live else _LOG_POLL_S
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

    def _final_line(self, verdict: str | None = None) -> str:
        """One scrollback line recording where, and how, the transfer ended."""
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
        bits = [bit for bit in (bytes_part, files, elapsed, verdict) if bit]
        return _paint("  " + " · ".join(bits), _DIM, enabled=self.color)

    def _capture_std_streams(self) -> None:
        """Route other writers' TTY output above the panel while it is live.

        ``sys.stdout`` / ``sys.stderr`` are swapped for line proxies, and any
        ``logging.StreamHandler`` already holding the real stream (library
        loggers bind theirs at import time) is pointed at the proxy too.
        """
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
        proxies = {id(real): proxy for _name, real, proxy in self._captured}
        self._rebound = []
        for handler in _stream_handlers():
            stream = getattr(handler, "stream", None)
            proxy = proxies.get(id(stream))
            if proxy is None or not hasattr(handler, "setStream"):
                continue
            handler.setStream(proxy)
            self._rebound.append((handler, stream, proxy))

    def _release_std_streams(self) -> None:
        rebound, self._rebound = self._rebound, []
        for handler, real, proxy in rebound:
            if getattr(handler, "stream", None) is proxy:
                handler.setStream(real)
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


def _stream_handlers() -> list:
    """Every ``logging.StreamHandler`` attached to any configured logger."""
    import logging

    loggers = [logging.getLogger()]
    loggers.extend(
        item
        for item in logging.Logger.manager.loggerDict.values()
        if isinstance(item, logging.Logger)
    )
    seen: set[int] = set()
    handlers = []
    for logger in loggers:
        for handler in logger.handlers:
            if isinstance(handler, logging.StreamHandler) and id(handler) not in seen:
                seen.add(id(handler))
                handlers.append(handler)
    return handlers


def meter_from_plan(
    plan: dict,
    session: dict,
    stop_controller=None,
    *,
    link=None,
    max_rate: int | None = None,
) -> TransferMeter:
    sizes = plan.get("bytes") or {}
    files = plan.get("files") or {}
    budget = getattr(stop_controller, "max_bytes", None) if stop_controller else None
    disk_path = getattr(stop_controller, "disk_path", None) if stop_controller else None
    disk_floor = (
        getattr(stop_controller, "min_free_bytes", None) if stop_controller else None
    )
    # Bytes handed to another vault (a skeleton) are done for this pin even
    # though this session will not fetch them: fold them into the verified
    # baseline so the panel shows true overall progress and reaches 100%
    # once everything fetchable *here* has landed.
    moved_bytes = int(sizes.get("moved") or 0)
    moved_files = int(files.get("moved") or 0)
    return TransferMeter(
        total_bytes=int(sizes.get("total") or 0),
        total_files=int(files.get("total") or 0),
        verified_bytes=int(sizes.get("verified") or 0) + moved_bytes,
        verified_files=int(files.get("verified") or 0) + moved_files,
        partial_bytes=int(sizes.get("partial") or 0),
        session=session,
        files_completed_base=int(session.get("files_completed") or 0),
        budget_bytes=budget,
        stop_controller=stop_controller,
        link=link,
        max_rate=max_rate,
        disk_path=disk_path,
        disk_floor=disk_floor,
    )

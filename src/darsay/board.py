"""darsay.io boards as remote catalogs.

A board URL is a catalog address, the third form after a vault slug and
a filesystem path. ``GET <board>/catalog.json`` is a plain
``darsay.catalog`` document; ``POST`` of the same path pushes a
refreshed one back — the round trip that lets the CLI, the only party
that can classify, keep a board's prices honest. Claims are board-side
coordination (like the board's holders and status fields, they never
enter catalog.json): ``archive --next <board>`` claims a row before
fetching, reports the transfer panel to it while bytes move
(``ProgressReporter``), and reports the boundaries — a clean pause,
registration — as they come.

The board URL is the capability — treat it like a secret. Boards are
not source providers: they hold want-lists, never bytes, so nothing
here touches the provider registry.
"""

from __future__ import annotations

import json
import os
import re
import socket
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from urllib import error as _urlerror
from urllib import request as _urlrequest
from urllib.parse import urlsplit

HTTP_TIMEOUT = 30
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
# A board's address in every spelling the site answers: the page, the
# page as a document (``/b/<id>.json``, what the site hands a program),
# the API, and the catalog download. All name the same board.
_BOARD_PATH = re.compile(
    r"^/(?:b|api/boards)/(?P<id>[0-9a-f]{32})(?:\.json|/catalog\.json)?/?$"
)


@dataclass(frozen=True)
class Board:
    """One board: where it lives and the id that is its capability."""

    origin: str
    id: str

    @property
    def page_url(self) -> str:
        return f"{self.origin}/b/{self.id}"

    @property
    def api_url(self) -> str:
        return f"{self.origin}/api/boards/{self.id}"

    @property
    def catalog_url(self) -> str:
        return f"{self.api_url}/catalog.json"

    def claim_url(self, entry_id: int) -> str:
        return f"{self.api_url}/entries/{entry_id}/claim"


def parse_board_url(spec: str) -> Board | None:
    """A Board when ``spec`` is a board URL, else None (never raises).

    ``https://darsay.io/b/<id>``, the same with ``.json`` (the board as a
    document), ``/api/boards/<id>``, and ``/api/boards/<id>/catalog.json``
    all name one board.
    """
    raw = (spec or "").strip()
    if not raw.lower().startswith(("https://", "http://")):
        return None
    parts = urlsplit(raw)
    if not parts.netloc:
        return None
    match = _BOARD_PATH.match(parts.path)
    if match is None:
        return None
    return Board(origin=f"{parts.scheme}://{parts.netloc}", id=match.group("id"))


def _user_agent() -> str:
    from . import __version__

    return f"darsay/{__version__} (+https://darsay.io)"


def _http(method: str, url: str, data: bytes | None = None) -> tuple[int, bytes]:
    """One HTTP exchange. The seam hermetic tests replace.

    The CLI identifies itself: Cloudflare's Browser Integrity Check bans
    the default ``Python-urllib`` signature outright (error 1010), and a
    client that speaks to someone's board should say who it is anyway.
    """
    headers = {"User-Agent": _user_agent()}
    if data is not None:
        headers["Content-Type"] = "application/json"
    request = _urlrequest.Request(url, data=data, method=method, headers=headers)
    try:
        with _urlrequest.urlopen(request, timeout=HTTP_TIMEOUT) as response:
            return response.status, response.read(MAX_RESPONSE_BYTES)
    except _urlerror.HTTPError as exc:
        return exc.code, exc.read(MAX_RESPONSE_BYTES)


def _reach(method: str, url: str, data: bytes | None = None) -> tuple[int, bytes]:
    try:
        return _http(method, url, data)
    except OSError as exc:
        raise SystemExit(
            f"error: cannot reach the board at {url}: {exc}. "
            "Check the connection and the URL, then re-run."
        ) from None


def _error_detail(body: bytes) -> str:
    try:
        parsed = json.loads(body)
        if isinstance(parsed, dict) and parsed.get("error"):
            return str(parsed["error"])
    except (ValueError, UnicodeDecodeError):
        pass
    return body.decode("utf-8", "replace")[:200] or "no detail"


def fetch_catalog(board: Board, dest_dir: Path) -> Path:
    """Download the board's catalog document to ``dest_dir/catalog.json``."""
    status, body = _reach("GET", board.catalog_url)
    if status != 200:
        raise SystemExit(
            f"error: board at {board.page_url} answered {status} "
            f"({_error_detail(body)}) — is the URL right?"
        )
    dest_dir.mkdir(parents=True, exist_ok=True)
    path = dest_dir / "catalog.json"
    path.write_bytes(body)
    return path


def push_catalog(board: Board, path: Path) -> dict:
    """POST a refreshed catalog document back to the board."""
    status, body = _reach("POST", board.catalog_url, path.read_bytes())
    if status != 200:
        raise SystemExit(
            f"error: board at {board.page_url} refused the push "
            f"({status}: {_error_detail(body)})\n"
            f"  The refreshed catalog is kept at {path} — nothing was lost."
        )
    try:
        result = json.loads(body)
    except ValueError:
        result = {}
    if not isinstance(result, dict) or result.get("ok") is not True:
        # An older worker answered the POST with the catalog download
        # itself; reporting that as a push would be a phantom success.
        raise SystemExit(
            f"error: the board at {board.page_url} does not support catalog "
            "import yet (the site predates the round trip) — deploy the "
            f"darsay.io update, then re-run.\n"
            f"  The refreshed catalog is kept at {path} — nothing was lost."
        )
    return result


def fetch_entries(board: Board) -> list[dict]:
    """The board's entry rows (with their ids and claims), via the API view."""
    status, body = _reach("GET", board.api_url)
    if status != 200:
        raise SystemExit(
            f"error: board at {board.page_url} answered {status} "
            f"({_error_detail(body)})"
        )
    try:
        parsed = json.loads(body)
    except ValueError:
        parsed = None
    entries = parsed.get("entries") if isinstance(parsed, dict) else None
    return entries if isinstance(entries, list) else []


def entry_for(
    entries: list[dict],
    source: str,
    revision: str | None,
    include: list[str] | None,
) -> dict | None:
    """The board row matching one catalog identity (source, revision, include set).

    The API row carries what catalog.json deliberately omits — the
    board-side ``status``, holders, and live claim — so a caller that
    must honor the board's own bookkeeping reads the row, not just its id.
    """
    from .catalog import include_key, try_parse_source

    want_source = try_parse_source(source)
    want_canonical = want_source.canonical if want_source else source
    want_include = include_key(include)
    want_revision = (revision or "").strip()
    for entry in entries:
        got = try_parse_source(str(entry.get("source") or ""))
        canonical = got.canonical if got else entry.get("source")
        if canonical != want_canonical:
            continue
        if (entry.get("revision") or "").strip() != want_revision:
            continue
        if include_key(entry.get("include")) != want_include:
            continue
        return entry
    return None


def entry_id_for(
    entries: list[dict],
    source: str,
    revision: str | None,
    include: list[str] | None,
) -> int | None:
    """Match one board row by catalog identity (source, revision, include set)."""
    entry = entry_for(entries, source, revision, include)
    identifier = entry.get("id") if entry else None
    return identifier if isinstance(identifier, int) else None


def claim(
    board: Board,
    entry_id: int,
    client: str,
    *,
    state: str = "archiving",
    percent: int | None = None,
    banked_bytes: int | None = None,
    total_bytes: int | None = None,
    force: bool = False,
    refetch: bool = False,
    facts: dict | None = None,
) -> tuple[bool, dict]:
    """Claim a row / report progress. ``(False, claim)`` when someone else holds it.

    ``refetch`` marks a deliberate claim on a row the board already checks
    off as have (``archive SOURCE --board``); without it the board refuses
    such claims, which is what keeps an out-of-date ``--next`` from
    re-downloading what the group already holds. ``facts`` are the
    panel's figures for this report (``report_from_snapshot``); they ride
    beside the named fields and the board renders them.
    """
    payload: dict = {"client": client, "state": state}
    if facts:
        payload.update(facts)
        payload["client"] = client
        payload["state"] = state
    if percent is not None:
        payload["percent"] = int(percent)
    if banked_bytes is not None:
        payload["banked_bytes"] = int(banked_bytes)
    if total_bytes is not None:
        payload["total_bytes"] = int(total_bytes)
    if force:
        payload["force"] = True
    if refetch:
        payload["refetch"] = True
    status, body = _reach(
        "POST", board.claim_url(entry_id), json.dumps(payload).encode("utf-8")
    )
    if status == 409:
        try:
            other = json.loads(body).get("claim") or {}
        except (ValueError, AttributeError):
            other = {}
        return False, other if isinstance(other, dict) else {}
    if status != 200:
        raise SystemExit(
            f"error: board at {board.page_url} refused the claim "
            f"({status}: {_error_detail(body)})"
        )
    try:
        row = json.loads(body)
    except ValueError:
        row = {}
    return True, row if isinstance(row, dict) else {}


# ── Progress reports ─────────────────────────────────────────────────────
# A claimed row shows the panel. While the transfer runs, the meter is read
# on a clock and its figures — percent, bytes, rate, ETA, files, the
# sparkline, the file in flight — are posted as the claim's report, so the
# board's rail moves on every screen that has the page open, not only in
# the terminal doing the fetching. A report goes out when a whole percent
# has passed or the panel's word changed (downloading, stalled, offline …);
# otherwise the row hears from us at least every REPORT_HEARTBEAT_S, so a
# board that has not heard for longer knows the client is gone.

DEFAULT_REPORT_EVERY = 60.0
REPORT_HEARTBEAT_S = 300.0
# The panel keeps this many throughput samples; the board draws the same.
REPORT_SPARK_SAMPLES = 24


def panel_phase(snap: dict) -> str:
    """The panel's status word for a snapshot, as one key the board knows.

    Mirrors ``progress.status_text``: an offline link, a retry, a digest
    pass over landed bytes, a stall, nothing yet, or bytes moving.
    """
    if snap.get("link"):
        return "offline"
    if snap.get("retry"):
        return "retrying"
    if snap.get("verifying"):
        return "verifying"
    if snap.get("stalled"):
        return "stalled"
    if snap.get("eta_seconds") is None and not (snap.get("rate") or 0):
        return "starting"
    return "downloading"


def report_from_snapshot(snap: dict) -> dict:
    """The claim report for one panel snapshot: the figures the terminal shows."""
    from . import __version__

    total = int(snap.get("total_bytes") or 0)
    done = int(snap.get("done_bytes") or 0)
    report: dict = {
        "phase": panel_phase(snap),
        "banked_bytes": done,
        "total_bytes": total,
        "files_done": int(snap.get("files_done") or 0),
        "files_total": int(snap.get("files_total") or 0),
        "elapsed_seconds": int(snap.get("elapsed") or 0),
        "session_bytes": int(snap.get("session_bytes") or 0),
        "agent": f"darsay {__version__}",
    }
    if total:
        report["percent"] = min(100, int(done * 100 / total))
    rate = snap.get("rate")
    if rate is not None:
        report["rate_bps"] = int(rate)
    eta = snap.get("eta_seconds")
    if eta is not None:
        report["eta_seconds"] = int(eta)
    history = [int(r) for r in (snap.get("rate_history") or [])]
    if history:
        report["rates"] = history[-REPORT_SPARK_SAMPLES:]
    current = snap.get("current") or []
    if current:
        lead = max(current, key=lambda item: int(item.get("total") or 0))
        report["current"] = {
            "path": str(lead.get("path") or ""),
            "done": int(lead.get("n") or 0),
            "total": int(lead["total"]) if lead.get("total") else None,
        }
    return report


class ProgressReporter:
    """Post the panel to a claimed row while a transfer runs.

    ``watch(meter, emit)`` is the ``on_meter`` hook ``archive`` offers: it
    starts a daemon thread that reads the meter — at once, then every
    ``interval`` seconds — and returns the callable that stops it. A
    report goes out when the percent moved a whole point or the panel's
    word changed, and in any case every ``heartbeat`` seconds, so a slow
    link is heard from too. Nothing here can fail the archive: a board
    that cannot be reached is noted once, above the panel, and tried
    again at the next tick; a board that says the row is someone else's
    now ends the reports, since there is nothing left to report to.
    """

    def __init__(
        self,
        board: Board,
        entry_id: int,
        client: str,
        *,
        interval: float = DEFAULT_REPORT_EVERY,
        heartbeat: float = REPORT_HEARTBEAT_S,
        post=None,
        clock=time.monotonic,
    ):
        self.board = board
        self.entry_id = entry_id
        self.client = client
        self.interval = max(0.0, float(interval or 0))
        self.heartbeat = max(self.interval, float(heartbeat))
        self._post = post or claim
        self._clock = clock
        self.sent = 0
        self._last_sent_at: float | None = None
        self._last_key: tuple | None = None
        self._warned = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def watch(self, meter, emit=None):
        """Start reporting ``meter``; the return value stops it."""
        if self.interval <= 0:
            return lambda: None
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, args=(meter, emit), name="darsay-board", daemon=True
        )
        self._thread.start()
        return self.stop

    def stop(self) -> None:
        """Stop reporting, waiting for a report in flight so none lands after
        the boundary the caller is about to send."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=HTTP_TIMEOUT + 5)
            self._thread = None

    def _loop(self, meter, emit) -> None:
        self.tick(meter, emit)
        while not self._stop.wait(self.interval):
            self.tick(meter, emit)

    def due(self, report: dict, now: float) -> bool:
        """Whether this report says something new, or the heartbeat is owed."""
        if self._last_sent_at is None:
            return True
        if (report.get("phase"), report.get("percent")) != self._last_key:
            return True
        return now - self._last_sent_at >= self.heartbeat

    def tick(self, meter, emit=None) -> bool:
        """Read the meter once; report if due. True when a report went out."""
        if self._stop.is_set():
            return False
        report = report_from_snapshot(meter.snapshot())
        now = self._clock()
        if not self.due(report, now):
            return False
        try:
            ok, _ = self._post(
                self.board, self.entry_id, self.client, state="archiving", facts=report
            )
        except (SystemExit, Exception) as exc:  # noqa: BLE001 — never fail the archive
            if not self._warned and emit is not None:
                emit(
                    f"warning: the board at {self.board.page_url} could not be "
                    f"updated ({exc}); the archive continues and the next report "
                    "will try again."
                )
            self._warned = True
            return False
        if not ok:
            if emit is not None:
                emit(
                    f"note: the board at {self.board.page_url} says another client "
                    "holds this row now; progress reports stop, the archive continues."
                )
            self._stop.set()
            return False
        self.sent += 1
        self._last_sent_at = now
        self._last_key = (report.get("phase"), report.get("percent"))
        self._warned = False
        return True


def release(board: Board, entry_id: int, client: str) -> bool:
    """Drop this client's claim on a row (best-effort; True on success)."""
    payload = json.dumps({"client": client}).encode("utf-8")
    status, _ = _reach("DELETE", board.claim_url(entry_id), payload)
    return status == 200


# The default client pseudonym: two words and two hex digits, drawn from a
# hash of hostname + user. Stable per machine, but the board never sees the
# hostname itself — a board URL travels, and who holds which machine is
# nobody's business but the operator's.
_ADJECTIVES = (
    "amber",
    "brindled",
    "farside",
    "gilded",
    "harbor",
    "leeward",
    "midnight",
    "patient",
    "quiet",
    "sable",
    "stellar",
    "umbral",
    "vaulted",
    "wandering",
    "winter",
    "zenith",
)
_NOUNS = (
    "archive",
    "atlas",
    "aurora",
    "comet",
    "heron",
    "lantern",
    "meridian",
    "monolith",
    "nebula",
    "orrery",
    "reliquary",
    "sextant",
    "signal",
    "sounding",
    "vault",
    "waypoint",
)


def _default_client() -> str:
    """A stable pseudonym for this machine, e.g. ``amber-heron-3f``."""
    import hashlib

    user = os.environ.get("USER") or os.environ.get("USERNAME") or ""
    seed = f"{socket.gethostname()}\n{user}".encode("utf-8", "replace")
    digest = hashlib.sha256(seed).digest()
    return (
        f"{_ADJECTIVES[digest[0] % len(_ADJECTIVES)]}-"
        f"{_NOUNS[digest[1] % len(_NOUNS)]}-{digest[2]:02x}"
    )


def client_id(vault: Path | None = None) -> str:
    """Who this machine is on a board: ``board.client`` config, else a
    stable pseudonym — never the raw hostname, which identifies the machine
    to everyone the board URL reaches."""
    from .config import setting

    configured = setting("board", "client", vault)
    if isinstance(configured, str) and configured.strip():
        return configured.strip()[:80]
    return _default_client()

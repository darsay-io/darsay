"""darsay.io boards as remote catalogs.

A board URL is a catalog address, the third form after a vault slug and
a filesystem path. ``GET <board>/catalog.json`` is a plain
``darsay.catalog`` document; ``POST`` of the same path pushes a
refreshed one back — the round trip that lets the CLI, the only party
that can classify, keep a board's prices honest. Claims are board-side
coordination (like the board's holders and status fields, they never
enter catalog.json): ``archive --next <board>`` claims a row before
fetching and reports progress at boundaries.

The board URL is the capability — treat it like a secret. Boards are
not source providers: they hold want-lists, never bytes, so nothing
here touches the provider registry.
"""

from __future__ import annotations

import json
import re
import socket
from dataclasses import dataclass
from pathlib import Path
from urllib import error as _urlerror
from urllib import request as _urlrequest
from urllib.parse import urlsplit

HTTP_TIMEOUT = 30
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
_BOARD_PATH = re.compile(
    r"^/(?:b|api/boards)/(?P<id>[0-9a-f]{32})(?:/catalog\.json)?/?$"
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
    """A Board when ``spec`` is a board URL, else None (never raises)."""
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


def entry_id_for(
    entries: list[dict],
    source: str,
    revision: str | None,
    include: list[str] | None,
) -> int | None:
    """Match one board row by catalog identity (source, revision, include set)."""
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
        identifier = entry.get("id")
        return identifier if isinstance(identifier, int) else None
    return None


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
) -> tuple[bool, dict]:
    """Claim a row / report progress. ``(False, claim)`` when someone else holds it."""
    payload: dict = {"client": client, "state": state}
    if percent is not None:
        payload["percent"] = int(percent)
    if banked_bytes is not None:
        payload["banked_bytes"] = int(banked_bytes)
    if total_bytes is not None:
        payload["total_bytes"] = int(total_bytes)
    if force:
        payload["force"] = True
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


def release(board: Board, entry_id: int, client: str) -> bool:
    """Drop this client's claim on a row (best-effort; True on success)."""
    payload = json.dumps({"client": client}).encode("utf-8")
    status, _ = _reach("DELETE", board.claim_url(entry_id), payload)
    return status == 200


def client_id(vault: Path | None = None) -> str:
    """Who this machine is on a board: ``board.client`` config, else hostname."""
    from .config import setting

    configured = setting("board", "client", vault)
    if isinstance(configured, str) and configured.strip():
        return configured.strip()[:80]
    return socket.gethostname()[:80] or "darsay"

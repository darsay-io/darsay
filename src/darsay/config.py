"""Machine-local operator configuration.

Configuration is operator preference, never archival fact: nothing here
changes what a bundle records, only how this machine behaves while
producing one. Config files therefore live outside bundles and are never
exported. Settings resolve in layers, later wins:

1. built-in defaults,
2. the user file — ``$DARSAY_CONFIG`` if set, else
   ``$XDG_CONFIG_HOME/darsay/config.toml`` (default ``~/.config/darsay/``),
3. the vault file — ``<vault>/config.toml``, which travels with the vault
   so an archive drive can carry limits suited to its own disk,
4. per-setting environment variables (``$DARSAY_MIN_FREE``,
   ``$DARSAY_MAX_RATE``, ``$DARSAY_MAX_OFFLINE``),
5. an explicit CLI flag.

Files are TOML::

    [transfer]
    min_free = "2G"      # pause archives below this much destination free space
    max_rate = "5M"      # cap network transfer at 5 MiB/s; 0 is unlimited
    max_offline = "1h"   # keep waiting for the network this long, then pause

    [host]                            # the vault file only: a fact about this disk
    ssh = "root@nas"                  # the host that owns it, as ssh names it
    path = "/volume1/darsay/vault"    # this vault's path on that host

Unknown keys in a known table warn (a typo must not silently disarm a
guard); unknown tables are ignored, so a vault config written by a newer
darsay still loads here. ``darsay config`` prints the effective values
and which layer set each one; ``darsay config KEY=VALUE`` writes the
vault file. ``[host]`` is read from the vault file alone — it describes
one disk, not a machine's preference — and with it ``verify``, ``mv``,
``cp``, and the reconcile pass of ``archive`` / ``assemble`` hash on that
host instead of over the wire (``farside.py``).
"""

from __future__ import annotations

import os
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

CONFIG_FILE = "config.toml"
DEFAULT_MIN_FREE = 2 * 1024**3
# Bytes per second; 0 is unlimited.
DEFAULT_MAX_RATE = 0
# Seconds to keep retrying while the network is unreachable before pausing
# cleanly. An hour outlasts a walk between Wi-Fi networks or a router
# reboot; a longer outage pauses with exit 10 and resumes on rerun.
DEFAULT_MAX_OFFLINE = 3600.0
# Seconds between a running archive's progress reports to a claimed board
# row: a minute moves the board's rail at a walking pace; a whole percent
# or a change of the panel's word is what makes a report, so a slow link
# reports less often and every link at least every five minutes.
DEFAULT_BOARD_REPORT_EVERY = 60.0

_BYTE_SIZE_RE = re.compile(r"\s*(\d+(?:\.\d+)?)\s*([KMGT]?)\s*(?:I?B)?\s*")
_RATE_SUFFIX_RE = re.compile(r"\s*/\s*s(?:ec)?\s*$", re.IGNORECASE)
_DURATION_RE = re.compile(
    r"\s*(\d+(?:\.\d+)?)\s*(s|sec|secs|m|min|mins|h|hr|hrs|d|day|days)?\s*",
    re.IGNORECASE,
)
_DURATION_UNITS = {"s": 1.0, "m": 60.0, "h": 3600.0, "d": 86400.0}


def parse_byte_size(value: int | str) -> int:
    """Bytes from an int or a string with binary K/M/G/T suffixes; 0 allowed."""
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValueError(f"expected a byte size, got {value!r}")
    if isinstance(value, int):
        if value < 0:
            raise ValueError(f"expected a byte size of 0 or more, got {value!r}")
        return value
    match = _BYTE_SIZE_RE.fullmatch(value.upper())
    if not match:
        raise ValueError(
            f"invalid byte size {value!r}; use bytes or a suffix such as 500M or 20G"
        )
    number = float(match.group(1))
    multiplier = 1024 ** ("KMGT".index(match.group(2)) + 1) if match.group(2) else 1
    return int(number * multiplier)


def parse_rate(value: int | str) -> int:
    """Bytes per second from an int or a byte size with an optional ``/s``.

    ``"5M"`` and ``"5M/s"`` both mean 5 MiB/s; ``0`` is unlimited.
    """
    if isinstance(value, str):
        value = _RATE_SUFFIX_RE.sub("", value)
    try:
        return parse_byte_size(value)
    except ValueError:
        raise ValueError(
            f"invalid rate {value!r}; use bytes per second or a suffix such as 500K or 5M"
        ) from None


def parse_duration(value: int | float | str) -> float:
    """Seconds from a number or a string with s/m/h/d suffixes; 0 allowed.

    A bare number is seconds. ``"30m"``, ``"1.5h"``, and ``"2d"`` read as
    people write them.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ValueError(f"expected a duration, got {value!r}")
    if isinstance(value, (int, float)):
        if value < 0:
            raise ValueError(f"expected a duration of 0 or more, got {value!r}")
        return float(value)
    match = _DURATION_RE.fullmatch(value)
    if not match:
        raise ValueError(
            f"invalid duration {value!r}; use seconds or a suffix such as 30m or 1h"
        )
    unit = (match.group(2) or "s")[0].lower()
    return float(match.group(1)) * _DURATION_UNITS[unit]


def _render_byte_size(value: int) -> str:
    from .readme_gen import human_size

    return human_size(value) if value else "0 (disabled)"


def _render_rate(value: int) -> str:
    from .progress import human_rate

    return human_rate(value) if value else "0 (unlimited)"


def _render_duration(value: float) -> str:
    from .progress import human_duration

    return human_duration(value) if value else "0 (pause at the first failure)"


@dataclass(frozen=True)
class Setting:
    """One operator setting: how it is parsed, shown, and overridden."""

    table: str
    key: str
    default: object
    parse: Callable[[object], object]
    render: Callable[[object], str]
    help: str
    example: str
    env: str | None = None
    flag: str | None = None
    # A fact about one vault's disk, read from that vault's file alone.
    vault_only: bool = False

    @property
    def name(self) -> str:
        return f"{self.table}.{self.key}"


def _text(value: object) -> str:
    if isinstance(value, bool) or not isinstance(value, str):
        raise ValueError(f"expected a string, got {value!r}")
    return value.strip()


# The registry. Every layer — file parsing, env overrides, ``darsay
# config`` — reads from these rows; a new setting is a new row, not a new
# special case at a call site.
SETTINGS: tuple[Setting, ...] = (
    Setting(
        table="transfer",
        key="min_free",
        default=DEFAULT_MIN_FREE,
        parse=parse_byte_size,
        render=_render_byte_size,
        help="pause archives below this much destination free space; 0 disables",
        example='"10G"',
        env="DARSAY_MIN_FREE",
        flag="archive --min-free",
    ),
    Setting(
        table="transfer",
        key="max_rate",
        default=DEFAULT_MAX_RATE,
        parse=parse_rate,
        render=_render_rate,
        help="cap network transfer at this many bytes per second; 0 is unlimited",
        example='"5M"',
        env="DARSAY_MAX_RATE",
        flag="archive --max-rate",
    ),
    Setting(
        table="transfer",
        key="max_offline",
        default=DEFAULT_MAX_OFFLINE,
        parse=parse_duration,
        render=_render_duration,
        help="keep waiting for the network this long before pausing; 0 pauses at the first failure",
        example='"1h"',
        env="DARSAY_MAX_OFFLINE",
        flag="archive --max-offline",
    ),
    Setting(
        table="board",
        key="client",
        default="",
        parse=lambda value: str(value).strip()[:80],
        render=lambda value: str(value) or "(hostname)",
        help="how this machine signs board claims; empty means the hostname",
        example='"jeremy-mbp"',
        env="DARSAY_BOARD_CLIENT",
    ),
    Setting(
        table="board",
        key="report_every",
        default=DEFAULT_BOARD_REPORT_EVERY,
        parse=parse_duration,
        render=_render_duration,
        help="how often a running archive reports its panel to the claimed board row; 0 reports only at the boundaries",
        example='"1m"',
        env="DARSAY_BOARD_REPORT_EVERY",
    ),
    Setting(
        table="host",
        key="ssh",
        default="",
        parse=_text,
        render=lambda value: str(value) or "(none: payloads are hashed here)",
        help=(
            "the host that owns this vault's disk, as ssh names it; with host.path, "
            "verify / mv / cp hash there instead of reading the mount back"
        ),
        example='"root@nas"',
        vault_only=True,
    ),
    Setting(
        table="host",
        key="path",
        default="",
        parse=_text,
        render=lambda value: str(value) or "(none)",
        help="this vault's path on that host",
        example='"/volume1/darsay/vault"',
        vault_only=True,
    ),
)
_BY_SLOT = {(item.table, item.key): item for item in SETTINGS}
_TABLES = {item.table for item in SETTINGS}


def user_config_path() -> Path:
    """``$DARSAY_CONFIG`` if set, else the XDG darsay config file."""
    explicit = os.environ.get("DARSAY_CONFIG")
    if explicit:
        return Path(explicit).expanduser()
    base = os.environ.get("XDG_CONFIG_HOME")
    root = Path(base).expanduser() if base else Path.home() / ".config"
    return root / "darsay" / CONFIG_FILE


def vault_config_path(vault: Path) -> Path:
    return vault / CONFIG_FILE


def _toml_module():
    """``tomllib`` (3.11+) or the ``tomli`` backport; ``None`` when neither."""
    try:
        import tomllib
    except ModuleNotFoundError:
        try:
            import tomli as tomllib
        except ModuleNotFoundError:
            return None
    return tomllib


def _read_file(path: Path, *, required: bool, vault_file: bool = True) -> dict:
    """One config file as validated ``{(table, key): value}`` settings.

    A ``vault_only`` setting in the user file is a category error — it
    describes one disk, not this machine — so it warns and is ignored.
    """
    try:
        text = path.read_bytes()
    except FileNotFoundError:
        if required:
            raise SystemExit(
                f"error: $DARSAY_CONFIG points at a missing file: {path}"
            ) from None
        return {}
    except OSError as exc:
        raise SystemExit(f"error: unreadable config at {path}: {exc}") from None
    # A present file the operator wrote must never be skipped silently: on
    # Python 3.10 without the backport, say so and name the alternatives.
    toml = _toml_module()
    if toml is None:
        raise SystemExit(
            f"error: cannot read {path}: TOML needs Python 3.11+ or "
            "`pip install tomli`; on this Python use --min-free or $DARSAY_MIN_FREE"
        )
    try:
        data = toml.loads(text.decode("utf-8"))
    except (UnicodeDecodeError, toml.TOMLDecodeError) as exc:
        raise SystemExit(f"error: unreadable config at {path}: {exc}") from None
    values = {}
    for table, keys in data.items():
        if table not in _TABLES:
            continue
        if not isinstance(keys, dict):
            raise SystemExit(f"error: {path}: [{table}] must be a table")
        for key, raw in keys.items():
            item = _BY_SLOT.get((table, key))
            if item is None:
                print(
                    f"warning: {path}: unknown key {table}.{key} (ignored)",
                    file=sys.stderr,
                )
                continue
            if item.vault_only and not vault_file:
                print(
                    f"warning: {path}: {item.name} describes one vault's disk and "
                    "belongs in that vault's config.toml (ignored here)",
                    file=sys.stderr,
                )
                continue
            try:
                values[(table, key)] = item.parse(raw)
            except ValueError as exc:
                raise SystemExit(f"error: {path}: {item.name}: {exc}") from None
    return values


def write_vault_settings(vault: Path, assignments: dict[str, str]) -> Path:
    """Set ``table.key`` values in ``<vault>/config.toml``, keeping the rest.

    Each value is validated by its setting's parser before anything is
    written, and written as a TOML string (every setting reads one). A
    present ``key =`` line in the table is replaced; a missing one is
    added after the table header; a missing table is appended. Comments
    and other lines stay where they are.
    """
    items = {}
    for name, raw in assignments.items():
        table, _, key = name.partition(".")
        item = _BY_SLOT.get((table, key))
        if item is None:
            known = ", ".join(entry.name for entry in SETTINGS)
            raise SystemExit(f"error: unknown setting {name!r}; settings: {known}")
        try:
            item.parse(raw)
        except ValueError as exc:
            raise SystemExit(f"error: {item.name}: {exc}") from None
        items[(table, key)] = raw
    path = vault_config_path(vault)
    lines = path.read_text(encoding="utf-8").splitlines() if path.is_file() else []
    for (table, key), raw in items.items():
        rendered = f'{key} = "{raw}"'
        header = next(
            (i for i, line in enumerate(lines) if line.strip() == f"[{table}]"), None
        )
        if header is None:
            if lines and lines[-1].strip():
                lines.append("")
            lines += [f"[{table}]", rendered]
            continue
        end = next(
            (
                i
                for i in range(header + 1, len(lines))
                if lines[i].strip().startswith("[")
            ),
            len(lines),
        )
        slot = next(
            (
                i
                for i in range(header + 1, end)
                if re.match(rf"\s*{re.escape(key)}\s*=", lines[i])
            ),
            None,
        )
        if slot is None:
            # After the table's last setting, before any blank lines that
            # separate it from the next table.
            insert_at = end
            while insert_at > header + 1 and not lines[insert_at - 1].strip():
                insert_at -= 1
            lines.insert(insert_at, rendered)
        else:
            lines[slot] = rendered
    text = "\n".join(lines) + "\n"
    toml = _toml_module()
    if toml is not None:
        toml.loads(text)  # a file this darsay cannot read back is never written
    path.write_text(text, encoding="utf-8")
    return path


def resolved_settings(vault: Path | None = None) -> dict:
    """Every known setting with its value and the layer that set it.

    Returns ``{(table, key): {"value": ..., "origin": ...}}`` where origin
    is ``"default"``, a config file path, or ``"$ENV_VAR"``.
    """
    resolved = {
        (item.table, item.key): {"value": item.default, "origin": "default"}
        for item in SETTINGS
    }
    layers = [(user_config_path(), bool(os.environ.get("DARSAY_CONFIG")), False)]
    if vault is not None:
        layers.append((vault_config_path(vault), False, True))
    for path, required, vault_file in layers:
        values = _read_file(path, required=required, vault_file=vault_file)
        for slot, value in values.items():
            resolved[slot] = {"value": value, "origin": str(path)}
    for item in SETTINGS:
        if item.env is None:
            continue
        raw = os.environ.get(item.env)
        if raw is None or not raw.strip():
            continue
        try:
            value = item.parse(raw)
        except ValueError as exc:
            raise SystemExit(f"error: ${item.env}: {exc}") from None
        resolved[(item.table, item.key)] = {"value": value, "origin": f"${item.env}"}
    return resolved


def setting(table: str, key: str, vault: Path | None = None):
    return resolved_settings(vault)[(table, key)]["value"]


def free_space_floor(
    vault: Path | None = None, override: int | None = None
) -> int | None:
    """Effective transfer free-space floor in bytes; ``None`` when disabled.

    ``override`` is the CLI value and wins outright; ``0`` at any layer
    disables the floor.
    """
    if override is not None:
        return override or None
    return setting("transfer", "min_free", vault) or None


def rate_cap(vault: Path | None = None, override: int | None = None) -> int | None:
    """Effective transfer cap in bytes per second; ``None`` when unlimited.

    ``override`` is the CLI value and wins outright; ``0`` at any layer
    lifts the cap.
    """
    if override is not None:
        return override or None
    return setting("transfer", "max_rate", vault) or None


def offline_patience(vault: Path | None = None, override: float | None = None) -> float:
    """Seconds to keep waiting for an unreachable network before pausing.

    ``override`` is the CLI value and wins outright; ``0`` pauses at the
    first failure.
    """
    if override is not None:
        return float(override)
    return float(setting("transfer", "max_offline", vault))

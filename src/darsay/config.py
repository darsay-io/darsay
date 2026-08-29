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
4. per-setting environment variables (``$DARSAY_MIN_FREE``),
5. an explicit CLI flag.

Files are TOML::

    [transfer]
    min_free = "2G"   # pause archives below this much destination free space

Unknown keys in a known table warn (a typo must not silently disarm a
guard); unknown tables are ignored, so a vault config written by a newer
darsay still loads here. ``darsay config`` prints the effective values
and which layer set each one.
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

_BYTE_SIZE_RE = re.compile(r"\s*(\d+(?:\.\d+)?)\s*([KMGT]?)\s*(?:I?B)?\s*")


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


def _render_byte_size(value: int) -> str:
    from .readme_gen import human_size

    return human_size(value) if value else "0 (disabled)"


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

    @property
    def name(self) -> str:
        return f"{self.table}.{self.key}"


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


def _read_file(path: Path, *, required: bool) -> dict:
    """One config file as validated ``{(table, key): value}`` settings."""
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
            try:
                values[(table, key)] = item.parse(raw)
            except ValueError as exc:
                raise SystemExit(f"error: {path}: {item.name}: {exc}") from None
    return values


def resolved_settings(vault: Path | None = None) -> dict:
    """Every known setting with its value and the layer that set it.

    Returns ``{(table, key): {"value": ..., "origin": ...}}`` where origin
    is ``"default"``, a config file path, or ``"$ENV_VAR"``.
    """
    resolved = {
        (item.table, item.key): {"value": item.default, "origin": "default"}
        for item in SETTINGS
    }
    layers = [(user_config_path(), bool(os.environ.get("DARSAY_CONFIG")))]
    if vault is not None:
        layers.append((vault_config_path(vault), False))
    for path, required in layers:
        for slot, value in _read_file(path, required=required).items():
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

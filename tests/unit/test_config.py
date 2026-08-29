from __future__ import annotations

import pytest

from darsay.config import (
    DEFAULT_MIN_FREE,
    free_space_floor,
    parse_byte_size,
    resolved_settings,
    user_config_path,
    vault_config_path,
)


@pytest.fixture
def no_env_floor(monkeypatch):
    """Undo the suite-wide $DARSAY_MIN_FREE=0 so file layers are visible."""
    monkeypatch.delenv("DARSAY_MIN_FREE", raising=False)


def test_parse_byte_size_grammar():
    assert parse_byte_size("1024") == 1024
    assert parse_byte_size("1K") == 1024
    assert parse_byte_size("500M") == 500 * 1024**2
    assert parse_byte_size("2GiB") == 2 * 1024**3
    assert parse_byte_size("1.5K") == int(1.5 * 1024)
    assert parse_byte_size("0") == 0
    assert parse_byte_size(4096) == 4096
    assert parse_byte_size(0) == 0


@pytest.mark.parametrize("bad", ["abc", "-1", "1X", "", True, None, 1.5, -1])
def test_parse_byte_size_rejects_junk(bad):
    with pytest.raises(ValueError):
        parse_byte_size(bad)


def test_default_floor_without_any_config(no_env_floor):
    assert free_space_floor() == DEFAULT_MIN_FREE


def test_user_config_path_honors_env(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert user_config_path() == tmp_path / "darsay" / "config.toml"
    monkeypatch.setenv("DARSAY_CONFIG", str(tmp_path / "explicit.toml"))
    assert user_config_path() == tmp_path / "explicit.toml"


def test_layering_user_then_vault_then_env_then_override(
    monkeypatch, tmp_path, no_env_floor
):
    user = tmp_path / "user.toml"
    user.write_text('[transfer]\nmin_free = "1G"\n', encoding="utf-8")
    monkeypatch.setenv("DARSAY_CONFIG", str(user))
    assert free_space_floor() == 1024**3

    vault = tmp_path / "vault"
    vault.mkdir()
    vault_config_path(vault).write_text(
        '[transfer]\nmin_free = "3G"\n', encoding="utf-8"
    )
    assert free_space_floor(vault) == 3 * 1024**3

    monkeypatch.setenv("DARSAY_MIN_FREE", "4G")
    assert free_space_floor(vault) == 4 * 1024**3
    assert resolved_settings(vault)[("transfer", "min_free")]["origin"] == (
        "$DARSAY_MIN_FREE"
    )

    assert free_space_floor(vault, override=5) == 5
    assert free_space_floor(vault, override=0) is None


def test_zero_disables_at_any_layer(monkeypatch, tmp_path, no_env_floor):
    vault = tmp_path / "vault"
    vault.mkdir()
    vault_config_path(vault).write_text("[transfer]\nmin_free = 0\n", encoding="utf-8")
    assert free_space_floor(vault) is None
    monkeypatch.setenv("DARSAY_MIN_FREE", "0")
    assert free_space_floor() is None


def test_unknown_key_warns_and_unknown_table_is_ignored(
    monkeypatch, tmp_path, capsys, no_env_floor
):
    config = tmp_path / "config.toml"
    config.write_text(
        '[transfer]\nmin_fre = "10G"\n\n[future]\nshiny = true\n', encoding="utf-8"
    )
    monkeypatch.setenv("DARSAY_CONFIG", str(config))
    assert free_space_floor() == DEFAULT_MIN_FREE
    err = capsys.readouterr().err
    assert "unknown key transfer.min_fre" in err
    assert "future" not in err


def test_bad_files_and_values_fail_loudly(monkeypatch, tmp_path, no_env_floor):
    config = tmp_path / "config.toml"
    monkeypatch.setenv("DARSAY_CONFIG", str(config))
    with pytest.raises(SystemExit, match="missing file"):
        free_space_floor()
    config.write_text("not toml [", encoding="utf-8")
    with pytest.raises(SystemExit, match="unreadable config"):
        free_space_floor()
    config.write_text("[transfer]\nmin_free = true\n", encoding="utf-8")
    with pytest.raises(SystemExit, match="transfer.min_free"):
        free_space_floor()
    config.write_text("transfer = 1\n", encoding="utf-8")
    with pytest.raises(SystemExit, match="must be a table"):
        free_space_floor()


def test_missing_toml_parser_refuses_a_present_file(
    monkeypatch, tmp_path, no_env_floor
):
    monkeypatch.setattr("darsay.config._toml_module", lambda: None)
    # No file: nothing to parse, defaults apply on any Python.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert free_space_floor() == DEFAULT_MIN_FREE
    config = tmp_path / "config.toml"
    config.write_text('[transfer]\nmin_free = "10G"\n', encoding="utf-8")
    monkeypatch.setenv("DARSAY_CONFIG", str(config))
    with pytest.raises(SystemExit, match="tomli"):
        free_space_floor()


def test_bad_env_value_fails_loudly(monkeypatch):
    monkeypatch.setenv("DARSAY_MIN_FREE", "lots")
    with pytest.raises(SystemExit, match="DARSAY_MIN_FREE"):
        free_space_floor()


def test_parse_duration_grammar():
    from darsay.config import parse_duration

    assert parse_duration("30") == 30.0
    assert parse_duration("30s") == 30.0
    assert parse_duration("15m") == 900.0
    assert parse_duration("1.5h") == 5400.0
    assert parse_duration("2d") == 2 * 86400.0
    assert parse_duration("1 hr") == 3600.0
    assert parse_duration("0") == 0.0
    assert parse_duration(90) == 90.0
    assert parse_duration(1.5) == 1.5


@pytest.mark.parametrize("bad", ["soon", "-1", "1x", "", True, None, -1])
def test_parse_duration_rejects_junk(bad):
    from darsay.config import parse_duration

    with pytest.raises(ValueError):
        parse_duration(bad)


def test_parse_rate_accepts_per_second_suffix():
    from darsay.config import parse_rate

    assert parse_rate("5M") == 5 * 1024**2
    assert parse_rate("5M/s") == 5 * 1024**2
    assert parse_rate("500K / sec") == 500 * 1024
    assert parse_rate("2MiB/s") == 2 * 1024**2
    assert parse_rate(0) == 0
    with pytest.raises(ValueError, match="invalid rate"):
        parse_rate("fast")


def test_rate_cap_and_offline_patience_layers(monkeypatch, tmp_path):
    from darsay.config import (
        DEFAULT_MAX_OFFLINE,
        offline_patience,
        rate_cap,
        resolved_settings,
    )

    assert rate_cap() is None
    assert offline_patience() == DEFAULT_MAX_OFFLINE
    vault = tmp_path / "vault"
    vault.mkdir()
    vault_config_path(vault).write_text(
        '[transfer]\nmax_rate = "5M"\nmax_offline = "30m"\n', encoding="utf-8"
    )
    assert rate_cap(vault) == 5 * 1024**2
    assert offline_patience(vault) == 1800.0
    monkeypatch.setenv("DARSAY_MAX_RATE", "0")
    monkeypatch.setenv("DARSAY_MAX_OFFLINE", "0")
    assert rate_cap(vault) is None
    assert offline_patience(vault) == 0.0
    origins = resolved_settings(vault)
    assert origins[("transfer", "max_rate")]["origin"] == "$DARSAY_MAX_RATE"
    assert origins[("transfer", "max_offline")]["origin"] == "$DARSAY_MAX_OFFLINE"
    # A CLI value wins outright; 0 lifts the cap but is a real 0 for patience.
    assert rate_cap(vault, override=1024) == 1024
    assert rate_cap(vault, override=0) is None
    assert offline_patience(vault, override=0) == 0.0
    assert offline_patience(vault, override=45.0) == 45.0


def test_config_renders_rate_and_duration():
    from darsay.config import SETTINGS

    by_name = {item.name: item for item in SETTINGS}
    assert by_name["transfer.max_rate"].render(0) == "0 (unlimited)"
    assert by_name["transfer.max_rate"].render(5 * 1024**2) == "5.0 MiB/s"
    assert by_name["transfer.max_offline"].render(3600.0) == "1h"
    assert "first failure" in by_name["transfer.max_offline"].render(0)

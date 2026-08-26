from __future__ import annotations

import argparse

import pytest

from darsay import __version__
from darsay.cli import _byte_size, _positive_float, _positive_int, _shard_key, main


def test_version_flag(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert f"darsay {__version__}" in capsys.readouterr().out


def test_no_args_prints_help(capsys):
    assert main([]) == 0
    out = capsys.readouterr().out
    assert "darsay archive" in out
    assert "estimate" in out


def test_help_lists_subcommands(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    for name in (
        "estimate",
        "archive",
        "verify",
        "export",
        "import",
        "assemble",
        "hydrate",
        "run",
        "rm",
        "du",
        "complete",
    ):
        assert name in out


def test_byte_size_suffixes():
    assert _byte_size("1024") == 1024
    assert _byte_size("1K") == 1024
    assert _byte_size("500M") == 500 * 1024**2
    assert _byte_size("20G") == 20 * 1024**3
    assert _byte_size("1.5K") == int(1.5 * 1024)
    assert _byte_size("2GiB") == 2 * 1024**3


def test_byte_size_rejects_zero_and_junk():
    with pytest.raises(argparse.ArgumentTypeError):
        _byte_size("0")
    with pytest.raises(argparse.ArgumentTypeError):
        _byte_size("-1")
    with pytest.raises(argparse.ArgumentTypeError):
        _byte_size("abc")


def test_positive_float_and_int():
    assert _positive_float("1.5") == 1.5
    assert _positive_int("4") == 4
    with pytest.raises(argparse.ArgumentTypeError):
        _positive_float("0")
    with pytest.raises(argparse.ArgumentTypeError):
        _positive_int("-2")
    with pytest.raises(argparse.ArgumentTypeError):
        _positive_int("nope")


def test_shard_key():
    assert _shard_key("1/3") == (1, 3)
    assert _shard_key(" 2 / 2 ") == (2, 2)
    with pytest.raises(argparse.ArgumentTypeError):
        _shard_key("1/1")
    with pytest.raises(argparse.ArgumentTypeError):
        _shard_key("0/3")
    with pytest.raises(argparse.ArgumentTypeError):
        _shard_key("4/3")
    with pytest.raises(argparse.ArgumentTypeError):
        _shard_key("1/1025")
    with pytest.raises(argparse.ArgumentTypeError):
        _shard_key("one/two")


def test_bundle_dir_requires_manifest(tmp_path):
    from argparse import Namespace

    from darsay.cli import _bundle_dir

    with pytest.raises(SystemExit, match="no manifest.json"):
        _bundle_dir(Namespace(vault=str(tmp_path), bundle=str(tmp_path)))


def test_vault_path_env_and_flag(monkeypatch, tmp_path):
    from argparse import Namespace
    from pathlib import Path

    from darsay.cli import _vault_path

    monkeypatch.delenv("DARSAY_HOME", raising=False)
    assert _vault_path(Namespace(vault=None)) == Path.home() / "darsay"
    assert _vault_path(Namespace(vault=str(tmp_path))) == tmp_path
    assert _vault_path(Namespace(vault="~/custom")) == Path.home() / "custom"
    monkeypatch.setenv("DARSAY_HOME", str(tmp_path / "from-env"))
    assert _vault_path(Namespace(vault=None)) == tmp_path / "from-env"
    # --vault wins over the env
    assert _vault_path(Namespace(vault=str(tmp_path / "flag"))) == tmp_path / "flag"

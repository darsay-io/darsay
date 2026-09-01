from __future__ import annotations

import argparse

import pytest

from darsay import __version__
from darsay.cli import (
    _byte_size,
    _min_free,
    _positive_float,
    _positive_int,
    _shard_key,
    main,
)


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
        "config",
        "complete",
        "catalog",
        "list",
    ):
        assert name in out


def test_byte_size_suffixes():
    assert _byte_size("1024") == 1024
    assert _byte_size("1K") == 1024
    assert _byte_size("500M") == 500 * 1024**2
    assert _byte_size("20G") == 20 * 1024**3
    assert _byte_size("1.5K") == int(1.5 * 1024)
    assert _byte_size("2GiB") == 2 * 1024**3


def test_min_free_allows_zero_to_disable():
    assert _min_free("0") == 0
    assert _min_free("2G") == 2 * 1024**3
    assert _min_free("500M") == 500 * 1024**2
    with pytest.raises(argparse.ArgumentTypeError):
        _min_free("lots")


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


def test_list_accepts_catalog_and_sort():
    # Drive the real parser via main --help-equivalent by parsing through main's error paths
    with pytest.raises(SystemExit):
        main(["list", "--sort", "nope"])
    with pytest.raises(SystemExit):
        main(["catalog", "add", "summer", "huggingface:acme/toy", "--desire", "0"])
    with pytest.raises(SystemExit):
        main(["catalog", "add", "summer", "huggingface:acme/toy", "--desire", "10"])


def test_archive_next_parse_orders(tmp_path):
    from darsay.cli import main as _main

    vault = ["--vault", str(tmp_path)]
    with pytest.raises(SystemExit, match="no catalog matching"):
        _main([*vault, "archive", "--next", "summer"])
    with pytest.raises(SystemExit, match="no catalog matching"):
        _main([*vault, "archive", "summer", "--next"])
    with pytest.raises(SystemExit, match="already chose the catalog"):
        _main([*vault, "archive", "--next", "summer", "huggingface:Qwen/Qwen3-0.6B"])


def test_flags_by_command_walks_every_subparser():
    from darsay.cli import flags_by_command

    flags = flags_by_command()
    assert {"--version", "--vault"} <= flags[""]
    assert {"--max-rate", "--max-offline", "--min-free", "--shard", "--yes"} <= flags[
        "archive"
    ]
    assert "--desire" in flags["catalog add"]
    assert "--prune" in flags["envs"]
    assert "--handoff" in flags["assemble"]
    assert "--rehash" in flags["assemble"]
    assert "--force" in flags["mv"]


def test_tty_confirm_defaults_to_yes_and_restores_sigint(monkeypatch):
    import signal

    from darsay.cli import _tty_confirm

    sentinel = signal.getsignal(signal.SIGINT)
    answers = iter(["", "y", "YES ", "n", "no"])
    monkeypatch.setattr("builtins.input", lambda question: next(answers))
    assert [_tty_confirm("Continue? ") for _ in range(5)] == [
        True,
        True,
        True,
        False,
        False,
    ]

    def closed(_question):
        raise EOFError

    monkeypatch.setattr("builtins.input", closed)
    assert _tty_confirm("Continue? ") is False
    assert signal.getsignal(signal.SIGINT) is sentinel


def test_rate_and_duration_flags():
    from darsay.cli import _duration, _rate

    assert _rate("5M") == 5 * 1024**2
    assert _rate("5M/s") == 5 * 1024**2
    assert _rate("0") == 0
    with pytest.raises(argparse.ArgumentTypeError):
        _rate("fast")
    assert _duration("30m") == 1800.0
    assert _duration("0") == 0.0
    with pytest.raises(argparse.ArgumentTypeError):
        _duration("soon")


def test_unexpected_error_is_one_line_unless_debug(monkeypatch, capsys):
    from darsay.cli import _run

    def boom(_args):
        raise RuntimeError("kaboom")

    monkeypatch.delenv("DARSAY_DEBUG", raising=False)
    assert _run(boom, None) == 1
    err = capsys.readouterr().err
    assert "unexpected RuntimeError: kaboom (test_cli_parse.py:" in err
    assert "DARSAY_DEBUG=1" in err
    assert "Traceback" not in err
    monkeypatch.setenv("DARSAY_DEBUG", "1")
    with pytest.raises(RuntimeError):
        _run(boom, None)


def test_source_error_is_a_clean_message(capsys):
    from darsay.cli import _run
    from darsay.sources import SourceError

    def unreachable(_args):
        raise SourceError("error: cannot reach Hugging Face — DNS lookup failed")

    assert _run(unreachable, None) == 1
    assert "cannot reach Hugging Face" in capsys.readouterr().err


def test_dry_run_is_offered_by_every_writing_command():
    from darsay.cli import flags_by_command

    flags = flags_by_command()
    writing = (
        "estimate",
        "archive",
        "rm",
        "regen",
        "hydrate",
        "run",
        "dehydrate",
        "envs",
        "export",
        "import",
        "mv",
        "assemble",
        "catalog new",
        "catalog add",
        "catalog drop",
        "catalog adopt",
        "catalog regen",
        "doctor",
    )
    for command in writing:
        assert "--dry-run" in flags[command], command
    # Read-only commands do not pretend to have anything to skip.
    for command in ("list", "du", "config", "info", "verify", "smoke", "complete"):
        assert "--dry-run" not in flags[command], command


def test_dry_run_short_flag_parses_alone_and_clustered(capsys):
    from darsay.cli import build_parser

    parser = build_parser()
    assert parser.parse_args(["rm", "toy", "-n"]).dry_run is True
    clustered = parser.parse_args(["rm", "toy", "-yn"])
    assert clustered.yes is True and clustered.dry_run is True
    run = parser.parse_args(["run", "toy", "Say", "hello", "-n"])
    assert run.prompt == ["Say", "hello"] and run.dry_run is True
    with pytest.raises(SystemExit):
        parser.parse_args(["list", "-n"])
    capsys.readouterr()


def test_real_command_drops_only_the_dry_run_flag():
    from darsay.cli import _real_command

    assert (
        _real_command(["--vault", "v", "rm", "toy", "--dry-run"])
        == "darsay --vault v rm toy"
    )
    assert _real_command(["rm", "toy", "-yn"]) == "darsay rm toy -y"
    assert _real_command(["rm", "toy", "-ny", "-n"]) == "darsay rm toy -y"
    assert (
        _real_command(["run", "toy", "Say hello", "-n"]) == "darsay run toy 'Say hello'"
    )
    assert (
        _real_command(["archive", "--next", "summer", "--dry-run", "--max-gb", "10"])
        == "darsay archive --next summer --max-gb 10"
    )

from __future__ import annotations

import re
from pathlib import Path

import pytest

from darsay.complete import BUNDLE_COMMANDS, CATALOG_COMMANDS, COMMANDS, script_for

ROOT = Path(__file__).resolve().parents[2]


def test_complete_command_lists_match_argparse():
    text = (ROOT / "src" / "darsay" / "cli.py").read_text(encoding="utf-8")
    cli_cmds = re.findall(r'add_cmd\("([^"]+)"', text)
    cat_cmds = re.findall(r'add_cat\("([^"]+)"', text)
    assert set(cli_cmds) == set(COMMANDS)
    assert set(cat_cmds) == set(CATALOG_COMMANDS)
    assert set(BUNDLE_COMMANDS) <= set(COMMANDS)


def test_script_for_known_shells():
    zsh = script_for("zsh")
    assert zsh.startswith("#compdef darsay")
    assert "darsay list --ids" in zsh
    assert "CURRENT-1" in zsh and "--next" in zsh
    bash = script_for("bash")
    assert "complete -F _darsay darsay" in bash
    assert "*.mvb.tar" in bash
    assert "catalog --ids" in bash
    assert "== --next" in bash
    fish = script_for("fish")
    assert "darsay.fish" in fish
    assert "-l next" in fish
    for name in COMMANDS:
        assert name in zsh
        assert name in bash
        assert name in fish
    for name in BUNDLE_COMMANDS:
        assert name in zsh
    with pytest.raises(SystemExit, match="unknown shell"):
        script_for("powershell")

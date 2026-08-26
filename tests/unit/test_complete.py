from __future__ import annotations

import pytest

from darsay.complete import script_for


def test_script_for_known_shells():
    zsh = script_for("zsh")
    assert zsh.startswith("#compdef darsay")
    assert "darsay list --ids" in zsh
    bash = script_for("bash")
    assert "complete -F _darsay darsay" in bash
    fish = script_for("fish")
    assert "darsay.fish" in fish
    with pytest.raises(SystemExit, match="unknown shell"):
        script_for("powershell")

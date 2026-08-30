#!/usr/bin/env python3
"""Run the source-tree CLI as one signal-addressable process for tests."""

from __future__ import annotations

import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))
os.environ.setdefault("DARSAY_HOME", os.getcwd())
os.environ.setdefault("XDG_CONFIG_HOME", str(Path.cwd() / ".config"))
os.environ.setdefault("DARSAY_MIN_FREE", "0")
os.environ.setdefault("DARSAY_DOCTOR_TEST_HOLD_LOCK_MS", "250")

from darsay.cli import main  # noqa: E402


def run(argv: list[str] | None = None) -> int:
    return main(argv)


if __name__ == "__main__":
    raise SystemExit(run())

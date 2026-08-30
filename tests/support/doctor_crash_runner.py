#!/usr/bin/env python3
"""Kill a real doctor process at one deterministic mutation boundary."""

from __future__ import annotations

import argparse
import os
import signal
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from darsay import doctor  # noqa: E402
from darsay.cli import main  # noqa: E402


def _kill_after_prepare() -> None:
    real_append = doctor._append_action

    def append_then_kill(run: Path, event: dict) -> None:
        real_append(run, event)
        if event.get("event") == "mutation_prepared":
            os.kill(os.getpid(), signal.SIGKILL)

    doctor._append_action = append_then_kill


def _kill_after_commit() -> None:
    real_replace = doctor.mutate_replace_at

    def replace_then_kill(parent_fd: int, temp_name: str, target_name: str) -> None:
        real_replace(parent_fd, temp_name, target_name)
        if target_name == "README.md":
            os.kill(os.getpid(), signal.SIGKILL)

    doctor.mutate_replace_at = replace_then_kill


def run() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("stage", choices=("after-prepare", "after-commit"))
    parser.add_argument("vault", type=Path)
    args = parser.parse_args()

    if args.stage == "after-prepare":
        _kill_after_prepare()
    else:
        _kill_after_commit()
    return main(["--vault", str(args.vault), "doctor", "fix", "--json"])


if __name__ == "__main__":
    raise SystemExit(run())

"""Repository-owned process and contract gates for ``darsay doctor``."""

from __future__ import annotations

import difflib
import json
import os
import signal
import stat
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
TEST_TOOL = REPO_ROOT / "tests" / "support" / "doctor_cli_runner.py"
CRASH_RUNNER = REPO_ROOT / "tests" / "support" / "doctor_crash_runner.py"
FM_ID = "fm-vault-and-bundle-state-generated-readme-is-missing-or-stale"
FIXTURE = REPO_ROOT / "tests" / "doctor_fixtures" / FM_ID
GOLDEN = REPO_ROOT / "tests" / "golden" / "doctor_capabilities.json"


def _doctor_env(tmp_path: Path, *, hold_lock_ms: int = 0) -> dict[str, str]:
    env = os.environ.copy()
    env.pop("DARSAY_CONFIG", None)
    env.update(
        {
            "DARSAY_DOCTOR_TEST_HOLD_LOCK_MS": str(hold_lock_ms),
            "DARSAY_MIN_FREE": "0",
            "DARSAY_TEST_PYTHON": sys.executable,
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPATH": os.pathsep.join([str(REPO_ROOT / "src"), str(REPO_ROOT)]),
            "XDG_CONFIG_HOME": str(tmp_path / ".config"),
        }
    )
    return env


def _corrupt_vault(tmp_path: Path) -> tuple[Path, Path]:
    vault = tmp_path / "vault"
    vault.mkdir()
    subprocess.run(
        [str(FIXTURE / "corrupt.sh"), str(vault)],
        cwd=REPO_ROOT,
        env=_doctor_env(tmp_path),
        check=True,
        text=True,
        capture_output=True,
        timeout=20,
    )
    readme = next(vault.glob("test--acme--toy/*/README.md"))
    return vault, readme


def _doctor(
    tmp_path: Path,
    vault: Path | None,
    *args: str,
    expected: int,
) -> tuple[subprocess.CompletedProcess[str], dict]:
    command = [sys.executable, str(TEST_TOOL)]
    if vault is not None:
        command.extend(("--vault", str(vault)))
    command.extend(("doctor", *args))
    completed = subprocess.run(
        command,
        cwd=REPO_ROOT,
        env=_doctor_env(tmp_path),
        text=True,
        capture_output=True,
        timeout=20,
    )
    assert completed.returncode == expected, completed.stdout + completed.stderr
    assert completed.stderr == ""
    return completed, json.loads(completed.stdout)


def _prepared_run(vault: Path) -> Path:
    candidates = [
        path
        for path in (vault / ".doctor" / "runs").iterdir()
        if '"event": "mutation_prepared"' in (path / "actions.jsonl").read_text()
    ]
    assert len(candidates) == 1
    return candidates[0]


@pytest.mark.parametrize(
    ("stage", "target_was_committed"),
    [("after-prepare", False), ("after-commit", True)],
)
def test_sigkill_recovery_is_explicit_and_byte_exact(
    tmp_path: Path, stage: str, target_was_committed: bool
):
    vault, readme = _corrupt_vault(tmp_path)
    before = readme.read_bytes()
    before_stat = readme.stat()
    completed = subprocess.run(
        [sys.executable, str(CRASH_RUNNER), stage, str(vault)],
        cwd=REPO_ROOT,
        env=_doctor_env(tmp_path),
        text=True,
        capture_output=True,
        timeout=20,
    )
    assert completed.returncode == -signal.SIGKILL
    assert (readme.read_bytes() != before) is target_was_committed

    run = _prepared_run(vault)
    _, refusal = _doctor(tmp_path, vault, "--json", expected=4)
    assert refusal["error"]["code"] == 4
    assert run.name in refusal["error"]["message"]

    _, undone = _doctor(
        tmp_path, vault, "undo", run.name, "--strict", "--json", expected=0
    )
    assert undone["undid_run"] == run.name
    assert readme.read_bytes() == before
    assert stat.S_IMODE(readme.stat().st_mode) == stat.S_IMODE(before_stat.st_mode)
    assert readme.stat().st_mtime_ns == before_stat.st_mtime_ns
    assert not (readme.parent / "transfer.lock").exists()
    events = [
        json.loads(line) for line in (run / "actions.jsonl").read_text().splitlines()
    ]
    assert events[-1]["event"] == "recovery_finished"


def test_two_process_fix_has_one_winner_and_one_concurrency_loser(tmp_path: Path):
    vault, _ = _corrupt_vault(tmp_path)
    command = [
        sys.executable,
        str(TEST_TOOL),
        "--vault",
        str(vault),
        "doctor",
        "fix",
        "--json",
    ]
    env = _doctor_env(tmp_path, hold_lock_ms=250)
    processes = [
        subprocess.Popen(
            command,
            cwd=REPO_ROOT,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        for _ in range(2)
    ]
    results = []
    try:
        for process in processes:
            stdout, stderr = process.communicate(timeout=20)
            assert stderr == ""
            results.append((process.returncode, json.loads(stdout)))
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)

    assert sorted(code for code, _ in results) == [0, 5]
    winner = next(report for code, report in results if code == 0)
    loser = next(report for code, report in results if code == 5)
    assert winner["target_actions"] == 1
    assert loser["error"]["code"] == 5
    _, healthy = _doctor(
        tmp_path, vault, "diagnose", "--only", "bundle.readme", "--json", expected=0
    )
    assert healthy["findings"] == []


def test_fixture_fix_is_idempotent_across_processes(tmp_path: Path):
    vault, _ = _corrupt_vault(tmp_path)
    _, first = _doctor(tmp_path, vault, "fix", "--json", expected=0)
    _, second = _doctor(tmp_path, vault, "fix", "--json", expected=0)
    assert first["target_actions"] == 1
    assert second["target_actions"] == 0
    assert second["summary"]["actions_taken"] == 0


def test_fixture_fix_and_undo_restore_corrupt_bytes(tmp_path: Path):
    vault, readme = _corrupt_vault(tmp_path)
    before = readme.read_bytes()
    before_stat = readme.stat()
    _, fixed = _doctor(tmp_path, vault, "fix", "--json", expected=0)
    subprocess.run(
        [str(FIXTURE / "assert.sh"), str(vault)],
        cwd=REPO_ROOT,
        env=_doctor_env(tmp_path),
        check=True,
        text=True,
        capture_output=True,
        timeout=20,
    )
    _, undone = _doctor(
        tmp_path, vault, "undo", fixed["run_id"], "--strict", "--json", expected=0
    )
    assert undone["undid_run"] == fixed["run_id"]
    assert readme.read_bytes() == before
    assert stat.S_IMODE(readme.stat().st_mode) == stat.S_IMODE(before_stat.st_mode)
    assert readme.stat().st_mtime_ns == before_stat.st_mtime_ns


def test_detector_result_is_unchanged_by_its_private_evidence(tmp_path: Path):
    vault, _ = _corrupt_vault(tmp_path)
    _, first = _doctor(
        tmp_path, vault, "diagnose", "--only", FM_ID, "--json", expected=1
    )
    _, second = _doctor(
        tmp_path, vault, "diagnose", "--only", FM_ID, "--json", expected=1
    )
    assert first["run_id"] != second["run_id"]
    assert first["findings"] == second["findings"]


def _canonical_capabilities(raw: str) -> str:
    value = json.loads(raw)
    value["tool_version"] = "[TOOL_VERSION]"
    value["identity"]["version"] = "[TOOL_VERSION]"
    return json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True) + "\n"


def _assert_golden(actual: str) -> None:
    if os.environ.get("UPDATE_GOLDENS") == "1":
        GOLDEN.parent.mkdir(parents=True, exist_ok=True)
        GOLDEN.write_text(actual, encoding="utf-8")
        return
    expected = GOLDEN.read_text(encoding="utf-8")
    if actual == expected:
        return
    actual_path = GOLDEN.with_suffix(".actual")
    actual_path.write_text(actual, encoding="utf-8")
    diff = "".join(
        difflib.unified_diff(
            expected.splitlines(keepends=True),
            actual.splitlines(keepends=True),
            fromfile=str(GOLDEN),
            tofile=str(actual_path),
        )
    )
    pytest.fail(
        f"doctor capabilities golden changed:\n{diff}\n"
        "Review the diff, then regenerate deliberately with "
        "UPDATE_GOLDENS=1 pytest "
        "tests/integration/test_doctor_safety.py::test_capabilities_match_golden"
    )


def test_capabilities_match_golden(tmp_path: Path):
    completed, capabilities = _doctor(
        tmp_path, None, "capabilities", "--json", expected=0
    )
    _assert_golden(_canonical_capabilities(completed.stdout))
    assert len(capabilities["detectors"]) == 9
    assert len(capabilities["fixers"]) == 4

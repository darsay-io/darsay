from __future__ import annotations

import fcntl
import hashlib
import json
import os
import socket
from pathlib import Path

import pytest

from darsay.cli import main
from darsay.doctor import (
    CHECKS,
    DoctorError,
    _capabilities,
    _finding,
    _stabilize_finding_ids,
)


def _partial_bundle(vault: Path) -> Path:
    bundle = vault / "acme--toy" / "deadbeefdead"
    bundle.mkdir(parents=True)
    (bundle / "transfer.json").write_text("{}\n", encoding="utf-8")
    return bundle


def _stale_lock(bundle: Path) -> bytes:
    payload = {
        "pid": 99999999,
        "host": socket.gethostname(),
        "started": "2026-01-01T00:00:00+00:00",
        "bundle": {
            "path": str(bundle.resolve()),
            "device": bundle.stat().st_dev,
            "inode": bundle.stat().st_ino,
        },
    }
    data = (json.dumps(payload, indent=2) + "\n").encode()
    (bundle / "transfer.lock").write_bytes(data)
    return data


def _json_stdout(capsys) -> dict:
    captured = capsys.readouterr()
    assert captured.err == ""
    return json.loads(captured.out)


def test_capabilities_are_registry_derived_and_offline():
    capabilities = _capabilities()
    assert [row["id"] for row in capabilities["checks"]] == [
        check.id for check in CHECKS
    ]
    assert capabilities["network"] == {"default": "disabled", "checks": []}
    assert "changes payload bytes" in capabilities["side_effects"]["never"]
    assert capabilities["exit_codes"]["5"].startswith("another doctor")
    assert {
        "artifacts_created",
        "network_attempts",
        "target_actions",
    } <= set(capabilities["report_schema"]["required"])


def test_finding_ids_are_stable_when_a_vault_is_relocated(tmp_path):
    first_vault = tmp_path / "first"
    second_vault = tmp_path / "second"
    first = [_finding("bundle.readme", "drift", first_vault / "repo/id/README.md")]
    second = [_finding("bundle.readme", "drift", second_vault / "repo/id/README.md")]

    _stabilize_finding_ids(first_vault, first)
    _stabilize_finding_ids(second_vault, second)

    assert first[0]["id"] == second[0]["id"]


def test_health_is_shallow_and_creates_no_artifacts(vault, capsys):
    assert main(["--vault", str(vault), "doctor", "health", "--json"]) == 0
    report = _json_stdout(capsys)
    assert report["status"] == "healthy"
    assert report["artifacts_created"] is False
    assert report["network_attempts"] == 0
    assert report["target_actions"] == 0
    assert report["elapsed_ms"] < 200
    assert not (vault / ".doctor").exists()


@pytest.mark.parametrize(
    ("bad_path", "kind", "expected_detail"),
    [
        (".doctor", "file", "doctor root is not a directory"),
        (".doctor/runs", "file", "doctor runs root is not a directory"),
        (".doctor/doctor.lock", "symlink", "doctor lock is a symlink"),
        (
            ".doctor/doctor.lock",
            "directory",
            "doctor lock is not a regular file",
        ),
    ],
)
def test_health_reports_unusable_evidence_paths_without_mutating_them(
    vault, tmp_path, capsys, bad_path, kind, expected_detail
):
    path = vault / bad_path
    path.parent.mkdir(parents=True, exist_ok=True)
    if kind == "file":
        path.write_bytes(b"sentinel doctor evidence bytes\n")
    elif kind == "directory":
        path.mkdir()
    else:
        outside = tmp_path / "outside-doctor-lock"
        outside.write_bytes(b"outside sentinel\n")
        path.symlink_to(outside)

    assert main(["--vault", str(vault), "doctor", "health", "--json"]) == 1
    report = _json_stdout(capsys)
    assert report["status"] == "degraded"
    assert report["exit_code"] == 1
    assert report["detail"] == expected_detail
    assert report["elapsed_ms"] < 200
    if kind == "file":
        assert path.read_bytes() == b"sentinel doctor evidence bytes\n"
    elif kind == "directory":
        assert list(path.iterdir()) == []
    else:
        assert path.is_symlink()
        assert path.resolve().read_bytes() == b"outside sentinel\n"


def test_missing_vault_is_reported_without_creating_it(tmp_path, capsys):
    missing = tmp_path / "misspelled-vault"

    assert main(["--vault", str(missing), "doctor", "health", "--json"]) == 1
    assert _json_stdout(capsys)["detail"] == "vault root does not exist"
    assert not missing.exists()

    assert main(["--vault", str(missing), "doctor", "--json"]) == 66
    assert _json_stdout(capsys)["error"]["code"] == 66
    assert not missing.exists()


@pytest.mark.parametrize(
    "doctor_args",
    [
        ["undo", "latest", "--json"],
        ["gc", "--before", "2026-01-01", "--yes", "--json"],
    ],
)
def test_mutating_history_commands_do_not_create_a_missing_vault(
    tmp_path, capsys, doctor_args
):
    missing = tmp_path / "mistyped-vault"

    assert main(["--vault", str(missing), "doctor", *doctor_args]) == 66
    assert _json_stdout(capsys)["error"]["code"] == 66
    assert not missing.exists()


def test_preexisting_doctor_symlink_is_refused_without_outside_writes(
    vault, tmp_path, capsys
):
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o755)
    before_mode = outside.stat().st_mode & 0o777
    (vault / ".doctor").symlink_to(outside, target_is_directory=True)

    assert main(["--vault", str(vault), "doctor", "--json"]) == 4
    assert _json_stdout(capsys)["error"]["code"] == 4
    assert list(outside.iterdir()) == []
    assert outside.stat().st_mode & 0o777 == before_mode


def test_diagnose_json_and_dry_run_do_not_change_stale_lock(vault, capsys):
    bundle = _partial_bundle(vault)
    original = _stale_lock(bundle)

    assert main(["--vault", str(vault), "doctor", "--json"]) == 1
    report = _json_stdout(capsys)
    assert report["artifacts_created"] is True
    assert report["network_attempts"] == 0
    assert report["target_actions"] == 0
    assert [row["check_id"] for row in report["findings"]] == ["transfer.lock"]
    assert report["findings"][0]["auto_fixable"] is True
    assert (bundle / "transfer.lock").read_bytes() == original


def test_robot_triage_plans_but_never_applies_a_fix(vault, capsys):
    bundle = _partial_bundle(vault)
    original = _stale_lock(bundle)

    assert main(["--vault", str(vault), "doctor", "--robot-triage"]) == 1
    report = _json_stdout(capsys)
    assert report["actions"] == []
    assert report["actions_planned"][0]["fixer_id"] == "transfer.lock.quarantine"
    assert report["recommended_command"].endswith("--fix --only transfer.lock")
    assert (bundle / "transfer.lock").read_bytes() == original

    assert (
        main(
            [
                "--vault",
                str(vault),
                "doctor",
                "--fix",
                "--dry-run",
                "--json",
            ]
        )
        == 2
    )
    dry = _json_stdout(capsys)
    assert dry["dry_run"] is True
    assert dry["actions"] == [
        {
            "action": "Rename",
            "fixer_id": "transfer.lock.quarantine",
            "path": "acme--toy/deadbeefdead/transfer.lock",
            "proposed": True,
        }
    ]
    assert (bundle / "transfer.lock").read_bytes() == original


def test_fix_quarantines_stale_lock_and_undo_restores_exact_bytes(vault, capsys):
    bundle = _partial_bundle(vault)
    original_lock = _stale_lock(bundle)
    original_ledger = (bundle / "transfer.json").read_bytes()

    assert main(["--vault", str(vault), "doctor", "fix", "--json"]) == 0
    fixed = _json_stdout(capsys)
    assert fixed["status"] == "healthy"
    assert not (bundle / "transfer.lock").exists()
    assert (bundle / "transfer.json").read_bytes() == original_ledger
    run = Path(fixed["artifacts"])
    actions = [
        json.loads(line) for line in (run / "actions.jsonl").read_text().splitlines()
    ]
    assert actions[0]["action"] == "Rename"
    assert actions[0]["before_sha256"]
    assert (run / actions[0]["backup"]).read_bytes() == original_lock
    assert (run / actions[0]["backup"]).stat().st_mode & 0o777 == 0o600

    assert (
        main(
            [
                "--vault",
                str(vault),
                "doctor",
                "undo",
                fixed["run_id"],
                "--json",
            ]
        )
        == 0
    )
    undone = _json_stdout(capsys)
    assert undone["undid_run"] == fixed["run_id"]
    assert (bundle / "transfer.lock").read_bytes() == original_lock
    assert (bundle / "transfer.json").read_bytes() == original_ledger


def test_fix_quarantines_disposable_broken_hydration(vault, capsys):
    bundle = _partial_bundle(vault)
    hydration = bundle / "hydration.json"
    hydration.write_text('{"env": {"python_executable": "/missing/python"}}\n')

    assert main(["--vault", str(vault), "doctor", "fix", "--json"]) == 0
    report = _json_stdout(capsys)
    assert not hydration.exists()
    assert report["artifacts_created"] is True
    assert report["network_attempts"] == 0
    assert report["target_actions"] == 1
    assert report["summary"]["actions"] == 1
    assert report["actions"][0]["fixer_id"] == "runtime.hydration.quarantine"


def test_malformed_manifest_is_manual_and_never_rewritten(vault, capsys):
    bundle = vault / "acme--toy" / "badbadbadbad"
    bundle.mkdir(parents=True)
    manifest = bundle / "manifest.json"
    original = b'{"schema_version": '
    manifest.write_bytes(original)

    assert main(["--vault", str(vault), "doctor", "fix", "--json"]) == 2
    report = _json_stdout(capsys)
    assert report["findings"][0]["check_id"] == "bundle.manifest"
    assert report["findings"][0]["auto_fixable"] is False
    assert report["actions"] == []
    assert manifest.read_bytes() == original


def test_concurrent_fixer_loses_with_stable_exit(vault, capsys):
    bundle = _partial_bundle(vault)
    _stale_lock(bundle)
    root = vault / ".doctor"
    root.mkdir()
    lock = (root / "doctor.lock").open("a+b")
    fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        assert main(["--vault", str(vault), "doctor", "fix", "--json"]) == 5
    finally:
        lock.close()
    error = _json_stdout(capsys)
    assert error["error"]["code"] == 5
    assert (bundle / "transfer.lock").exists()


def test_stale_lock_fixer_refuses_a_live_replacement(vault):
    from darsay.doctor import _apply_finding, _new_run, _scan

    bundle = _partial_bundle(vault)
    _stale_lock(bundle)
    findings, _ = _scan(vault)
    finding = next(row for row in findings if row["check_id"] == "transfer.lock")
    live = {
        "pid": os.getpid(),
        "host": socket.gethostname(),
        "started": "2026-08-30T12:00:00+00:00",
        "bundle": {
            "path": str(bundle.resolve()),
            "device": bundle.stat().st_dev,
            "inode": bundle.stat().st_ino,
        },
    }
    live_bytes = (json.dumps(live, sort_keys=True) + "\n").encode()
    lock = bundle / "transfer.lock"
    lock.write_bytes(live_bytes)
    _, run, _ = _new_run(vault, "toctou-test")

    with pytest.raises(DoctorError) as raised:
        _apply_finding(vault, run, finding)

    assert raised.value.code == 4
    assert lock.read_bytes() == live_bytes


def test_prepared_only_journal_blocks_new_runs_until_strict_undo(vault, capsys):
    from darsay.doctor import _append_action, _new_run

    bundle = _partial_bundle(vault)
    target = bundle / "README.md"
    before = b"operator bytes before interrupted repair\n"
    after = b"generated bytes committed before SIGTERM\n"
    target.write_bytes(before)
    before_info = target.stat()
    run_id, run, _ = _new_run(vault, "fix")
    relative = target.relative_to(vault)
    backup_relative = Path("backups") / relative
    backup = run / backup_relative
    backup.parent.mkdir(parents=True, exist_ok=True)
    backup.write_bytes(before)
    target.write_bytes(after)
    _append_action(
        run,
        {
            "schema_version": 1,
            "event": "mutation_prepared",
            "action_id": "interrupted-write",
            "action": "WriteFile",
            "op": "WriteFile",
            "run_id": run_id,
            "fixer_id": "bundle.readme.regenerate",
            "path": relative.as_posix(),
            "backup": backup_relative.as_posix(),
            "before_exists": True,
            "before_sha256": hashlib.sha256(before).hexdigest(),
            "before_mode": before_info.st_mode & 0o7777,
            "before_mtime_ns": before_info.st_mtime_ns,
            "after_sha256": hashlib.sha256(after).hexdigest(),
            "started_at_ns": 1,
            "finished_at_ns": None,
            "ok": None,
            "rolled_back": False,
        },
    )
    bundle_info = bundle.stat()
    (bundle / "transfer.lock").write_text(
        json.dumps(
            {
                "pid": 99999999,
                "host": socket.gethostname(),
                "started": "2026-08-30T12:00:00+00:00",
                "purpose": "darsay doctor repair",
                "doctor_run_id": run_id,
                "bundle": {
                    "path": str(bundle.resolve()),
                    "device": bundle_info.st_dev,
                    "inode": bundle_info.st_ino,
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )

    assert main(["--vault", str(vault), "doctor", "--json"]) == 4
    refused = _json_stdout(capsys)
    assert refused["error"]["code"] == 4
    assert "incomplete doctor mutation" in refused["error"]["message"]
    assert target.read_bytes() == after

    assert (
        main(
            [
                "--vault",
                str(vault),
                "doctor",
                "undo",
                run_id,
                "--strict",
                "--json",
            ]
        )
        == 0
    )
    _json_stdout(capsys)
    assert target.read_bytes() == before
    assert not (bundle / "transfer.lock").exists()
    source_events = [
        json.loads(line) for line in (run / "actions.jsonl").read_text().splitlines()
    ]
    assert source_events[-1]["event"] == "recovery_finished"
    assert source_events[-1]["action_ids"] == ["interrupted-write"]
    assert source_events[-1]["rolled_back"] is True

    assert (
        main(
            [
                "--vault",
                str(vault),
                "doctor",
                "--only",
                "config.parse",
                "--json",
            ]
        )
        == 0
    )
    assert _json_stdout(capsys)["findings"] == []


def test_undo_recovery_marker_failure_compensates_inverse_transaction(
    vault, capsys, monkeypatch
):
    from darsay.doctor import _append_action, _new_run

    bundle = _partial_bundle(vault)
    target = bundle / "README.md"
    before = b"operator bytes before interrupted repair\n"
    after = b"generated bytes committed before SIGTERM\n"
    target.write_bytes(before)
    before_info = target.stat()
    source_id, source_run, _ = _new_run(vault, "fix")
    relative = target.relative_to(vault)
    backup_relative = Path("backups") / relative
    backup = source_run / backup_relative
    backup.parent.mkdir(parents=True, exist_ok=True)
    backup.write_bytes(before)
    target.write_bytes(after)
    _append_action(
        source_run,
        {
            "schema_version": 1,
            "event": "mutation_prepared",
            "action_id": "interrupted-write",
            "action": "WriteFile",
            "op": "WriteFile",
            "run_id": source_id,
            "fixer_id": "bundle.readme.regenerate",
            "path": relative.as_posix(),
            "backup": backup_relative.as_posix(),
            "before_exists": True,
            "before_sha256": hashlib.sha256(before).hexdigest(),
            "before_mode": before_info.st_mode & 0o7777,
            "before_mtime_ns": before_info.st_mtime_ns,
            "after_sha256": hashlib.sha256(after).hexdigest(),
            "started_at_ns": 1,
            "finished_at_ns": None,
            "ok": None,
            "rolled_back": False,
        },
    )

    doctor = __import__("darsay.doctor", fromlist=["_append_action"])
    real_append = doctor._append_action

    def fail_recovery_marker(run_path, action):
        if action.get("event") == "recovery_finished":
            raise DoctorError("injected recovery marker failure", 74)
        real_append(run_path, action)

    monkeypatch.setattr("darsay.doctor._append_action", fail_recovery_marker)

    assert (
        main(
            [
                "--vault",
                str(vault),
                "doctor",
                "undo",
                source_id,
                "--strict",
                "--json",
            ]
        )
        == 74
    )
    error = _json_stdout(capsys)
    assert error["error"]["message"] == "injected recovery marker failure"
    assert target.read_bytes() == after

    runs = {}
    for run in (vault / ".doctor" / "runs").iterdir():
        meta = json.loads((run / "run.json").read_text(encoding="utf-8"))
        runs.setdefault(meta["command"], []).append(run)
    undo_run = runs["undo"][0]
    rollback_run = runs["rollback"][0]
    for journal in (undo_run / "actions.jsonl", rollback_run / "actions.jsonl"):
        events = [json.loads(line) for line in journal.read_text().splitlines()]
        assert [event["event"] for event in events] == [
            "mutation_prepared",
            "mutation_finished",
        ]
        assert events[-1]["ok"] is True

    source_events = [
        json.loads(line)
        for line in (source_run / "actions.jsonl").read_text().splitlines()
    ]
    assert [event["event"] for event in source_events] == ["mutation_prepared"]

    assert main(["--vault", str(vault), "doctor", "--json"]) == 4
    refused = _json_stdout(capsys)
    assert "incomplete doctor mutation" in refused["error"]["message"]
    assert target.read_bytes() == after


def test_undo_preflights_all_actions_before_mutating(vault, capsys):
    from darsay.doctor import _new_run, mutate

    bundle = _partial_bundle(vault)
    readme = bundle / "README.md"
    hydration = bundle / "hydration.json"
    readme.write_bytes(b"before-readme")
    hydration.write_bytes(b"before-hydration")
    run_id, run, _ = _new_run(vault, "multi-action-test")
    mutate(
        vault,
        run,
        readme,
        operation="WriteFile",
        fixer="bundle.readme.regenerate",
        data=b"after-readme",
    )
    mutate(
        vault,
        run,
        hydration,
        operation="Rename",
        fixer="runtime.hydration.quarantine",
    )
    readme.write_bytes(b"post-fix-user-edit")

    assert main(["--vault", str(vault), "doctor", "undo", run_id, "--json"]) == 4
    assert _json_stdout(capsys)["error"]["code"] == 4
    assert readme.read_bytes() == b"post-fix-user-edit"
    assert not hydration.exists()


def test_undo_refuses_tampered_action_path(vault, capsys):
    bundle = _partial_bundle(vault)
    _stale_lock(bundle)
    assert main(["--vault", str(vault), "doctor", "fix", "--json"]) == 0
    report = _json_stdout(capsys)
    journal = Path(report["artifacts"]) / "actions.jsonl"
    events = [json.loads(line) for line in journal.read_text().splitlines()]
    action = events[0]
    action["path"] = "../outside"
    journal.write_text(
        "\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8"
    )

    assert (
        main(
            [
                "--vault",
                str(vault),
                "doctor",
                "undo",
                report["run_id"],
                "--json",
            ]
        )
        == 4
    )
    error = _json_stdout(capsys)
    assert error["error"]["code"] == 4
    assert not (bundle / "transfer.lock").exists()


@pytest.mark.parametrize("relative", ["manifest.json", "model/payload.bin"])
def test_tampered_journal_cannot_redirect_undo_to_immutable_bundle_state(
    vault, capsys, relative
):
    bundle = _partial_bundle(vault)
    _stale_lock(bundle)

    assert main(["--vault", str(vault), "doctor", "fix", "--json"]) == 0
    report = _json_stdout(capsys)
    target = bundle / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    original = b"immutable archival bytes\n"
    target.write_bytes(original)
    run = Path(report["artifacts"])
    journal = run / "actions.jsonl"
    events = [json.loads(line) for line in journal.read_text().splitlines()]
    prepared = events[0]
    forged_backup = b"attacker-selected replacement\n"
    (run / prepared["backup"]).write_bytes(forged_backup)
    prepared["path"] = target.relative_to(vault).as_posix()
    prepared["before_exists"] = True
    prepared["before_sha256"] = hashlib.sha256(forged_backup).hexdigest()
    prepared["after_sha256"] = hashlib.sha256(original).hexdigest()
    journal.write_text(
        "\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8"
    )

    assert (
        main(
            [
                "--vault",
                str(vault),
                "doctor",
                "undo",
                report["run_id"],
                "--json",
            ]
        )
        == 4
    )
    assert _json_stdout(capsys)["error"]["code"] == 4
    assert target.read_bytes() == original


@pytest.mark.parametrize("relative", ["manifest.json", "model/payload.bin"])
def test_mutate_chokepoint_rejects_immutable_bundle_targets(vault, relative):
    from darsay.doctor import _new_run, mutate

    bundle = _partial_bundle(vault)
    target = bundle / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    original = b"immutable archival bytes\n"
    target.write_bytes(original)
    _, run, _ = _new_run(vault, "scope-bypass-test")

    with pytest.raises(DoctorError) as raised:
        mutate(
            vault,
            run,
            target,
            operation="WriteFile",
            fixer="bundle.readme.regenerate",
            data=b"forged replacement\n",
        )

    assert raised.value.code == 4
    assert target.read_bytes() == original
    assert (run / "actions.jsonl").read_bytes() == b""


def test_doctor_usage_errors_map_to_64(capsys):
    assert main(["doctor", "not-a-command"]) == 64
    assert "invalid choice" in capsys.readouterr().err


def test_unknown_only_selector_is_side_effect_free(vault, capsys):
    assert main(["--vault", str(vault), "doctor", "--only", "typo", "--json"]) == 64
    assert _json_stdout(capsys)["error"]["code"] == 64
    assert not (vault / ".doctor").exists()


def test_since_is_incremental_and_invalid_timestamp_is_side_effect_free(vault, capsys):
    bundle = _partial_bundle(vault)
    _stale_lock(bundle)
    old = 1_700_000_000
    for path in sorted(bundle.rglob("*"), reverse=True):
        os.utime(path, (old, old), follow_symlinks=False)
    os.utime(bundle, (old, old))

    assert (
        main(
            [
                "--vault",
                str(vault),
                "doctor",
                "--since",
                "2030-01-01T00:00:00Z",
                "--json",
            ]
        )
        == 0
    )
    assert _json_stdout(capsys)["findings"] == []

    clean = vault.parent / "invalid-since"
    assert (
        main(
            [
                "--vault",
                str(clean),
                "doctor",
                "--since",
                "not-a-time",
                "--json",
            ]
        )
        == 64
    )
    assert _json_stdout(capsys)["error"]["code"] == 64
    assert not clean.exists()


def test_doctor_does_not_change_existing_vault_mode(vault, capsys):
    vault.chmod(0o755)
    assert main(["--vault", str(vault), "doctor", "--json"]) == 0
    _json_stdout(capsys)
    assert vault.stat().st_mode & 0o777 == 0o755


def test_latest_symlink_and_run_references_stay_inside_vault(vault, capsys, tmp_path):
    assert main(["--vault", str(vault), "doctor", "--json"]) == 0
    _json_stdout(capsys)
    latest = vault / ".doctor" / "latest"
    if latest.is_symlink():
        latest.unlink()
        latest.symlink_to(tmp_path)
        assert main(["--vault", str(vault), "doctor", "undo", "latest", "--json"]) == 4
        error = _json_stdout(capsys)
        assert error["error"]["code"] == 4


def test_doctor_never_reads_network_proxy_environment(vault, capsys, monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "https://token=super-secret@example.invalid")
    before = dict(os.environ)
    assert main(["--vault", str(vault), "doctor", "--json"]) == 0
    report = _json_stdout(capsys)
    assert "super-secret" not in json.dumps(report)
    assert dict(os.environ) == before


def test_action_journal_failure_prevents_target_mutation(vault, capsys, monkeypatch):
    bundle = _partial_bundle(vault)
    original = _stale_lock(bundle)

    def fail_journal(*args, **kwargs):
        raise DoctorError("injected journal failure", 74)

    monkeypatch.setattr("darsay.doctor._append_action", fail_journal)
    assert main(["--vault", str(vault), "doctor", "fix", "--json"]) == 74
    error = _json_stdout(capsys)
    assert error["error"]["code"] == 74
    assert (bundle / "transfer.lock").read_bytes() == original


def test_atomic_write_failure_leaves_original_bytes_and_reports_io_error(
    vault, capsys, monkeypatch
):
    bundle = _partial_bundle(vault)
    readme = bundle / "README.md"
    original = b"original generated output\n"
    readme.write_bytes(original)
    real_replace = __import__(
        "darsay.doctor", fromlist=["mutate_replace_at"]
    ).mutate_replace_at
    injected = False

    def fail_once(parent_fd, temp_name, target_name):
        nonlocal injected
        if target_name == "README.md" and not injected:
            injected = True
            raise OSError("injected atomic write failure")
        return real_replace(parent_fd, temp_name, target_name)

    monkeypatch.setattr("darsay.doctor.mutate_replace_at", fail_once)
    # Exercise the allowlisted WriteFile branch against a private run.
    from darsay.doctor import _new_run, mutate

    _, run, _ = _new_run(vault, "failure-injection")
    with pytest.raises(OSError, match="injected atomic write failure"):
        mutate(
            vault,
            run,
            readme,
            operation="WriteFile",
            fixer="bundle.readme.regenerate",
            data=b"replacement",
        )
    assert readme.read_bytes() == original
    events = [
        json.loads(line) for line in (run / "actions.jsonl").read_text().splitlines()
    ]
    assert events[0]["event"] == "mutation_prepared"
    assert events[0]["ok"] is None
    assert events[0]["finished_at_ns"] is None
    assert events[1]["event"] == "mutation_finished"
    assert events[1]["ok"] is False
    assert events[1]["finished_at_ns"] >= events[0]["started_at_ns"]

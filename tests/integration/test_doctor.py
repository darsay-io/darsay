from __future__ import annotations

import hashlib
import json
import os
import socket
from pathlib import Path

import pytest

from darsay.cli import main
from darsay.doctor import DoctorError
from tests.integration.conftest import archive_quiet
from tests.payloads import model_files


def _payload_hashes(bundle: Path) -> dict[str, str]:
    return {
        path.relative_to(bundle).as_posix(): hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
        for path in (bundle / "model").rglob("*")
        if path.is_file()
    }


def _live_lock(bundle: Path) -> tuple[Path, bytes]:
    info = bundle.stat()
    lock_bytes = (
        json.dumps(
            {
                "pid": os.getpid(),
                "host": socket.gethostname(),
                "started": "2026-08-30T12:00:00+00:00",
                "bundle": {
                    "path": str(bundle.resolve()),
                    "device": info.st_dev,
                    "inode": info.st_ino,
                },
            },
            sort_keys=True,
        )
        + "\n"
    ).encode()
    lock = bundle / "transfer.lock"
    lock.write_bytes(lock_bytes)
    return lock, lock_bytes


def test_doctor_regenerates_only_derived_readme_and_undoes_byte_exactly(
    vault, test_provider, capsys
):
    test_provider.add_repo("acme/toy", model_files())
    bundle = archive_quiet("test:acme/toy", vault=vault)
    payload_before = _payload_hashes(bundle)
    curation_before = (bundle / "curation.md").read_bytes()
    readme = bundle / "README.md"
    drifted = b"operator accidentally edited generated output\n"
    readme.write_bytes(drifted)

    assert main(["--vault", str(vault), "doctor", "fix", "--json"]) == 0
    fixed = __import__("json").loads(capsys.readouterr().out)
    assert readme.read_bytes() != drifted
    assert _payload_hashes(bundle) == payload_before
    assert (bundle / "curation.md").read_bytes() == curation_before
    assert fixed["actions"][0]["fixer_id"] == "bundle.readme.regenerate"

    generated = readme.read_bytes()
    live_lock, _ = _live_lock(bundle)
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
        == 5
    )
    assert json.loads(capsys.readouterr().out)["error"]["code"] == 5
    assert readme.read_bytes() == generated
    live_lock.unlink()

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
    capsys.readouterr()
    assert readme.read_bytes() == drifted
    assert _payload_hashes(bundle) == payload_before
    assert (bundle / "curation.md").read_bytes() == curation_before


def test_live_project_lock_blocks_all_bundle_fixes_before_first_mutation(
    vault, test_provider, capsys
):
    test_provider.add_repo("acme/toy", model_files())
    bundle = archive_quiet("test:acme/toy", vault=vault)
    readme = bundle / "README.md"
    hydration = bundle / "hydration.json"
    stale_readme = b"operator drift that must not change while transfer is live\n"
    broken_hydration = b'{"env": {"python_executable": "/missing/python"}}\n'
    readme.write_bytes(stale_readme)
    hydration.write_bytes(broken_hydration)
    lock, lock_bytes = _live_lock(bundle)

    assert main(["--vault", str(vault), "doctor", "fix", "--json"]) == 5
    error = json.loads(capsys.readouterr().out)
    assert error["error"]["code"] == 5
    assert "live bundle transfer lock" in error["error"]["message"]
    assert readme.read_bytes() == stale_readme
    assert hydration.read_bytes() == broken_hydration
    assert lock.read_bytes() == lock_bytes
    journals = list((vault / ".doctor" / "runs").glob("*/actions.jsonl"))
    assert journals
    assert all(path.read_bytes() == b"" for path in journals)


def test_doctor_missing_readme_fix_is_undoable_without_deletion(
    vault, test_provider, capsys
):
    test_provider.add_repo("acme/toy", model_files())
    bundle = archive_quiet("test:acme/toy", vault=vault)
    readme = bundle / "README.md"
    readme.unlink()

    assert main(["--vault", str(vault), "doctor", "fix", "--json"]) == 0
    fixed = __import__("json").loads(capsys.readouterr().out)
    assert readme.is_file()

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
    capsys.readouterr()
    assert not readme.exists()
    undo_run = (vault / ".doctor" / "latest").resolve()
    quarantined = undo_run / "quarantine" / readme.relative_to(vault)
    assert quarantined.is_file()


def test_doctor_detects_payload_corruption_but_never_repairs_it(
    vault, test_provider, capsys
):
    test_provider.add_repo("acme/toy", model_files())
    bundle = archive_quiet("test:acme/toy", vault=vault)
    payload = bundle / "model" / "model.safetensors"
    payload.write_bytes(payload.read_bytes() + b"corruption")
    corrupted = payload.read_bytes()

    assert main(["--vault", str(vault), "doctor", "fix", "--json"]) == 2
    report = __import__("json").loads(capsys.readouterr().out)
    payload_findings = [
        finding
        for finding in report["findings"]
        if finding["check_id"] == "bundle.payload"
    ]
    assert len(payload_findings) == 1
    assert payload_findings[0]["auto_fixable"] is False
    assert payload.read_bytes() == corrupted


def test_doctor_reports_payload_symlink_without_following_it(
    vault, test_provider, capsys, tmp_path
):
    test_provider.add_repo("acme/toy", model_files())
    bundle = archive_quiet("test:acme/toy", vault=vault)
    outside = tmp_path / "token=do-not-read"
    outside.write_text("secret sentinel", encoding="utf-8")
    (bundle / "model" / "outside-link").symlink_to(outside)

    assert main(["--vault", str(vault), "doctor", "--json"]) == 1
    report = __import__("json").loads(capsys.readouterr().out)
    path_findings = [
        finding
        for finding in report["findings"]
        if finding["check_id"] == "bundle.paths"
    ]
    assert len(path_findings) == 1
    assert "secret sentinel" not in __import__("json").dumps(report)


def test_unsafe_manifest_path_suppresses_readme_repair(vault, test_provider, capsys):
    json = __import__("json")
    test_provider.add_repo("acme/toy", model_files())
    bundle = archive_quiet("test:acme/toy", vault=vault)
    manifest_path = bundle / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["inventory"]["files"][0]["path"] = "model/../outside"
    manifest_path.write_text(json.dumps(manifest) + "\n", encoding="utf-8")
    readme_before = (bundle / "README.md").read_bytes()

    assert main(["--vault", str(vault), "doctor", "fix", "--json"]) == 2
    report = json.loads(capsys.readouterr().out)

    assert any(f["check_id"] == "bundle.paths" for f in report["findings"])
    assert all(f["check_id"] != "bundle.readme" for f in report["findings"])
    assert all(a["fixer_id"] != "bundle.readme.regenerate" for a in report["actions"])
    assert (bundle / "README.md").read_bytes() == readme_before


def test_payload_root_traversal_is_refused_and_only_bounds_execution(
    vault, test_provider, capsys, tmp_path, monkeypatch
):
    json = __import__("json")
    test_provider.add_repo("acme/toy", model_files())
    bundle = archive_quiet("test:acme/toy", vault=vault)
    manifest_path = bundle / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["inventory"]["layout"]["payload_root"] = "../../../outside"
    manifest_path.write_text(json.dumps(manifest) + "\n", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir(exist_ok=True)
    sentinel = outside / "sentinel.bin"
    sentinel.write_bytes(b"must-not-be-read")
    reads = []

    def record_hash(path, **kwargs):
        reads.append(Path(path).resolve())
        return {"sha256": hashlib.sha256(Path(path).read_bytes()).hexdigest()}

    monkeypatch.setattr("darsay.hashing.hash_file", record_hash)

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
    assert __import__("json").loads(capsys.readouterr().out)["findings"] == []
    assert reads == []

    assert main(["--vault", str(vault), "doctor", "--json"]) == 1
    report = __import__("json").loads(capsys.readouterr().out)
    assert any(row["check_id"] == "bundle.paths" for row in report["findings"])
    assert sentinel.resolve() not in reads


def test_parent_directory_swap_cannot_redirect_readme_write(
    vault, test_provider, tmp_path, monkeypatch
):
    from darsay.doctor import _apply_finding, _new_run, _scan

    test_provider.add_repo("acme/toy", model_files())
    bundle = archive_quiet("test:acme/toy", vault=vault)
    readme = bundle / "README.md"
    readme.write_bytes(b"stale generated output\n")
    parked = tmp_path / "parked-bundle"
    outside = tmp_path / "outside"
    outside.mkdir()
    outside_readme = outside / "README.md"
    outside_original = b"outside bytes must survive\n"
    outside_readme.write_bytes(outside_original)
    findings, _ = _scan(vault, checks={"bundle.readme"})
    finding = next(row for row in findings if row["check_id"] == "bundle.readme")
    _, run, _ = _new_run(vault, "parent-swap-test")
    doctor = __import__("darsay.doctor", fromlist=["_append_action"])
    real_append = doctor._append_action
    swapped = False

    def swap_after_prepare(run_path, action):
        nonlocal swapped
        real_append(run_path, action)
        if action.get("event") == "mutation_prepared" and not swapped:
            swapped = True
            bundle.rename(parked)
            bundle.symlink_to(outside, target_is_directory=True)

    monkeypatch.setattr("darsay.doctor._append_action", swap_after_prepare)

    with pytest.raises(DoctorError) as raised:
        _apply_finding(vault, run, finding)

    assert getattr(raised.value, "code", None) == 4
    assert outside_readme.read_bytes() == outside_original
    assert (parked / "README.md").read_bytes() == b"stale generated output\n"
    assert not list(parked.glob("*.doctor.tmp.*"))


def test_doctor_regenerates_a_stale_hash_list_and_undoes_it(
    vault, test_provider, capsys
):
    test_provider.add_repo("acme/toy", model_files())
    bundle = archive_quiet("test:acme/toy", vault=vault)
    sums = bundle / "SHA256SUMS"
    good = sums.read_bytes()
    sums.write_bytes(b"edited by hand\n")

    assert main(["--vault", str(vault), "doctor", "--json"]) == 1
    report = json.loads(capsys.readouterr().out)
    finding = next(
        f for f in report["findings"] if f["check_id"] == "bundle.sha256sums"
    )
    assert finding["fixer_id"] == "bundle.sha256sums.regenerate"

    argv = ["--vault", str(vault), "doctor", "fix", "--only", "bundle.sha256sums"]
    assert main([*argv, "--json"]) == 0
    fixed = json.loads(capsys.readouterr().out)
    assert sums.read_bytes() == good
    assert fixed["actions"][0]["fixer_id"] == "bundle.sha256sums.regenerate"

    assert (
        main(["--vault", str(vault), "doctor", "undo", fixed["run_id"], "--json"]) == 0
    )
    capsys.readouterr()
    assert sums.read_bytes() == b"edited by hand\n"

    sums.unlink()
    assert main([*argv, "--json"]) == 0
    capsys.readouterr()
    assert sums.read_bytes() == good

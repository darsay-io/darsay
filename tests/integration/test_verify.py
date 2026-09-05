from __future__ import annotations

import json

from darsay.archiver import load_manifest
from darsay.hashing import hash_file
from darsay.identity import machine_name
from darsay.verify import verify_bundle
from tests.conftest import silent
from tests.integration.conftest import archive_quiet
from tests.payloads import model_files


def test_verify_pass_does_not_touch_payload(vault, test_provider):
    test_provider.add_repo("acme/toy", model_files())
    bundle = archive_quiet("test:acme/toy", vault=vault)
    before = {
        path: hash_file(path, with_blake3=False)["sha256"]
        for path in (bundle / "model").rglob("*")
        if path.is_file()
    }
    report = verify_bundle(bundle, progress=silent)
    assert report["result"] == "pass"
    after = {
        path: hash_file(path, with_blake3=False)["sha256"]
        for path in (bundle / "model").rglob("*")
        if path.is_file()
    }
    assert after == before


def test_verify_detects_modified_missing_extra(vault, test_provider):
    test_provider.add_repo("acme/toy", model_files())
    bundle = archive_quiet("test:acme/toy", vault=vault)
    (bundle / "model" / "model.safetensors").write_bytes(b"tampered")
    (bundle / "model" / "config.json").unlink()
    (bundle / "model" / "extra.bin").write_bytes(b"new")
    report = verify_bundle(bundle, progress=silent)
    assert report["result"] == "fail"
    checksum = report["checksum"]
    assert any("model.safetensors" in p for p in checksum["mismatched"])
    assert any("config.json" in p for p in checksum["missing"])
    assert any("extra.bin" in p for p in checksum["extra"])
    manifest = load_manifest(bundle)
    assert manifest["security"]["integrity_status"] == "compromised"
    history = json.loads((bundle / "verification.json").read_text())
    assert history["latest"]["result"] == "fail"
    assert len(history["history"]) >= 2  # initial archive + this re-verify


def test_verify_heals_integrity_after_restore(vault, test_provider):
    test_provider.add_repo("acme/toy", model_files())
    bundle = archive_quiet("test:acme/toy", vault=vault)
    original = (bundle / "model" / "model.safetensors").read_bytes()
    (bundle / "model" / "model.safetensors").write_bytes(b"tampered")
    assert verify_bundle(bundle, progress=silent)["result"] == "fail"
    assert load_manifest(bundle)["security"]["integrity_status"] == "compromised"
    (bundle / "model" / "model.safetensors").write_bytes(original)
    assert verify_bundle(bundle, progress=silent)["result"] == "pass"
    assert (
        load_manifest(bundle)["security"]["integrity_status"]
        == "verified-against-upstream"
    )
    # The compromise is still in the append-only log.
    changes = load_manifest(bundle)["security"]["unexpected_changes"]
    assert any(c["type"] == "modified" for c in changes)


def test_verify_does_not_heal_upstream_mismatch(vault, test_provider):
    test_provider.add_repo("acme/toy", model_files())
    bundle = archive_quiet("test:acme/toy", vault=vault)
    manifest = load_manifest(bundle)
    manifest["security"]["integrity_status"] = "upstream-mismatch"
    (bundle / "manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    assert verify_bundle(bundle, progress=silent)["result"] == "pass"
    assert load_manifest(bundle)["security"]["integrity_status"] == "upstream-mismatch"


def test_verify_records_where_it_ran(tmp_path, vault, test_provider, capsys):
    """An rsync'd copy verified where it landed carries a true location."""
    import shutil

    from darsay.cli import main

    test_provider.add_repo("acme/toy", model_files())
    bundle = archive_quiet("test:acme/toy", vault=vault)
    old = load_manifest(bundle)["archive"]["location"]
    assert old == str(bundle.resolve())

    # In place: nothing to say, nothing changes.
    report = verify_bundle(bundle, progress=silent)
    assert "relocated_from" not in report
    assert report["location"] == old and report["host"] == machine_name()

    other = tmp_path / "nas"
    copy = other / bundle.parent.name / bundle.name
    shutil.copytree(bundle, copy)
    assert main(["--vault", str(other), "verify", str(copy)]) == 0
    out = capsys.readouterr().out
    assert "Verification: PASS" in out
    assert (
        f"Location: {copy.resolve()} on {machine_name()}  "
        f"(the record said {old} on {machine_name()})"
    ) in out

    manifest = load_manifest(copy)
    assert manifest["archive"]["location"] == str(copy.resolve())
    assert "moves" not in manifest["archive"], "an rsync is not a move"
    assert str(copy.resolve()) in (copy / "README.md").read_text(encoding="utf-8")
    history = json.loads((copy / "verification.json").read_text(encoding="utf-8"))
    assert history["latest"]["location"] == str(copy.resolve())
    assert history["history"][0]["location"] == old, "archive-time entry kept"
    assert f"- **Where:** `{copy.resolve()}` on" in (
        copy / "VERIFICATION.md"
    ).read_text(encoding="utf-8")
    # The original is untouched.
    assert load_manifest(bundle)["archive"]["location"] == old

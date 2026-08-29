from __future__ import annotations

import json
import shutil

import pytest

from darsay.archiver import load_manifest
from darsay.transfer import (
    PartialTransfer,
    assemble_partials,
    load_ledger,
    transfer_lock,
    transfer_plan,
)
from tests.conftest import silent
from tests.integration.conftest import archive_quiet
from tests.payloads import model_files


def test_archive_emits_payload_progress_log_lines(vault, test_provider, monkeypatch):
    """Integration: archive against TestProvider reports whole-payload progress."""
    monkeypatch.setenv("DARSAY_PROGRESS", "line")
    from darsay.archiver import archive

    test_provider.add_repo("acme/toy", model_files())
    logs: list[str] = []
    bundle = archive("test:acme/toy", vault=vault, progress=logs.append, jobs=1)
    text = "\n".join(str(item) for item in logs)
    assert bundle is not None
    assert (bundle / "manifest.json").is_file()
    assert "Transfer plan:" in text
    assert "small files" in text
    assert "%" in text
    assert "files" in text


def test_archive_progress_off_still_completes(vault, test_provider, monkeypatch):
    monkeypatch.setenv("DARSAY_PROGRESS", "0")
    from darsay.archiver import archive

    test_provider.add_repo("acme/toy", model_files())
    logs: list[str] = []
    bundle = archive("test:acme/toy", vault=vault, progress=logs.append, jobs=1)
    text = "\n".join(str(item) for item in logs)
    assert bundle is not None
    assert "Transfer plan:" in text
    assert "%" not in text


def test_budget_stop_is_resumable(vault, test_provider):
    files = model_files()
    test_provider.add_repo("acme/toy", files)
    with pytest.raises(PartialTransfer) as stopped:
        archive_quiet("test:acme/toy", vault=vault, max_bytes=1)
    bundle = stopped.value.bundle_dir
    assert not (bundle / "manifest.json").is_file()
    ledger = load_ledger(bundle)
    plan = transfer_plan(bundle / "model", ledger)
    assert plan["complete"] is False
    assert plan["files"]["verified"] >= 1
    # Ledger must not embed this machine's vault path.
    dumped = json.dumps(ledger)
    assert str(vault.resolve()) not in dumped
    assert str(bundle.resolve()) not in dumped

    # Resume completes without re-downloading verified files.
    downloaded_before = list(test_provider.downloads)
    bundle = archive_quiet("test:acme/toy", vault=vault)
    manifest = load_manifest(bundle)
    assert manifest["inventory"]["file_count"] == len(files)
    # Verified files from the first session should not be fetched again.
    first_verified = [
        path
        for path, state in ledger["files"].items()
        if state.get("status") == "verified"
    ]
    resumed_downloads = test_provider.downloads[len(downloaded_before) :]
    assert not set(first_verified) & set(resumed_downloads)


def test_disk_floor_pauses_cleanly_and_resumes(vault, test_provider, monkeypatch):
    from types import SimpleNamespace

    files = model_files()
    test_provider.add_repo("acme/toy", files)
    disk = SimpleNamespace(free=1 * 1024**3)
    monkeypatch.setattr("darsay.transfer.shutil.disk_usage", lambda path: disk)
    with pytest.raises(PartialTransfer) as stopped:
        archive_quiet("test:acme/toy", vault=vault, min_free=2 * 1024**3)
    assert stopped.value.reason == "disk"
    bundle = stopped.value.bundle_dir
    assert not (bundle / "manifest.json").is_file()
    ledger = load_ledger(bundle)
    assert ledger["sessions"][-1]["end_reason"] == "disk"

    # Space cleared: the same command converges.
    disk.free = 100 * 1024**3
    bundle = archive_quiet("test:acme/toy", vault=vault, min_free=2 * 1024**3)
    manifest = load_manifest(bundle)
    assert manifest["inventory"]["file_count"] == len(files)


def test_vault_config_floor_applies_and_zero_disables(
    vault, test_provider, monkeypatch
):
    monkeypatch.delenv("DARSAY_MIN_FREE", raising=False)
    test_provider.add_repo("acme/toy", model_files())
    # A 1 PiB floor is unsatisfiable on any real machine, so the configured
    # vault pauses deterministically without faking disk state.
    (vault / "config.toml").write_text(
        '[transfer]\nmin_free = "1024T"\n', encoding="utf-8"
    )
    with pytest.raises(PartialTransfer) as stopped:
        archive_quiet("test:acme/toy", vault=vault)
    assert stopped.value.reason == "disk"

    # An explicit 0 (the CLI's --min-free 0) disables the floor for one run.
    bundle = archive_quiet("test:acme/toy", vault=vault, min_free=0)
    assert (bundle / "manifest.json").is_file()


def test_reconcile_adopts_existing_bytes(vault, test_provider):
    files = model_files()
    test_provider.add_repo("acme/toy", files)
    # Plant a full payload before archive; reconcile should adopt it.
    from darsay.archiver import bundle_dir_for
    from darsay.sources import parse_source

    ref = parse_source("test:acme/toy")
    # Pin first so we know the revision prefix.
    snapshot_rev = test_provider.repos[("acme/toy", "main")].revision
    dest = bundle_dir_for(vault, ref, snapshot_rev)
    payload = dest / "model"
    payload.mkdir(parents=True)
    for name, data in files.items():
        path = payload / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    bundle = archive_quiet("test:acme/toy", vault=vault)
    manifest = load_manifest(bundle)
    assert manifest["source"]["transfer"]["bytes_adopted"] > 0
    assert test_provider.downloads == []


def test_reconcile_discards_unexpected_and_size_mismatch(
    vault, test_provider, tmp_path
):
    from darsay.sources import parse_source
    from darsay.transfer import begin_session, new_ledger, reconcile

    files = {"keep.bin": b"abcd", "good.bin": b"xxxx"}
    test_provider.add_repo("acme/toy", files)
    source = parse_source("test:acme/toy")
    snapshot = test_provider.pin(source, "main")
    bundle = tmp_path / "partial"
    payload = bundle / "model"
    payload.mkdir(parents=True)
    (payload / "keep.bin").write_bytes(b"abcd")
    (payload / "good.bin").write_bytes(b"wrong-size")  # size mismatch
    (payload / "stray.bin").write_bytes(b"nope")  # not in inventory
    ledger = new_ledger(snapshot)
    from darsay.transfer import save_ledger

    save_ledger(bundle, ledger)
    session = begin_session(bundle, ledger)
    plan = reconcile(bundle, payload, ledger, session, progress=silent)
    assert not (payload / "stray.bin").exists()
    assert not (payload / "good.bin").exists()
    assert (payload / "keep.bin").exists()
    assert ledger["files"]["keep.bin"]["status"] == "verified"
    assert ledger["files"]["good.bin"]["status"] == "missing"
    assert plan["files"]["verified"] == 1


def test_copied_partial_resumes_in_another_vault(vault, test_provider, tmp_path):
    test_provider.add_repo("acme/toy", model_files())
    with pytest.raises(PartialTransfer) as stopped:
        archive_quiet("test:acme/toy", vault=vault, max_bytes=1)
    source_bundle = stopped.value.bundle_dir
    other_vault = tmp_path / "other-vault"
    dest = other_vault / source_bundle.parent.name / source_bundle.name
    shutil.copytree(source_bundle, dest)
    # A copied lock must not block the new machine.
    (dest / "transfer.lock").write_text(
        json.dumps(
            {
                "pid": 1,
                "host": "other-host",
                "started": "2026-01-01T00:00:00+00:00",
                "bundle": {"path": "/original/path", "device": 1, "inode": 1},
            }
        )
    )
    bundle = archive_quiet("test:acme/toy", vault=other_vault)
    assert (bundle / "manifest.json").is_file()


def test_sibling_blob_reuse_skips_network(vault, test_provider):
    shared = b"shared-license-text\n"
    v1 = model_files(extra={"LICENSE": shared})
    v2 = model_files(extra={"LICENSE": shared, "notes.txt": b"rev2"})
    test_provider.add_repo(
        "acme/toy",
        v1,
        revision="1" * 40,
        revision_ref="v1",
    )
    test_provider.add_repo(
        "acme/toy",
        v2,
        revision="2" * 40,
        revision_ref="v2",
    )
    archive_quiet("test:acme/toy", vault=vault, revision="v1")
    test_provider.downloads.clear()
    bundle = archive_quiet("test:acme/toy", vault=vault, revision="v2")
    manifest = load_manifest(bundle)
    mirrors = manifest["source"]["mirrors_used"]
    assert any(item.startswith("local:") for item in mirrors)
    assert "LICENSE" not in test_provider.downloads


def test_assemble_combines_complementary_partials(vault, test_provider, tmp_path):
    files = model_files()
    test_provider.add_repo("acme/toy", files)
    names = sorted(files)
    # Two participants stop after disjoint prefixes by using shards + tiny budget.
    partials = []
    for shard in ((1, 2), (2, 2)):
        sub_vault = tmp_path / f"lane{shard[0]}"
        sub_vault.mkdir()
        try:
            archive_quiet(
                "test:acme/toy",
                vault=sub_vault,
                shard=shard,
                max_bytes=1,
            )
        except PartialTransfer as stop:
            partials.append(stop.bundle_dir)
        else:
            # Budget might complete if the first assigned file is tiny and
            # the rest fit; fall back to the registered-or-partial dir.
            found = list(sub_vault.glob("*/*"))
            assert found
            partials.append(found[0])
    dest, plan = assemble_partials(partials, vault, progress=silent)
    assert dest.is_dir()
    ledger = load_ledger(dest)
    assert ledger["repo_id"] == "acme/toy"
    # Assembling must not write a manifest; archive registers.
    assert not (dest / "manifest.json").is_file()
    if plan["complete"]:
        bundle = archive_quiet("test:acme/toy", vault=vault)
        assert load_manifest(bundle)["inventory"]["file_count"] == len(names)


def test_copied_lock_is_reclaimed(tmp_path):
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    lock = bundle / "transfer.lock"
    lock.write_text(
        json.dumps(
            {
                "pid": 999999,
                "host": "elsewhere",
                "started": "2026-01-01T00:00:00+00:00",
                "bundle": {"path": "/old", "device": 0, "inode": 0},
            }
        )
    )
    with transfer_lock(bundle, progress=silent):
        assert lock.is_file()
    assert not lock.exists()

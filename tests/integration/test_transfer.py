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


def test_network_outage_is_waited_out_and_recorded(vault, test_provider, monkeypatch):
    import socket

    monkeypatch.setattr("darsay.transfer.RECONNECT_WAITS_S", (0.01, 0.01))
    files = model_files()
    test_provider.add_repo("acme/toy", files)
    weight = "model.safetensors"
    test_provider.fail_next(
        weight,
        ConnectionResetError("reset by peer"),
        socket.gaierror(8, "nodename nor servname provided, or not known"),
    )
    bundle = archive_quiet("test:acme/toy", vault=vault, max_offline=60)
    manifest = load_manifest(bundle)
    assert manifest["inventory"]["file_count"] == len(files)
    assert test_provider.attempts[weight] == 3
    ledger = load_ledger(bundle)
    session = ledger["sessions"][-1]
    assert session["end_reason"] == "complete"
    assert session["reconnects"] == 1
    events = {(e["event"], e["path"]) for e in ledger["events"]}
    assert ("network_lost", weight) in events
    assert ("network_restored", weight) in events
    restored = next(e for e in ledger["events"] if e["event"] == "network_restored")
    assert "2 reconnect attempts" in restored["detail"]


def test_zero_offline_patience_pauses_cleanly(vault, test_provider):
    files = model_files()
    test_provider.add_repo("acme/toy", files)
    weight = "model.safetensors"
    test_provider.fail_next(weight, ConnectionResetError("reset"))
    with pytest.raises(PartialTransfer) as stopped:
        archive_quiet("test:acme/toy", vault=vault, max_offline=0)
    assert stopped.value.reason == "offline"
    assert "connection reset" in stopped.value.detail
    bundle = stopped.value.bundle_dir
    assert not (bundle / "manifest.json").is_file()
    assert load_ledger(bundle)["sessions"][-1]["end_reason"] == "offline"
    # Network back: the same command converges.
    bundle = archive_quiet("test:acme/toy", vault=vault, max_offline=0)
    assert load_manifest(bundle)["inventory"]["file_count"] == len(files)


def test_non_network_errors_still_propagate(vault, test_provider, monkeypatch):
    monkeypatch.setattr("darsay.transfer.RECONNECT_WAITS_S", (0.01,))
    test_provider.add_repo("acme/toy", model_files())
    test_provider.fail_next("model.safetensors", ValueError("file too large"))
    with pytest.raises(ValueError, match="file too large"):
        archive_quiet("test:acme/toy", vault=vault, max_offline=60)
    bundle = next((vault / "test--acme--toy").iterdir())
    assert load_ledger(bundle)["sessions"][-1]["end_reason"] == "error"


def test_rate_cap_paces_the_transfer(vault, test_provider, monkeypatch):
    slept: list[float] = []
    clock = {"t": 0.0}

    def sleep(seconds):
        slept.append(seconds)
        clock["t"] += seconds

    monkeypatch.setattr("darsay.transfer.time.sleep", sleep)
    monkeypatch.setattr("darsay.transfer.time.monotonic", lambda: clock["t"])
    files = model_files()
    test_provider.add_repo("acme/toy", files)
    total = sum(len(data) for data in files.values())
    # A cap far below the payload size forces at least one paced sleep.
    bundle = archive_quiet("test:acme/toy", vault=vault, max_rate=max(1, total // 10))
    assert load_manifest(bundle)["inventory"]["file_count"] == len(files)
    assert slept
    assert all(0 < s <= 0.2 for s in slept)
    slept.clear()
    # Unlimited: no pacing at all.
    archive_quiet("test:acme/toy", vault=vault, max_rate=0, force=True)
    assert slept == []


def test_disk_full_mid_transfer_pauses_cleanly_and_resumes(vault, test_provider):
    import errno

    files = model_files()
    test_provider.add_repo("acme/toy", files)
    weight = "model.safetensors"
    # The floor is off (0) in tests: ENOSPC is the only guard left, and it
    # must still be a pause, not an error.
    test_provider.fail_next(weight, OSError(errno.ENOSPC, "No space left on device"))
    with pytest.raises(PartialTransfer) as stopped:
        archive_quiet("test:acme/toy", vault=vault)
    assert stopped.value.reason == "disk"
    assert f"no space left on device while writing {weight}" in stopped.value.detail
    bundle = stopped.value.bundle_dir
    assert not (bundle / "manifest.json").is_file()
    session = load_ledger(bundle)["sessions"][-1]
    assert session["end_reason"] == "disk"
    assert session["tool"].startswith("darsay ")
    # Space freed: the same command converges.
    bundle = archive_quiet("test:acme/toy", vault=vault)
    assert load_manifest(bundle)["inventory"]["file_count"] == len(files)


def test_headroom_check_pauses_before_a_file_that_cannot_fit(
    vault, test_provider, monkeypatch
):
    from types import SimpleNamespace

    files = model_files()
    test_provider.add_repo("acme/toy", files)
    floor = 2 * 1024**3
    largest = max(files, key=lambda name: len(files[name]))
    # Every file but the largest fits above the floor.
    disk = SimpleNamespace(free=floor + len(files[largest]) - 1)
    monkeypatch.setattr("darsay.transfer.shutil.disk_usage", lambda path: disk)
    with pytest.raises(PartialTransfer) as stopped:
        archive_quiet("test:acme/toy", vault=vault, min_free=floor)
    assert stopped.value.reason == "disk"
    assert stopped.value.detail.startswith(
        f"{largest} needs {len(files[largest])} B more"
    )
    # The file that could not fit was never begun; the others landed.
    assert test_provider.attempts.get(largest, 0) == 0
    ledger = load_ledger(stopped.value.bundle_dir)
    verified = {
        path for path, state in ledger["files"].items() if state["status"] == "verified"
    }
    assert verified == set(files) - {largest}
    disk.free = 100 * 1024**3
    bundle = archive_quiet("test:acme/toy", vault=vault, min_free=floor)
    assert load_manifest(bundle)["inventory"]["file_count"] == len(files)


def test_preflight_confirm_declined_pauses_before_any_byte(
    vault, test_provider, monkeypatch
):
    from types import SimpleNamespace

    files = model_files()
    test_provider.add_repo("acme/toy", files)
    floor = 2 * 1024**3
    disk = SimpleNamespace(free=floor + 1)
    monkeypatch.setattr("darsay.transfer.shutil.disk_usage", lambda path: disk)
    questions: list[str] = []
    logs: list[str] = []

    def decline(question: str) -> bool:
        questions.append(question)
        return False

    with pytest.raises(PartialTransfer) as stopped:
        archive_quiet(
            "test:acme/toy",
            vault=vault,
            min_free=floor,
            confirm=decline,
            progress=logs.append,
        )
    assert questions == ["Continue anyway? [Y/n] "]
    assert stopped.value.reason == "disk"
    assert stopped.value.detail.startswith("declined at the disk preflight")
    assert test_provider.downloads == []
    ledger = load_ledger(stopped.value.bundle_dir)
    assert ledger["sessions"][-1]["end_reason"] == "disk"
    text = "\n".join(logs)
    assert "WARNING: disk preflight is insufficient" in text
    assert "  after about 1 B more (0 of" in text
    assert "then re-run to continue" in text

    # Accepting (and, here, freeing space) lets the same command finish.
    def accept(question: str) -> bool:
        disk.free = 100 * 1024**3
        return True

    bundle = archive_quiet("test:acme/toy", vault=vault, min_free=floor, confirm=accept)
    assert load_manifest(bundle)["inventory"]["file_count"] == len(files)
    # Without a confirm callback (cron, pipes) the run simply proceeds.
    disk.free = floor + 1
    test_provider.add_repo("acme/other", files)
    with pytest.raises(PartialTransfer) as unattended:
        archive_quiet("test:acme/other", vault=vault, min_free=floor)
    assert "declined" not in unattended.value.detail

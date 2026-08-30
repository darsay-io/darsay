from __future__ import annotations

import json
import shutil
from pathlib import Path

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
    already = [
        path
        for path, state in load_ledger(dest)["files"].items()
        if state.get("status") == "verified"
    ]
    test_provider.downloads.clear()
    bundle = archive_quiet("test:acme/toy", vault=other_vault)
    assert (bundle / "manifest.json").is_file()
    assert not set(already) & set(test_provider.downloads)


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


def _archive_half(source, *, vault):
    """Fetch as much as a one-byte budget allows; return the partial dir."""
    try:
        archive_quiet(source, vault=vault, max_bytes=1)
    except PartialTransfer as stop:
        return stop.bundle_dir
    found = list(vault.glob("*/*"))
    assert found
    return found[0]


def test_assemble_move_skeletonizes_source_and_never_refetches(
    vault, test_provider, tmp_path
):
    """Archive one pin across two disks that never mount together.

    Laptop fetches a half, hands it to the big vault with ``--move`` (keeping
    a skeleton), fetches the rest, hands that over too, and the big vault
    registers with zero payload network bytes — the moved half is never
    re-downloaded.
    """
    files = model_files(param_shape=[64, 64])
    test_provider.add_repo("acme/big", files)
    laptop = tmp_path / "laptop"
    laptop.mkdir()
    big = vault

    skeleton = _archive_half("test:acme/big", vault=laptop)

    # First hand-over: the verified half moves out; the skeleton remains.
    dest, _plan = assemble_partials([skeleton], big, progress=silent, move=True)
    src_ledger = load_ledger(skeleton)
    moved = {p for p, s in src_ledger["files"].items() if s.get("status") == "moved"}
    assert moved, "expected at least one file to move out"
    for path in moved:
        state = src_ledger["files"][path]
        assert "moved_at" in state
        assert not (skeleton / "model" / path).exists(), "moved bytes still on disk"
    assert any(e["event"] == "moved_out" for e in src_ledger["events"])
    dest_ledger = load_ledger(dest)
    assert any(e["event"] == "moved_in" for e in dest_ledger["events"])

    # Fetch the rest on the laptop: a moved file is never requested again.
    test_provider.downloads.clear()
    with pytest.raises(PartialTransfer) as paused:
        archive_quiet("test:acme/big", vault=laptop)
    assert paused.value.reason == "moved"
    assert not (set(test_provider.downloads) & moved)

    # Second hand-over drains the skeleton: it holds no payload byte, so it
    # dissolves — exactly what a plain mv would have left.
    assemble_partials([skeleton], big, progress=silent, move=True)
    assert not skeleton.exists()

    # The big vault now has every file verified; archive registers with no
    # payload network bytes.
    test_provider.downloads.clear()
    bundle = archive_quiet("test:acme/big", vault=big)
    assert bundle is not None
    assert (bundle / "manifest.json").is_file()
    assert test_provider.downloads == []
    assert load_manifest(bundle)["inventory"]["file_count"] == len(files)


def test_archive_on_a_fully_moved_skeleton_pauses_moved(vault, test_provider, tmp_path):
    """A skeleton with every file moved out has nothing to fetch: it pauses
    cleanly with reason 'moved' (assemble to register), never errors."""
    from darsay.sources import parse_source
    from darsay.transfer import new_ledger, save_ledger

    files = model_files()
    test_provider.add_repo("acme/toy", files)
    snap = test_provider.pin(parse_source("test:acme/toy"), None)

    # Every expected file is verified-elsewhere (moved), none present here.
    skel = tmp_path / "vault" / "test--acme--toy" / snap.revision[:12]
    (skel / "model").mkdir(parents=True)
    ledger = new_ledger(snap)
    for item in ledger["expected"]:
        ledger["files"][item["path"]] = {
            "status": "moved",
            "size": item["size"],
            "sha256": "x",
            "moved_at": "2026-08-30T00:00:00+00:00",
        }
    save_ledger(skel, ledger)

    with pytest.raises(PartialTransfer) as paused:
        archive_quiet("test:acme/toy", vault=(tmp_path / "vault"))
    assert paused.value.reason == "moved"
    # Nothing was fetched; the skeleton is untouched.
    assert test_provider.downloads == []
    assert load_ledger(skel)["sessions"][-1]["end_reason"] == "moved"


def test_assemble_move_keeps_skeleton_when_a_copy_fails_to_verify(
    vault, test_provider, tmp_path, monkeypatch
):
    """A copy that corrupts in transit must not cost the source its only
    copy: the destination discards it, the file stays verified at the
    source, and the skeleton is kept even with nothing left to fetch."""
    import darsay.transfer as transfer_mod

    files = model_files(param_shape=[64, 64])
    test_provider.add_repo("acme/big", files)
    laptop = tmp_path / "laptop"
    laptop.mkdir()
    big = vault

    # First half over, then fetch the rest: the skeleton now holds only
    # verified files (the second half) beside its moved records.
    skeleton = _archive_half("test:acme/big", vault=laptop)
    assemble_partials([skeleton], big, progress=silent, move=True)
    with pytest.raises(PartialTransfer) as paused:
        archive_quiet("test:acme/big", vault=laptop)
    assert paused.value.reason == "moved"

    src_ledger = load_ledger(skeleton)
    victim = next(
        path
        for path, state in sorted(src_ledger["files"].items())
        if state.get("status") == "verified"
    )
    real_copy = transfer_mod._copy_local_file

    def corrupting_copy(source, destination):
        method = real_copy(source, destination)
        if str(destination).endswith(victim):
            destination.write_bytes(b"corrupted-in-transit")
        return method

    monkeypatch.setattr(transfer_mod, "_copy_local_file", corrupting_copy)
    dest, _plan = assemble_partials([skeleton], big, progress=silent, move=True)

    # The skeleton survives and still holds the one good copy.
    assert skeleton.exists()
    src_ledger = load_ledger(skeleton)
    assert src_ledger["files"][victim]["status"] == "verified"
    assert (skeleton / "model" / victim).is_file()
    # Everything else moved; the destination never verified the bad copy.
    others = set(src_ledger["files"]) - {victim}
    assert all(src_ledger["files"][path]["status"] == "moved" for path in others)
    assert load_ledger(dest)["files"].get(victim, {}).get("status") != "verified"

    # A clean re-run hands the file over, drains the skeleton, and the big
    # vault registers without a single payload byte from the network.
    monkeypatch.undo()
    assemble_partials([skeleton], big, progress=silent, move=True)
    assert not skeleton.exists()
    test_provider.downloads.clear()
    bundle = archive_quiet("test:acme/big", vault=big)
    assert (bundle / "manifest.json").is_file()
    assert test_provider.downloads == []


def test_assemble_move_via_cli_reports_the_skeleton(vault, test_provider, tmp_path):
    from darsay.cli import main

    files = model_files(param_shape=[64, 64])
    test_provider.add_repo("acme/big", files)
    laptop = tmp_path / "laptop"
    laptop.mkdir()
    skeleton = _archive_half("test:acme/big", vault=laptop)

    rc = main(["--vault", str(vault), "assemble", str(skeleton), "--move"])
    assert rc == 0
    # The moved half is gone from the skeleton; its ledger records the move.
    src_ledger = load_ledger(skeleton)
    assert any(s.get("status") == "moved" for s in src_ledger["files"].values())


def _plant_bundle_in_vault(source, dest_vault):
    """``rsync`` / ``cp -a`` a bundle into another vault at the same slug/rev."""
    dest = dest_vault / source.parent.name / source.name
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, dest)
    return dest


def test_assemble_move_after_rsync_copies_nothing_and_skeletonizes_source(
    vault, test_provider, tmp_path, monkeypatch
):
    """An out-of-band copy into the dest vault is the same as assemble.

    ``rsync srcvault/<slug>/<rev>/ dstvault/<slug>/<rev>/`` then
    ``darsay --vault dstvault assemble src --move`` must not recopy payload
    bytes the destination already holds. It re-hashes them against the pin,
    deletes the source files, and keeps the source ledger as a skeleton.
    A second ``--move`` is a no-op for those files.
    """
    import darsay.transfer as transfer_mod
    from darsay.cli import main

    files = model_files(param_shape=[64, 64])
    test_provider.add_repo("acme/big", files)
    laptop = tmp_path / "laptop"
    laptop.mkdir()
    source = _archive_half("test:acme/big", vault=laptop)
    dest = _plant_bundle_in_vault(source, vault)

    verified_before = {
        path
        for path, state in load_ledger(source)["files"].items()
        if state.get("status") == "verified"
    }
    assert verified_before, "expected the partial to hold at least one file"
    dest_bytes_before = {
        path: (dest / "model" / path).read_bytes() for path in verified_before
    }

    copied_payload = []
    real_copy = transfer_mod._copy_local_file

    def tracking_copy(src, dst):
        dest_path = Path(dst)
        if ".cache" not in dest_path.parts:
            copied_payload.append(dest_path)
        return real_copy(src, dst)

    monkeypatch.setattr(transfer_mod, "_copy_local_file", tracking_copy)

    rc = main(["--vault", str(vault), "assemble", str(source), "--move"])
    assert rc == 0
    assert copied_payload == [], "destination already held the payload; recopy is waste"

    src_ledger = load_ledger(source)
    assert (source / "transfer.json").is_file(), "source metadata must stay"
    for path in verified_before:
        assert src_ledger["files"][path]["status"] == "moved"
        assert "moved_at" in src_ledger["files"][path]
        assert not (source / "model" / path).exists(), "source payload should be gone"
        assert (dest / "model" / path).read_bytes() == dest_bytes_before[path]

    dest_ledger = load_ledger(dest)
    for path in verified_before:
        assert dest_ledger["files"][path]["status"] == "verified"

    # Same command again: dest still holds the bytes, source already moved.
    copied_payload.clear()
    rc = main(["--vault", str(vault), "assemble", str(source), "--move"])
    assert rc == 0
    assert copied_payload == []
    src_ledger = load_ledger(source)
    for path in verified_before:
        assert src_ledger["files"][path]["status"] == "moved"
        assert not (source / "model" / path).exists()
        assert (dest / "model" / path).read_bytes() == dest_bytes_before[path]


def test_assemble_after_rsync_does_not_rehash_verified_dest(
    vault, test_provider, tmp_path, monkeypatch
):
    """After rsync, dest ledger + size is enough: do not read dest to re-hash."""
    import darsay.hashing as hashing_mod

    files = model_files(param_shape=[64, 64])
    test_provider.add_repo("acme/big", files)
    laptop = tmp_path / "laptop"
    laptop.mkdir()
    source = _archive_half("test:acme/big", vault=laptop)
    dest = _plant_bundle_in_vault(source, vault)
    verified = {
        path
        for path, state in load_ledger(source)["files"].items()
        if state.get("status") == "verified"
    }
    hashed_paths = []
    real_hash = hashing_mod.hash_file

    def tracking_hash(path, *args, **kwargs):
        hashed_paths.append(Path(path))
        return real_hash(path, *args, **kwargs)

    monkeypatch.setattr(hashing_mod, "hash_file", tracking_hash)
    monkeypatch.setattr("darsay.transfer.hash_file", tracking_hash)

    logs: list[str] = []
    assemble_partials([source], vault, progress=logs.append, move=True)
    dest_payload = dest / "model"
    dest_hashed = [
        p for p in hashed_paths if dest_payload in p.parents or p.parent == dest_payload
    ]
    assert dest_hashed == [], "rsync'd verified dest files must not be re-hashed"
    text = "\n".join(str(item) for item in logs)
    assert "not re-hashing dest" in text
    assert "Releasing source payload files" in text
    src_ledger = load_ledger(source)
    for path in verified:
        assert src_ledger["files"][path]["status"] == "moved"
        assert not (source / "model" / path).exists()
        assert (dest / "model" / path).is_file()


def test_assemble_rehash_after_rsync_hashes_dest(
    vault, test_provider, tmp_path, monkeypatch
):
    """``--rehash`` is the dest-local integrity pass; it does read dest."""
    import darsay.hashing as hashing_mod

    files = model_files(param_shape=[64, 64])
    test_provider.add_repo("acme/big", files)
    laptop = tmp_path / "laptop"
    laptop.mkdir()
    source = _archive_half("test:acme/big", vault=laptop)
    dest = _plant_bundle_in_vault(source, vault)
    hashed_paths = []
    real_hash = hashing_mod.hash_file

    def tracking_hash(path, *args, **kwargs):
        hashed_paths.append(Path(path))
        return real_hash(path, *args, **kwargs)

    monkeypatch.setattr(hashing_mod, "hash_file", tracking_hash)
    monkeypatch.setattr("darsay.transfer.hash_file", tracking_hash)

    assemble_partials([source], vault, progress=silent, move=True, rehash=True)
    dest_payload = dest / "model"
    dest_hashed = [
        p for p in hashed_paths if dest_payload in p.parents or p.parent == dest_payload
    ]
    assert dest_hashed, "--rehash must hash dest files"


def test_assemble_rehash_warns_on_network_filesystem(
    vault, test_provider, tmp_path, monkeypatch
):
    from darsay.transfer import _warn_if_network_rehash

    monkeypatch.setattr("darsay.transfer.filesystem_type", lambda path: "smbfs")
    logs: list[str] = []
    _warn_if_network_rehash(vault, logs.append)
    text = "\n".join(logs)
    assert "smbfs" in text
    assert "--rehash will read every dest byte over the network" in text
    logs.clear()
    monkeypatch.setattr("darsay.transfer.filesystem_type", lambda path: "apfs")
    _warn_if_network_rehash(vault, logs.append)
    assert logs == []


def test_assemble_move_into_registered_dest_does_not_mutate_payload(
    vault, test_provider, tmp_path
):
    """rsync a half, dest finishes and registers, then --move skeletonizes source."""
    files = model_files(param_shape=[64, 64])
    test_provider.add_repo("acme/big", files)
    laptop = tmp_path / "laptop"
    laptop.mkdir()
    source = _archive_half("test:acme/big", vault=laptop)
    dest = _plant_bundle_in_vault(source, vault)
    verified = {
        path
        for path, state in load_ledger(source)["files"].items()
        if state.get("status") == "verified"
    }
    dest_bytes = {path: (dest / "model" / path).read_bytes() for path in verified}

    test_provider.downloads.clear()
    registered = archive_quiet("test:acme/big", vault=vault)
    assert (registered / "manifest.json").is_file()
    downloads_after_register = list(test_provider.downloads)

    dest, _plan = assemble_partials([source], vault, progress=silent, move=True)
    assert (dest / "manifest.json").is_file()
    for path in verified:
        assert (dest / "model" / path).read_bytes() == dest_bytes[path]
        assert load_ledger(source)["files"][path]["status"] == "moved"
        assert not (source / "model" / path).exists()
    assert test_provider.downloads == downloads_after_register


def test_assemble_move_refuses_registered_source(vault, test_provider, tmp_path):
    files = model_files()
    test_provider.add_repo("acme/toy", files)
    source = archive_quiet("test:acme/toy", vault=vault)
    other = tmp_path / "other"
    other.mkdir()
    with pytest.raises(SystemExit, match="cannot --move a registered bundle"):
        assemble_partials([source], other, progress=silent, move=True)
    assert (source / "manifest.json").is_file()
    assert (source / "model" / "config.json").is_file() or any(
        (source / "model").rglob("*")
    )

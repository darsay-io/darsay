from __future__ import annotations

import pytest

from darsay.providers.base import FileSpec, Snapshot, SourceRef
from darsay.transfer import (
    DISK_PROBE_INTERVAL_S,
    TRANSFER_VERSION,
    CleanStop,
    LedgerError,
    NetworkCounter,
    StopController,
    _digest_matches,
    _lock_was_copied,
    _payload_path,
    _same_transfer_set,
    add_disk_preflight,
    disk_verdict,
    load_ledger,
    new_ledger,
    save_ledger,
    transfer_groups,
    transfer_plan,
    transfer_summary,
)


def _source() -> SourceRef:
    return SourceRef(
        provider="test",
        artifact_type="model",
        locator="acme/toy",
        canonical="test:acme/toy",
        url="https://test.invalid/acme/toy",
        bundle_name="test--acme--toy",
        publisher="acme",
        name="toy",
    )


def test_stop_controller_byte_budget():
    ctrl = StopController(max_bytes=100)
    ctrl.start()
    ctrl.check({"bytes_network": 50})
    with pytest.raises(CleanStop) as exc:
        ctrl.check({"bytes_network": 100})
    assert exc.value.reason == "budget"


def test_stop_controller_time_budget(monkeypatch):
    clock = {"t": 0.0}
    monkeypatch.setattr("darsay.transfer.time.monotonic", lambda: clock["t"])
    ctrl = StopController(max_minutes=1)
    ctrl.start()
    ctrl.check({"bytes_network": 0})
    clock["t"] = 61.0
    with pytest.raises(CleanStop) as exc:
        ctrl.check({"bytes_network": 0})
    assert exc.value.reason == "budget"
    assert "time" in exc.value.detail


def test_stop_controller_interrupt():
    ctrl = StopController()
    ctrl.interrupted = True
    with pytest.raises(CleanStop) as exc:
        ctrl.check({"bytes_network": 0})
    assert exc.value.reason == "interrupt"


def test_stop_controller_disk_floor_throttles_and_sticks(monkeypatch, tmp_path):
    from types import SimpleNamespace

    clock = {"t": 0.0}
    monkeypatch.setattr("darsay.transfer.time.monotonic", lambda: clock["t"])
    disk = {"free": 10 * 1024**3}
    probes: list = []

    def fake_usage(path):
        probes.append(path)
        return SimpleNamespace(free=disk["free"])

    monkeypatch.setattr("darsay.transfer.shutil.disk_usage", fake_usage)
    ctrl = StopController(min_free_bytes=2 * 1024**3, disk_path=tmp_path)
    ctrl.start()
    ctrl.check({"bytes_network": 0})
    # A chunk-rate storm of checks probes the disk once per interval.
    ctrl.check({"bytes_network": 0})
    assert probes == [tmp_path]
    clock["t"] = DISK_PROBE_INTERVAL_S + 0.1
    ctrl.check({"bytes_network": 0})
    assert len(probes) == 2

    disk["free"] = 1 * 1024**3
    clock["t"] += DISK_PROBE_INTERVAL_S + 0.1
    with pytest.raises(CleanStop) as stop:
        ctrl.check({"bytes_network": 0})
    assert stop.value.reason == "disk"
    assert "floor" in stop.value.detail
    # Sticky: every later check stops too, without re-probing.
    clock["t"] += 100.0
    with pytest.raises(CleanStop) as again:
        ctrl.check({"bytes_network": 0})
    assert again.value.reason == "disk"
    assert len(probes) == 3


def test_stop_controller_without_floor_never_probes(monkeypatch, tmp_path):
    def forbidden(_path):
        raise AssertionError("disk probed without a floor")

    monkeypatch.setattr("darsay.transfer.shutil.disk_usage", forbidden)
    StopController(disk_path=tmp_path).check({"bytes_network": 0})
    StopController(min_free_bytes=1024).check({"bytes_network": 0})


def test_stop_controller_disk_probe_errors_are_ignored(monkeypatch, tmp_path):
    def broken(_path):
        raise OSError("statvfs failed")

    monkeypatch.setattr("darsay.transfer.shutil.disk_usage", broken)
    ctrl = StopController(min_free_bytes=1024, disk_path=tmp_path)
    ctrl.check({"bytes_network": 0})


def test_disk_verdict_honors_floor():
    assert disk_verdict(100, 50) == "ok"
    assert disk_verdict(100, 95) == "tight"
    assert disk_verdict(100, 101) == "insufficient"
    assert disk_verdict(100, 50, min_free=48) == "tight"
    assert disk_verdict(100, 50, min_free=60) == "insufficient"
    assert disk_verdict(100, 50, min_free=None) == "ok"


def test_add_disk_preflight_records_floor(monkeypatch, tmp_path):
    from types import SimpleNamespace

    monkeypatch.setattr(
        "darsay.transfer.shutil.disk_usage",
        lambda path: SimpleNamespace(free=100),
    )
    plan = {"bytes": {"remaining_network": 50}}
    add_disk_preflight(tmp_path, plan, min_free=60)
    assert plan["disk"]["min_free_bytes"] == 60
    assert plan["disk"]["verdict"] == "insufficient"
    plan = {"bytes": {"remaining_network": 50}}
    add_disk_preflight(tmp_path, plan)
    assert plan["disk"]["min_free_bytes"] is None
    assert plan["disk"]["verdict"] == "ok"


def test_network_counter_defers_first_overage():
    session = {"bytes_network": 0}
    ctrl = StopController(max_bytes=10)
    counter = NetworkCounter(session, ctrl)
    counter.add(15)
    assert session["bytes_network"] == 15
    assert counter.pending_stop is not None
    with pytest.raises(CleanStop):
        counter.add(1)


def test_network_counter_poll_raises_only_banked_stop():
    session = {"bytes_network": 0}
    ctrl = StopController()
    counter = NetworkCounter(session, ctrl)
    counter.poll()  # nothing banked, nothing raised
    ctrl.interrupted = True
    counter.add(10)  # banks the stop; that chunk's bytes are durable
    with pytest.raises(CleanStop) as exc:
        counter.poll()
    assert exc.value.reason == "interrupt"


def test_sigint_escalates_across_presses(monkeypatch):
    import os
    import signal
    import threading

    if threading.current_thread() is not threading.main_thread():
        pytest.skip("signal delivery needs the main thread")

    exits: list[int] = []
    monkeypatch.setattr(os, "_exit", lambda code: exits.append(code))
    ctrl = StopController()
    with ctrl.sigint_handler():
        signal.raise_signal(signal.SIGINT)
        assert ctrl.interrupted is True
        assert ctrl.sigints == 1
        with pytest.raises(KeyboardInterrupt):
            signal.raise_signal(signal.SIGINT)
        signal.raise_signal(signal.SIGINT)
    assert exits == [130]


def test_payload_path_rejects_unsafe():
    root = __import__("pathlib").Path("/tmp/payload")
    with pytest.raises(SystemExit, match="unsafe path"):
        _payload_path(root, "../etc/passwd")
    with pytest.raises(SystemExit, match="unsafe path"):
        _payload_path(root, "/abs")
    with pytest.raises(SystemExit, match="unsafe path"):
        _payload_path(root, "foo/../bar")
    safe = _payload_path(root, "dir/file.bin")
    assert safe == root / "dir" / "file.bin"


def test_digest_matches():
    assert _digest_matches({"lfs_sha256": "aa"}, {"sha256": "aa"}) is True
    assert _digest_matches({"lfs_sha256": "aa"}, {"sha256": "bb"}) is False
    assert _digest_matches({"git_sha1": "aa"}, {"git_sha1": "aa"}) is True
    assert _digest_matches({}, {"sha256": "aa"}) is None


def test_transfer_groups_byte_balanced_and_deterministic():
    files = [
        {"path": "a", "size": 100},
        {"path": "b", "size": 100},
        {"path": "c", "size": 50},
        {"path": "d", "size": 50},
    ]
    first = transfer_groups(files, (1, 2))
    second = transfer_groups(files, (2, 2))
    # Same grouping, rotated start lane.
    lanes_first = {lane: [i["path"] for i in items] for lane, items in first}
    lanes_second = {lane: [i["path"] for i in items] for lane, items in second}
    assert lanes_first == lanes_second
    assert first[0][0] == 0
    assert second[0][0] == 1
    # Re-run is stable.
    assert transfer_groups(files, (1, 2)) == first


def test_transfer_groups_unsharded_sorts_by_size():
    files = [{"path": "b", "size": 10}, {"path": "a", "size": 1}]
    [(lane, items)] = transfer_groups(files, None)
    assert lane is None
    assert [i["path"] for i in items] == ["a", "b"]


def test_lock_copied_uses_device_inode_not_path():
    ours = {"bundle": {"device": 1, "inode": 2, "path": "/a"}}
    copied = {"bundle": {"device": 1, "inode": 99, "path": "/a"}}
    alias = {"bundle": {"device": 1, "inode": 2, "path": "/other"}}
    assert _lock_was_copied(copied, ours) is True
    assert _lock_was_copied(alias, ours) is False
    assert _lock_was_copied({}, ours) is False


def test_same_transfer_set_treats_hf_alias_as_huggingface():
    left = {
        "transfer_version": 1,
        "repo_id": "acme/toy",
        "repo_type": "model",
        "revision": "aaa",
        "expected": [],
        "provider": "hf",
    }
    right = dict(left, provider="huggingface")
    assert _same_transfer_set(left, right) is True
    assert _same_transfer_set(left, dict(right, repo_id="other")) is False


def test_ledger_roundtrip_and_validation(tmp_path):
    snapshot = Snapshot(
        source=_source(),
        revision="a" * 40,
        revision_ref="main",
        files=[FileSpec(path="LICENSE", size=4, sha256="ab")],
        metadata={"card_data": {}},
    )
    ledger = new_ledger(snapshot)
    assert ledger["transfer_version"] == TRANSFER_VERSION
    assert ledger["address"] == "test:acme/toy"
    assert "files" in ledger and ledger["files"] == {}
    save_ledger(tmp_path, ledger)
    loaded = load_ledger(tmp_path)
    assert loaded["expected"][0]["path"] == "LICENSE"

    (tmp_path / "transfer.json").write_text("{}\n")
    with pytest.raises(LedgerError, match="unsupported"):
        load_ledger(tmp_path)


def test_transfer_plan_complete_and_remaining(tmp_path):
    ledger = {
        "provider": "huggingface",
        "expected": [
            {"path": "a", "size": 10},
            {"path": "b", "size": 20},
        ],
        "files": {"a": {"status": "verified"}},
    }
    plan = transfer_plan(tmp_path, ledger)
    assert plan["files"]["verified"] == 1
    assert plan["files"]["missing"] == 1
    assert plan["bytes"]["remaining_network"] == 20
    assert plan["complete"] is False
    ledger["files"]["b"] = {"status": "verified"}
    assert transfer_plan(tmp_path, ledger)["complete"] is True


def test_transfer_summary_aggregates_sessions():
    ledger = {
        "pinned_at": "t0",
        "sessions": [
            {
                "started": "t1",
                "ended": "t2",
                "bytes_network": 10,
                "bytes_adopted": 5,
                "bytes_local_sources": 1,
                "retries": 2,
            },
            {
                "started": "t3",
                "ended": "t4",
                "bytes_network": 3,
                "bytes_adopted": 0,
                "bytes_local_sources": 0,
                "retries": 1,
            },
        ],
    }
    summary = transfer_summary(ledger)
    assert summary["sessions"] == 2
    assert summary["bytes_network"] == 13
    assert summary["retries"] == 3

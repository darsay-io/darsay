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


class _Clock:
    def __init__(self, t: float = 0.0):
        self.t = t

    def __call__(self) -> float:
        return self.t


def test_throttle_accepts_chunks_and_bills_the_debt():
    from darsay.transfer import Throttle

    clock = _Clock(0.0)
    throttle = Throttle(1000, clock=clock)  # 1000 B/s, 1 s of credit
    assert throttle.debit(500) == 0.0
    # Credit is spent; the next 2500 bytes cost 2.5 s of sleep... minus the
    # 500 B still in the bucket: (500 - 2500) / 1000 = -2 s.
    assert throttle.debit(2500) == pytest.approx(2.0)
    clock.t = 2.0  # slept it off
    assert throttle.debit(0) == 0.0
    assert throttle.debit(1000) == pytest.approx(1.0)
    # A long pause refills at most one second of credit: no burst later.
    clock.t = 100.0
    assert throttle.debit(1000) == 0.0
    assert throttle.debit(1000) == pytest.approx(1.0)


def test_network_counter_paces_when_throttled(monkeypatch):
    from darsay.transfer import Throttle

    slept: list[float] = []
    clock = _Clock(0.0)

    def sleep(seconds):
        slept.append(seconds)
        clock.t += seconds

    monkeypatch.setattr("darsay.transfer.time.sleep", sleep)
    monkeypatch.setattr("darsay.transfer.time.monotonic", clock)
    session = {"bytes_network": 0}
    counter = NetworkCounter(session, throttle=Throttle(1000, clock=clock))
    counter.add(1000)
    assert slept == []
    counter.add(1000)  # 1 s over the cap -> paced in slices
    assert slept and sum(slept) == pytest.approx(1.0)
    assert session["bytes_network"] == 2000


def test_network_counter_pacing_yields_to_a_stop(monkeypatch):
    from darsay.transfer import Throttle

    slept: list[float] = []
    monkeypatch.setattr("darsay.transfer.time.sleep", lambda s: slept.append(s))
    ctrl = StopController()
    counter = NetworkCounter({"bytes_network": 0}, ctrl, throttle=Throttle(10))
    counter.add(10)
    ctrl.interrupted = True
    counter.add(10)  # banks the interrupt; must not sleep off a 1 s debt
    assert slept == []
    assert counter.pending_stop is not None


def test_link_tracks_outage_and_reports_bytes_as_recovery():
    from darsay.transfer import RECONNECT_WAITS_S, Link

    clock = _Clock(100.0)
    session = {"reconnects": 0}
    link = Link(3600, session, clock=clock)
    assert link.snapshot() is None
    retry_at = link.lost("a.bin", "DNS lookup failed", 0)
    assert retry_at == 100.0 + RECONNECT_WAITS_S[0]
    snap = link.snapshot()
    assert snap["state"] == "offline"
    assert snap["reason"] == "DNS lookup failed"
    assert snap["retry_in"] == pytest.approx(RECONNECT_WAITS_S[0])
    assert snap["attempts"] == 1
    # Bytes draining from an older stream do not end the outage ...
    link.online()
    assert link.offline
    # ... but a retry that receives bytes does.
    clock.t = 102.0
    link.retrying("a.bin")
    assert link.snapshot()["state"] == "reconnecting"
    assert link.snapshot()["retry_in"] is None
    clock.t = 103.0
    link.online()
    assert link.snapshot() is None
    assert session["reconnects"] == 1
    kinds = [kind for _serial, kind, _info in link.transitions]
    assert kinds == ["lost", "restored"]
    restored = link.transitions[-1][2]
    assert restored["seconds"] == pytest.approx(3.0)
    assert restored["attempts"] == 1
    # Later attempts back off along the schedule and cap at its tail.
    link.lost("a.bin", "connection reset", 1)
    link.lost("b.bin", "connection reset", 99)
    snap = link.snapshot()
    assert snap["attempts"] == 2
    assert snap["retry_in"] == pytest.approx(RECONNECT_WAITS_S[1])
    assert link.transitions_after(2)[0][1] == "lost"


def test_wait_for_link_sleeps_then_retries(monkeypatch):
    from darsay.transfer import Link, _wait_for_link

    clock = _Clock(0.0)
    monkeypatch.setattr("darsay.transfer.time.monotonic", clock)

    def sleep(seconds):
        clock.t += seconds

    monkeypatch.setattr("darsay.transfer.time.sleep", sleep)
    link = Link(60, {"reconnects": 0}, clock=clock)
    _wait_for_link(link, "a.bin", "timed out", 0, None, {"bytes_network": 0})
    assert clock.t >= 2.0
    assert link.snapshot()["state"] == "reconnecting"


def test_wait_for_link_gives_up_after_patience(monkeypatch):
    from darsay.transfer import Link, _wait_for_link

    clock = _Clock(0.0)
    monkeypatch.setattr("darsay.transfer.time.monotonic", clock)
    monkeypatch.setattr(
        "darsay.transfer.time.sleep", lambda s: setattr(clock, "t", clock.t + s)
    )
    link = Link(5, {"reconnects": 0}, clock=clock)
    session = {"bytes_network": 0}
    _wait_for_link(link, "a.bin", "connection reset", 0, None, session)
    with pytest.raises(CleanStop) as stop:
        _wait_for_link(link, "a.bin", "connection reset", 4, None, session)
    assert stop.value.reason == "offline"
    assert "network unreachable for" in stop.value.detail
    assert "connection reset" in stop.value.detail


def test_wait_for_link_zero_patience_pauses_at_once():
    from darsay.transfer import Link, _wait_for_link

    link = Link(0, {"reconnects": 0})
    with pytest.raises(CleanStop) as stop:
        _wait_for_link(link, "a.bin", "DNS lookup failed", 0, None, {})
    assert stop.value.reason == "offline"
    assert link.snapshot() is None


def test_wait_for_link_honors_interrupt(monkeypatch):
    from darsay.transfer import Link, _wait_for_link

    monkeypatch.setattr("darsay.transfer.time.sleep", lambda s: None)
    ctrl = StopController()
    ctrl.interrupted = True
    link = Link(60, {"reconnects": 0})
    with pytest.raises(CleanStop) as stop:
        _wait_for_link(link, "a.bin", "timed out", 0, ctrl, {"bytes_network": 0})
    assert stop.value.reason == "interrupt"


def test_print_plan_prices_the_rate_cap(tmp_path):
    from darsay.transfer import print_plan

    plan = {
        "files": {"verified": 0, "partial": 0, "missing": 1, "total": 1},
        "bytes": {
            "verified": 0,
            "partial": 0,
            "missing": 60 * 1024**2,
            "total": 60 * 1024**2,
            "remaining_network": 60 * 1024**2,
        },
        "disk": {
            "checked_path": str(tmp_path),
            "free_bytes": 10**12,
            "needed_bytes": 60 * 1024**2,
            "min_free_bytes": None,
            "verdict": "ok",
        },
    }
    lines: list[str] = []
    print_plan(plan, progress=lines.append, max_rate=1024**2)
    rate = [line for line in lines if line.strip().startswith("rate:")]
    assert rate == [
        "  rate:     capped at 1.0 MiB/s — about 1 min for the remaining 60.0 MiB"
    ]
    lines.clear()
    print_plan(plan, progress=lines.append)
    assert not any("rate:" in line for line in lines)

from __future__ import annotations

import pytest

from darsay.providers.base import (
    FileSpec,
    Snapshot,
    SourceRef,
    describe_network_error,
)
from darsay.transfer import (
    DISK_PROBE_INTERVAL_S,
    PACE_SLICE_S,
    RECONNECT_WAITS_S,
    TRANSFER_VERSION,
    CleanStop,
    LedgerError,
    Link,
    NetworkCounter,
    StopController,
    Throttle,
    _digest_matches,
    _fetch_with_reconnect,
    _lock_was_copied,
    _payload_path,
    _same_transfer_set,
    _wait_for_link,
    add_disk_preflight,
    disk_verdict,
    load_ledger,
    new_ledger,
    reconcile,
    release_moved,
    save_ledger,
    transfer_groups,
    transfer_plan,
    transfer_summary,
)
from tests.conftest import silent
from tests.fakes import Clock


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


def test_transfer_plan_moved_bucket_is_fetched_not_complete(tmp_path):
    """A moved file is done for the pin but not registerable here."""
    ledger = {
        "provider": "huggingface",
        "expected": [
            {"path": "a", "size": 10},
            {"path": "b", "size": 20},
        ],
        "files": {
            "a": {"status": "verified"},
            "b": {"status": "moved"},
        },
    }
    plan = transfer_plan(tmp_path, ledger)
    assert plan["files"]["moved"] == 1
    assert plan["bytes"]["moved"] == 20
    # Moved bytes owe no network and do not make the bundle registerable.
    assert plan["bytes"]["remaining_network"] == 0
    assert plan["fetched"] is True
    assert plan["complete"] is False


def test_print_plan_shows_a_moved_line_only_when_present(tmp_path):
    from darsay.transfer import print_plan

    def plan_with(moved_files, moved_bytes):
        return {
            "files": {
                "verified": 1,
                "moved": moved_files,
                "partial": 0,
                "missing": 0,
                "total": 1 + moved_files,
            },
            "bytes": {
                "verified": 10,
                "moved": moved_bytes,
                "partial": 0,
                "missing": 0,
                "total": 10 + moved_bytes,
                "remaining_network": 0,
            },
            "disk": {
                "checked_path": str(tmp_path),
                "free_bytes": 10**12,
                "needed_bytes": 0,
                "min_free_bytes": None,
                "verdict": "ok",
            },
        }

    lines: list[str] = []
    print_plan(plan_with(1, 5 * 1024**2), progress=lines.append)
    assert any("moved:" in line and "another vault" in line for line in lines)
    lines.clear()
    print_plan(plan_with(0, 0), progress=lines.append)
    assert not any("moved:" in line for line in lines)


def test_reconcile_trusts_an_absent_moved_file(tmp_path):
    """A moved record whose bytes are absent is kept, never demoted."""
    payload = tmp_path / "model"
    payload.mkdir()
    ledger = {
        "provider": "huggingface",
        "expected": [{"path": "w.bin", "size": 4, "lfs_sha256": "deadbeef"}],
        "files": {
            "w.bin": {
                "status": "moved",
                "size": 4,
                "sha256": "abc",
                "moved_at": "2026-08-30T00:00:00+00:00",
            }
        },
        "events": [],
    }
    session = {"files_completed": 0, "bytes_adopted": 0}
    plan = reconcile(tmp_path, payload, ledger, session, progress=silent)
    assert ledger["files"]["w.bin"]["status"] == "moved"
    assert plan["files"]["moved"] == 1
    assert plan["fetched"] is True
    # No spurious demotion event.
    assert not any(e["event"] == "verified_file_missing" for e in ledger["events"])


def test_reconcile_adopts_a_moved_file_that_reappears(tmp_path):
    """If moved bytes come back, they are re-hashed and promoted to verified."""
    import hashlib

    payload = tmp_path / "model"
    payload.mkdir()
    data = b"come-back"
    (payload / "w.bin").write_bytes(data)
    ledger = {
        "provider": "huggingface",
        "expected": [
            {
                "path": "w.bin",
                "size": len(data),
                "lfs_sha256": hashlib.sha256(data).hexdigest(),
            }
        ],
        "files": {
            "w.bin": {
                "status": "moved",
                "size": len(data),
                "moved_at": "2026-08-30T00:00:00+00:00",
            }
        },
        "events": [],
    }
    session = {"files_completed": 0, "bytes_adopted": 0}
    plan = reconcile(tmp_path, payload, ledger, session, progress=silent)
    assert ledger["files"]["w.bin"]["status"] == "verified"
    assert "moved_at" not in ledger["files"]["w.bin"]
    assert plan["complete"] is True


def test_reconcile_keeps_the_moved_record_when_returned_bytes_are_wrong(tmp_path):
    """Bad bytes at a moved path are removed, but the move is not unsaid:
    the verified copy lives in another vault, so nothing re-fetches it."""
    payload = tmp_path / "model"
    payload.mkdir()
    bad_digest = b"thirteen-byte"  # right size, wrong digest
    (payload / "w.bin").write_bytes(bad_digest)
    (payload / "v.bin").write_bytes(b"way-too-long")  # wrong size outright
    moved = {
        "status": "moved",
        "size": len(bad_digest),
        "sha256": "0" * 64,
        "moved_at": "2026-08-30T00:00:00+00:00",
    }
    ledger = {
        "provider": "huggingface",
        "expected": [
            {"path": "w.bin", "size": len(bad_digest), "lfs_sha256": "0" * 64},
            {"path": "v.bin", "size": 4, "lfs_sha256": "1" * 64},
        ],
        "files": {"w.bin": dict(moved), "v.bin": {**moved, "size": 4}},
        "events": [],
    }
    session = {"files_completed": 0, "bytes_adopted": 0}
    plan = reconcile(tmp_path, payload, ledger, session, progress=silent)
    for name in ("w.bin", "v.bin"):
        assert ledger["files"][name]["status"] == "moved"
        assert not (payload / name).exists(), "failing bytes were not removed"
    assert plan["files"]["moved"] == 2
    assert plan["fetched"] is True
    events = {e["event"] for e in ledger["events"]}
    assert events == {"digest_mismatch", "size_mismatch"}


def test_release_moved_only_releases_destination_verified_bytes(tmp_path):
    """Verify-then-delete: a file the destination did not verify stays put."""
    source_dir = tmp_path / "src"
    payload = source_dir / "model"
    payload.mkdir(parents=True)
    for name in ("a.bin", "b.bin"):
        (payload / name).write_bytes(name.encode())
    source_ledger = {
        "expected": [{"path": "a.bin", "size": 5}, {"path": "b.bin", "size": 5}],
        "files": {
            "a.bin": {"status": "verified", "size": 5, "sha256": "x"},
            "b.bin": {"status": "verified", "size": 5, "sha256": "y"},
        },
        "events": [],
        "sessions": [],
    }
    # Destination verified only a.bin; b.bin's copy is not trusted there.
    destination_ledger = {"files": {"a.bin": {"status": "verified"}}}
    moved_files, moved_bytes = release_moved(
        source_dir, source_ledger, destination_ledger, "model"
    )
    assert moved_files == 1
    assert moved_bytes == 5
    assert source_ledger["files"]["a.bin"]["status"] == "moved"
    assert "moved_at" in source_ledger["files"]["a.bin"]
    assert not (payload / "a.bin").exists()
    # b.bin was never verified at the destination, so its only copy survives.
    assert source_ledger["files"]["b.bin"]["status"] == "verified"
    assert (payload / "b.bin").read_bytes() == b"b.bin"


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


def _advance(clock: Clock):
    """A ``time.sleep`` stand-in that moves the injected clock instead."""
    return lambda seconds: setattr(clock, "t", clock.t + seconds)


def test_throttle_accepts_chunks_and_bills_the_debt():
    clock = Clock(0.0)
    throttle = Throttle(1000, clock=clock)  # 1000 B/s
    # Every chunk is accepted; the caller sleeps off exactly what it added.
    assert throttle.debit(500) == pytest.approx(0.5)
    assert throttle.debit(2500) == pytest.approx(3.0)
    clock.t = 3.0  # slept it off
    assert throttle.debit(0) == 0.0
    assert throttle.debit(1000) == pytest.approx(1.0)
    # A long pause drains the debt but never banks credit: no burst later.
    clock.t = 100.0
    assert throttle.debit(1000) == pytest.approx(1.0)


def test_network_counter_paces_when_throttled(monkeypatch):
    slept: list[float] = []
    clock = Clock(0.0)

    def sleep(seconds):
        slept.append(seconds)
        clock.t += seconds

    monkeypatch.setattr("darsay.transfer.time.sleep", sleep)
    monkeypatch.setattr("darsay.transfer.time.monotonic", clock)
    session = {"bytes_network": 0}
    counter = NetworkCounter(session, throttle=Throttle(1000, clock=clock))
    counter.add(1000)  # 1 s of debt -> paced in slices
    assert sum(slept) == pytest.approx(1.0)
    assert all(0 < s <= PACE_SLICE_S for s in slept)
    assert session["bytes_network"] == 1000


def test_network_counter_pacing_yields_to_a_stop(monkeypatch):
    slept: list[float] = []
    clock = Clock(0.0)
    ctrl = StopController()

    def sleep(seconds):
        slept.append(seconds)
        clock.t += seconds
        ctrl.interrupted = True  # Ctrl-C lands mid-sleep

    monkeypatch.setattr("darsay.transfer.time.sleep", sleep)
    monkeypatch.setattr("darsay.transfer.time.monotonic", clock)
    counter = NetworkCounter(
        {"bytes_network": 0}, ctrl, throttle=Throttle(10, clock=clock)
    )
    counter.add(10)  # 1 s of debt; the first slice sees the interrupt
    assert slept == [PACE_SLICE_S]
    counter.add(10)  # banks the interrupt; must not sleep off the debt
    assert slept == [PACE_SLICE_S]
    assert counter.pending_stop is not None


def test_link_tracks_outage_and_reports_bytes_as_recovery():
    clock = Clock(100.0)
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
    # Bytes draining from a stream opened before the drop do not end the
    # outage ...
    link.online()
    assert link.offline
    # ... but an attempt begun during it does, whichever path it is for.
    clock.t = 102.0
    link.retrying("b.bin")
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


def test_wait_for_link_sleeps_out_the_interval(monkeypatch):
    clock = Clock(0.0)
    monkeypatch.setattr("darsay.transfer.time.monotonic", clock)
    monkeypatch.setattr("darsay.transfer.time.sleep", _advance(clock))
    link = Link(60, {"reconnects": 0}, clock=clock)
    _wait_for_link(link, "a.bin", "timed out", 0, None, {"bytes_network": 0})
    assert clock.t == pytest.approx(RECONNECT_WAITS_S[0])
    # Still offline: the attempt, not the wait, is what proves the link.
    assert link.snapshot()["state"] == "offline"


def test_wait_for_link_ends_early_once_another_worker_is_back(monkeypatch):
    clock = Clock(0.0)
    monkeypatch.setattr("darsay.transfer.time.monotonic", clock)
    link = Link(60, {"reconnects": 0}, clock=clock)

    def sleep(seconds):
        clock.t += seconds
        link.retrying("b.bin")  # another worker's attempt receives bytes
        link.online()

    monkeypatch.setattr("darsay.transfer.time.sleep", sleep)
    _wait_for_link(link, "a.bin", "timed out", 0, None, {"bytes_network": 0})
    assert clock.t < RECONNECT_WAITS_S[0]
    assert not link.offline


def test_wait_for_link_gives_up_after_patience(monkeypatch):
    clock = Clock(0.0)
    monkeypatch.setattr("darsay.transfer.time.monotonic", clock)
    monkeypatch.setattr("darsay.transfer.time.sleep", _advance(clock))
    link = Link(5, {"reconnects": 0}, clock=clock)
    session = {"bytes_network": 0}
    _wait_for_link(link, "a.bin", "connection reset", 0, None, session)
    with pytest.raises(CleanStop) as stop:
        _wait_for_link(link, "a.bin", "connection reset", 4, None, session)
    assert stop.value.reason == "offline"
    assert "network unreachable for" in stop.value.detail
    assert "connection reset" in stop.value.detail


def test_wait_for_link_zero_patience_pauses_at_once():
    link = Link(0, {"reconnects": 0})
    with pytest.raises(CleanStop) as stop:
        _wait_for_link(link, "a.bin", "DNS lookup failed", 0, None, {})
    assert stop.value.reason == "offline"
    assert link.snapshot() is None


def test_wait_for_link_honors_interrupt(monkeypatch):
    monkeypatch.setattr("darsay.transfer.time.sleep", lambda s: None)
    ctrl = StopController()
    ctrl.interrupted = True
    link = Link(60, {"reconnects": 0})
    with pytest.raises(CleanStop) as stop:
        _wait_for_link(link, "a.bin", "timed out", 0, ctrl, {"bytes_network": 0})
    assert stop.value.reason == "interrupt"


class _FlakyProvider:
    """``download_file`` raises the queued errors first, then succeeds."""

    def __init__(self, link: Link, *errors: BaseException):
        self.link = link
        self.errors = list(errors)
        self.calls: list[dict] = []

    def download_file(
        self, source, revision, relative, payload_dir, *, force, tqdm_class
    ):
        self.calls.append({"force": force, "link": self.link.snapshot()})
        if self.errors:
            raise self.errors.pop(0)

    def transient_network_error(self, exc):
        return describe_network_error(exc)


def _outage_fixture(monkeypatch, patience: float = 60):
    clock = Clock(0.0)
    monkeypatch.setattr("darsay.transfer.time.monotonic", clock)
    monkeypatch.setattr("darsay.transfer.time.sleep", _advance(clock))
    session = {"bytes_network": 0, "reconnects": 0}
    link = Link(patience, session, clock=clock)
    return link, NetworkCounter(session, link=link)


def test_fetch_with_reconnect_marks_the_attempt_and_resumes(monkeypatch, tmp_path):
    link, counter = _outage_fixture(monkeypatch)
    provider = _FlakyProvider(link, ConnectionResetError("reset"))
    events: list[dict] = []
    _fetch_with_reconnect(
        provider,
        None,
        {"revision": "r"},
        "a.bin",
        tmp_path,
        force=True,
        tqdm_class=None,
        counter=counter,
        events=events,
    )
    first, retry = provider.calls
    assert first["link"] is None and first["force"] is True
    # The retry is announced as in flight, and resumes rather than restarts.
    assert retry["link"]["state"] == "reconnecting"
    assert retry["force"] is False
    assert link.snapshot() is None
    assert counter.session["reconnects"] == 1
    assert [e["event"] for e in events] == ["network_lost", "network_restored"]
    assert events[1]["detail"] == "resumed after 1 reconnect attempt"


def test_fetch_with_reconnect_never_waits_out_an_interrupt(monkeypatch, tmp_path):
    # A KeyboardInterrupt raised while a transport error was being handled
    # carries that error as its context; it must still propagate.
    try:
        try:
            raise ConnectionResetError("reset")
        except ConnectionResetError:
            raise KeyboardInterrupt() from None
    except KeyboardInterrupt as exc:
        interrupt = exc
    assert describe_network_error(interrupt) == "connection reset"
    link, counter = _outage_fixture(monkeypatch)
    provider = _FlakyProvider(link, interrupt)
    with pytest.raises(KeyboardInterrupt):
        _fetch_with_reconnect(
            provider,
            None,
            {"revision": "r"},
            "a.bin",
            tmp_path,
            force=False,
            tqdm_class=None,
            counter=counter,
            events=[],
        )
    assert link.snapshot() is None


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


def test_stop_controller_headroom_pauses_before_a_file_that_cannot_fit(
    monkeypatch, tmp_path
):
    from types import SimpleNamespace

    disk = {"free": 6 * 1024**3}
    monkeypatch.setattr(
        "darsay.transfer.shutil.disk_usage",
        lambda path: SimpleNamespace(free=disk["free"]),
    )
    floor = 2 * 1024**3
    ctrl = StopController(min_free_bytes=floor, disk_path=tmp_path)
    # 4 GiB of headroom: a 3 GiB file may begin, a 5 GiB one may not.
    ctrl.check_headroom("small.bin", 3 * 1024**3)
    with pytest.raises(CleanStop) as stop:
        ctrl.check_headroom("model-00023-of-00141.safetensors", 5 * 1024**3)
    assert stop.value.reason == "disk"
    assert "model-00023-of-00141.safetensors needs 5.0 GiB more" in stop.value.detail
    assert "only 4.0 GiB is free above the 2.0 GiB floor" in stop.value.detail
    # Sticky like the floor: the ordinary check trips too, without probing.
    with pytest.raises(CleanStop) as again:
        ctrl.check({"bytes_network": 0})
    assert again.value is stop.value

    def forbidden(_path):
        raise AssertionError("probed with nothing to fetch or no floor")

    monkeypatch.setattr("darsay.transfer.shutil.disk_usage", forbidden)
    StopController(min_free_bytes=floor, disk_path=tmp_path).check_headroom("x", 0)
    StopController(disk_path=tmp_path).check_headroom("x", 10**12)
    StopController(min_free_bytes=0, disk_path=tmp_path).check_headroom("x", 10**12)


def test_disk_full_is_recognised_through_the_cause_chain():
    import errno

    from darsay.transfer import _disk_full, _disk_stop, _full_while_writing

    full = OSError(errno.ENOSPC, "No space left on device")
    assert _disk_full(full)
    wrapped = RuntimeError("write failed")
    wrapped.__cause__ = full
    assert _disk_full(wrapped)
    assert not _disk_full(OSError(errno.EIO, "io"))
    assert not _disk_full(ValueError("nope"))

    ctrl = StopController(min_free_bytes=1, disk_path=None)
    stop = _disk_stop(ctrl, _full_while_writing("weights.bin"))
    assert stop.reason == "disk"
    assert "no space left on device while writing weights.bin" in stop.detail
    assert ctrl.wants_stop()
    # Sticky: the first detail wins for every worker after it.
    assert _disk_stop(ctrl, "later") is stop
    assert _disk_stop(None, "w.bin is stuck").reason == "disk"


def test_fetch_with_reconnect_turns_enospc_into_a_disk_pause(tmp_path):
    import errno

    class _FullDisk:
        calls = 0

        def download_file(self, *args, **kwargs):
            self.calls += 1
            raise OSError(errno.ENOSPC, "No space left on device")

        def transient_network_error(self, exc):
            # Would be waited out for an hour if the disk were mistaken for
            # the network.
            return "connection reset"

    session = {"bytes_network": 0, "reconnects": 0}
    ctrl = StopController(min_free_bytes=1, disk_path=None)
    link = Link(3600.0, session)
    counter = NetworkCounter(session, ctrl, link=link)
    provider = _FullDisk()
    events: list[dict] = []
    with pytest.raises(CleanStop) as stop:
        _fetch_with_reconnect(
            provider,
            None,
            {"revision": "r"},
            "w.bin",
            tmp_path,
            force=False,
            tqdm_class=None,
            counter=counter,
            events=events,
        )
    assert stop.value.reason == "disk"
    assert provider.calls == 1
    assert ctrl.wants_stop()
    assert events == []
    assert link.snapshot() is None


def test_record_download_result_treats_a_full_disk_as_a_pause(monkeypatch, tmp_path):
    import errno

    from darsay.transfer import _record_download_result

    def full(_bundle_dir, _ledger):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr("darsay.transfer.save_ledger", full)
    ledger = {"events": [], "files": {}}
    session = {"files_completed": 0, "retries": 0, "bytes_local_sources": 0}
    result = {
        "path": "a.bin",
        "record": {"status": "verified", "source": "network"},
        "events": [],
        "retries": 0,
    }
    ctrl = StopController(min_free_bytes=1, disk_path=None)
    with pytest.raises(CleanStop) as stop:
        _record_download_result(tmp_path, ledger, session, result, ctrl)
    assert stop.value.reason == "disk"
    assert "ledger could not be written after a.bin" in stop.value.detail
    assert ctrl.wants_stop()
    # The in-memory state is already committed; reconciliation re-derives
    # it on disk next run.
    assert ledger["files"]["a.bin"]["status"] == "verified"


def test_finish_session_tolerates_a_full_disk_only_for_a_disk_pause(
    monkeypatch, tmp_path
):
    import errno

    from darsay.transfer import finish_session

    def full(_bundle_dir, _ledger):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr("darsay.transfer.save_ledger", full)
    session = {"ended": None, "end_reason": None}
    assert finish_session(tmp_path, {}, session, "disk") is False
    assert session["end_reason"] == "disk" and session["ended"]
    with pytest.raises(OSError):
        finish_session(tmp_path, {}, session, "budget")
    monkeypatch.setattr("darsay.transfer.save_ledger", lambda _b, _l: None)
    assert finish_session(tmp_path, {}, session, "disk") is True


def test_live_transfer_reports_how_it_ended():
    from darsay.transfer import _live_transfer

    class _Display:
        def __init__(self):
            self.verdicts: list = []

        def start(self):
            pass

        def stop(self, verdict=None):
            self.verdicts.append(verdict)

        def echo(self, *args, **kwargs):
            pass

    display = _Display()
    with _live_transfer(display):
        pass
    with pytest.raises(CleanStop), _live_transfer(display):
        raise CleanStop("disk", "full")
    with pytest.raises(CleanStop), _live_transfer(display):
        raise CleanStop("budget", "cap")
    with pytest.raises(KeyboardInterrupt), _live_transfer(display):
        raise KeyboardInterrupt
    with pytest.raises(ValueError), _live_transfer(display):
        raise ValueError("boom")
    assert display.verdicts == [
        "complete",
        "paused: disk",
        "paused: budget",
        "aborted",
        "error: ValueError",
    ]


def test_session_rate_needs_a_minute_of_moving_bytes():
    from darsay.transfer import session_rate

    ledger: dict = {"sessions": []}
    assert session_rate(ledger) is None
    ledger["sessions"].append(
        {
            "started": "2026-08-29T00:00:00+00:00",
            "ended": "2026-08-29T00:00:30+00:00",
            "bytes_network": 30 * 1024**2,
        }
    )
    assert session_rate(ledger) is None  # thirty seconds says nothing yet
    ledger["sessions"].append(
        {
            "started": "2026-08-29T01:00:00+00:00",
            "ended": "2026-08-29T01:01:30+00:00",
            "bytes_network": 90 * 1024**2,
        }
    )
    assert session_rate(ledger) == pytest.approx(1024**2)
    # Unfinished, byte-less, or malformed sessions are skipped, not fatal.
    ledger["sessions"].extend(
        [
            {
                "started": "2026-08-29T02:00:00+00:00",
                "ended": None,
                "bytes_network": 10**9,
            },
            {
                "started": "2026-08-29T03:00:00+00:00",
                "ended": "later",
                "bytes_network": 5,
            },
            {"started": "x", "ended": "y", "bytes_network": 0},
        ]
    )
    assert session_rate(ledger) == pytest.approx(1024**2)


def test_disk_outlook_counts_the_files_that_fit_and_prices_the_wait(tmp_path):
    from darsay.transfer import disk_outlook

    gib = 1024**3
    ledger = {
        "provider": "huggingface",
        "expected": [
            {"path": "b.bin", "size": 5 * gib},
            {"path": "a.bin", "size": 1 * gib},
            {"path": "c.bin", "size": 5 * gib},
            {"path": "done.bin", "size": 9 * gib},
        ],
        "files": {"done.bin": {"status": "verified"}},
        "sessions": [
            {
                "started": "2026-08-29T00:00:00+00:00",
                "ended": "2026-08-29T00:10:00+00:00",
                "bytes_network": 600 * 1024**2,
            }
        ],
    }
    plan = {
        "files": {"verified": 1, "partial": 0, "missing": 3, "total": 4},
        "bytes": {"remaining_network": 11 * gib},
        "disk": {
            "free_bytes": 9 * gib,
            "min_free_bytes": 2 * gib,
            "needed_bytes": 11 * gib,
            "verdict": "insufficient",
        },
    }
    # 7 GiB usable above the floor: a.bin and b.bin land, c.bin does not.
    lines = disk_outlook(plan, ledger, tmp_path)
    assert lines == [
        "WARNING: disk preflight is insufficient; the transfer will pause "
        "at the free-space floor",
        "  after about 7.0 GiB more (2 of 3 remaining files), "
        "roughly 1h 59 min at 1.0 MiB/s.",
        "  Free space (or move the vault to a larger disk), then re-run to continue.",
    ]
    # A rate cap below the observed pace is the honest one.
    capped = disk_outlook(plan, ledger, tmp_path, max_rate=512 * 1024)
    assert "roughly 3h 58 min at 512.0 KiB/s" in capped[1]
    # No pace on record: the bytes and files still tell the story.
    ledger["sessions"].clear()
    assert disk_outlook(plan, ledger, tmp_path)[1].endswith("(2 of 3 remaining files).")
    # Everything fits (the floor alone made the verdict): no hint to free space.
    plan["disk"]["free_bytes"] = 14 * gib
    assert disk_outlook(plan, ledger, tmp_path)[1:] == [
        "  after about 12.0 GiB more (3 of 3 remaining files)."
    ]
    # Without a floor the disk itself is the stop.
    plan["disk"]["min_free_bytes"] = None
    plan["disk"]["free_bytes"] = 7 * gib
    assert disk_outlook(plan, ledger, tmp_path)[0].endswith("when the disk fills")


def test_begin_session_records_the_tool_version(tmp_path):
    from darsay import __version__
    from darsay.transfer import begin_session

    ledger = {"sessions": []}
    session = begin_session(tmp_path, ledger)
    assert session["tool"] == f"darsay {__version__}"
    assert (tmp_path / "transfer.json").is_file()

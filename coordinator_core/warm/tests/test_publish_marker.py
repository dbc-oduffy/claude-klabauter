"""Publish marker: while held, no warm spawn decision returns True."""

from __future__ import annotations

import json
import os
import subprocess
import time

import pytest

from coordinator_core.warm import breadcrumb, front_door, supervisor


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.setenv(breadcrumb.RUNTIME_BASE_ENV, str(tmp_path / "rt"))
    eng = tmp_path / "engine"
    eng.mkdir()
    monkeypatch.setattr(breadcrumb, "try_claim_boot", lambda *a, **k: True)
    return eng


def _all_three(root):
    return (
        breadcrumb.should_spawn(root),
        supervisor.should_spawn(root),
        front_door.should_spawn(root),
    )


def test_handover_pending_held_inside_the_hold_and_released_after(root):
    assert not breadcrumb.handover_pending_held(root)
    with breadcrumb.hold_handover_pending(root) as path:
        assert path.exists()
        assert breadcrumb.handover_pending_held(root)
        with breadcrumb.hold_handover_pending(root):
            assert breadcrumb.handover_pending_held(root)
        assert breadcrumb.handover_pending_held(root)
    assert not breadcrumb.handover_pending_held(root)
    assert not path.exists()


def test_handover_pending_expires_so_a_crashed_publish_cannot_wedge_mints(root):
    with breadcrumb.hold_handover_pending(root):
        future = time.time() + breadcrumb.HANDOVER_PENDING_TTL_SECS + 1
        assert not breadcrumb.handover_pending_held(root, now=future)


def test_handover_pending_unreadable_reads_not_held(root):
    path = breadcrumb.handover_pending_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    assert not breadcrumb.handover_pending_held(root)


def test_handover_pending_exit_leaves_a_hold_another_publisher_rewrote(root):
    with breadcrumb.hold_handover_pending(root) as path:
        path.write_text(json.dumps({"pid": 1, "token": "other", "expires_at": time.time() + 60}), encoding="utf-8")
    assert path.exists()


def test_marker_blocks_all_three_and_release_allows(root):
    with breadcrumb.hold_publish_marker(root, holder="t") as path:
        assert path.exists()
        assert _all_three(root) == (False, False, False)
    assert not path.exists()
    assert _all_three(root) == (True, True, True)


def test_ensure_listener_records_no_spawn_while_held(root, monkeypatch):
    calls = []
    monkeypatch.setattr(supervisor, "is_engine_root", lambda r: True)
    monkeypatch.setattr(supervisor, "spawn_detached", lambda *a, **k: calls.append(a) or True)
    with breadcrumb.hold_publish_marker(root, holder="t"):
        assert supervisor.ensure_listener(root) is None
    assert calls == []
    supervisor.ensure_listener(root)
    assert len(calls) == 1


def test_dead_holder_does_not_block(root, monkeypatch):
    with breadcrumb.hold_publish_marker(root, holder="t"):
        monkeypatch.setattr(breadcrumb, "stable_pid_alive", lambda *a, **k: False)
        assert breadcrumb.should_spawn(root) is True


def test_unparseable_marker_blocks_until_aged_out(root):
    path = breadcrumb.publish_marker_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    svc = path.parent
    assert breadcrumb.publish_marker_held(svc) is True
    later = time.time() + breadcrumb.PUBLISH_MARKER_MAX_AGE_SECS + 5
    assert breadcrumb.publish_marker_held(svc, now=later) is False
    assert path.exists()


def test_check_spawns_no_subprocess(root, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("subprocess spawned")

    monkeypatch.setattr(subprocess, "Popen", boom)
    with breadcrumb.hold_publish_marker(root, holder="t"):
        assert breadcrumb.publish_marker_held(breadcrumb.svc_dir(root)) is True
        assert breadcrumb.should_spawn(root) is False


def test_exit_leaves_marker_rewritten_by_another_pid(root):
    with breadcrumb.hold_publish_marker(root, holder="t") as path:
        rec = json.loads(path.read_text(encoding="utf-8"))
        rec["pid"] = os.getpid() + 1
        path.write_text(json.dumps(rec), encoding="utf-8")
    assert path.exists()


def _write_marker_for(root, pid, holder="parent"):
    from coordinator_core.session.core import _win_create_time_epoch

    path = breadcrumb.publish_marker_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    rec = {
        "pid": pid,
        "stable_pid_start_epoch": _win_create_time_epoch(pid),
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "holder": holder,
    }
    path.write_text(json.dumps(rec), encoding="utf-8")
    return path, rec


_FAKE_ANCESTOR = 2**22 + 7


def _stub_ancestor(monkeypatch, pid=_FAKE_ANCESTOR, alive=True):
    monkeypatch.setattr(breadcrumb, "stable_pid_alive", lambda *a, **k: alive)
    monkeypatch.setattr(breadcrumb, "_is_ancestor_pid", lambda p: p == pid)


def test_nested_hold_leaves_live_ancestor_marker_intact(root, monkeypatch):
    _stub_ancestor(monkeypatch)
    path, rec = _write_marker_for(root, _FAKE_ANCESTOR)
    before = path.read_text(encoding="utf-8")
    with breadcrumb.hold_publish_marker(root, holder="child") as held:
        assert held == path
        assert path.read_text(encoding="utf-8") == before
        assert breadcrumb.publish_marker_held(breadcrumb.svc_dir(root)) is True
    assert path.read_text(encoding="utf-8") == before


def test_same_pid_nested_hold_is_reentrant(root):
    with breadcrumb.hold_publish_marker(root, holder="outer") as path:
        before = path.read_bytes()
        with breadcrumb.hold_publish_marker(root, holder="inner") as inner:
            assert inner == path
            assert path.read_bytes() == before
        assert path.read_bytes() == before
        assert breadcrumb.publish_marker_held(breadcrumb.svc_dir(root)) is True
    assert not path.exists()


def test_pid_reuse_marker_is_rewritten_and_released(root):
    path, rec = _write_marker_for(root, os.getpid())
    rec["stable_pid_start_epoch"] = "1"
    path.write_text(json.dumps(rec), encoding="utf-8")
    with breadcrumb.hold_publish_marker(root, holder="t"):
        now = json.loads(path.read_text(encoding="utf-8"))
        assert now["pid"] == os.getpid()
        assert str(now["stable_pid_start_epoch"]) != "1"
        assert now["holder"] == "t"
    assert not path.exists()


_CHILD = (
    "import sys\n"
    "from pathlib import Path\n"
    "from coordinator_core.warm import breadcrumb\n"
    "with breadcrumb.hold_publish_marker(Path(sys.argv[1]), holder='child') as p:\n"
    "    import json\n"
    "    print(json.loads(p.read_text(encoding='utf-8'))['pid'])\n"
)


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_real_child_nests_under_real_parent_marker(root):
    pytest.importorskip("psutil")
    import sys
    from pathlib import Path

    env = dict(os.environ)
    env[breadcrumb.RUNTIME_BASE_ENV] = os.environ[breadcrumb.RUNTIME_BASE_ENV]
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[3])
    with breadcrumb.hold_publish_marker(root, holder="parent") as path:
        before = path.read_bytes()
        proc = subprocess.run(
            [sys.executable, "-c", _CHILD, str(root)],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        assert proc.returncode == 0, proc.stderr
        assert int(proc.stdout.strip()) == os.getpid()
        assert path.read_bytes() == before


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_live_non_ancestor_marker_is_overwritten_and_released(root):
    import sys

    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    try:
        path, _ = _write_marker_for(root, child.pid)
        with breadcrumb.hold_publish_marker(root, holder="t"):
            assert json.loads(path.read_text(encoding="utf-8"))["pid"] == os.getpid()
        assert not path.exists()
    finally:
        child.kill()
        child.wait()


def test_stale_ancestor_marker_is_reclaimed(root, monkeypatch):
    _stub_ancestor(monkeypatch, alive=False)
    path, _ = _write_marker_for(root, _FAKE_ANCESTOR)
    with breadcrumb.hold_publish_marker(root, holder="t"):
        assert json.loads(path.read_text(encoding="utf-8"))["pid"] == os.getpid()
    assert not path.exists()


def test_aged_out_ancestor_marker_is_reclaimed(root, monkeypatch):
    _stub_ancestor(monkeypatch)
    path, rec = _write_marker_for(root, _FAKE_ANCESTOR)
    old = time.gmtime(time.time() - breadcrumb.PUBLISH_MARKER_MAX_AGE_SECS - 60)
    rec["started_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", old)
    path.write_text(json.dumps(rec), encoding="utf-8")
    with breadcrumb.hold_publish_marker(root, holder="t"):
        assert json.loads(path.read_text(encoding="utf-8"))["pid"] == os.getpid()
    assert not path.exists()


def test_aged_out_marker_with_live_holder_does_not_block(root):
    with breadcrumb.hold_publish_marker(root, holder="t"):
        later = time.time() + breadcrumb.PUBLISH_MARKER_MAX_AGE_SECS + 5
        assert breadcrumb.publish_marker_held(breadcrumb.svc_dir(root), now=later) is False


def test_extra_liveness_false_branch_gated_by_marker(root, monkeypatch):
    started = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    record = {"pid": os.getpid(), "started_at": started, "stable_pid_start_epoch": 0}
    lock = breadcrumb.boot_lock_path(breadcrumb.breadcrumb_path(root))

    def decide():
        return breadcrumb.should_spawn_decision(
            record, lock_path=lock, extra_liveness=lambda r: False
        )

    # the record's own pid check must pass for the extra_liveness branch
    monkeypatch.setattr(breadcrumb, "stable_pid_alive", lambda *a, **k: True)
    with breadcrumb.hold_publish_marker(root, holder="t"):
        assert decide() is False
    assert decide() is True


class _FakeClock:
    def __init__(self):
        self.now = 0.0
        self.slept = 0.0

    def clock(self):
        return self.now

    def sleep(self, secs):
        self.slept += secs
        self.now += secs


def test_drain_wait_secs_is_under_the_load_norm():
    assert 0 < breadcrumb.DRAIN_WAIT_SECS <= 0.5


def test_wait_returns_true_at_once_when_nothing_is_held(root):
    fake = _FakeClock()
    assert breadcrumb.wait_for_publish_release(root, sleep=fake.sleep, clock=fake.clock) is True
    assert fake.slept == 0


def test_wait_times_out_false_and_is_clamped_to_drain_wait(root):
    fake = _FakeClock()
    with breadcrumb.hold_publish_marker(root, holder="t") as path:
        got = breadcrumb.wait_for_publish_release(root, 30.0, sleep=fake.sleep, clock=fake.clock)
        assert path.exists(), "the waiter must never unlink the marker"
    assert got is False
    assert fake.slept <= breadcrumb.DRAIN_WAIT_SECS + 1e-9


def test_wait_returns_true_when_marker_released_mid_wait(root):
    fake = _FakeClock()
    with breadcrumb.hold_publish_marker(root, holder="t") as path:

        def release_on_first_sleep(secs):
            fake.sleep(secs)
            path.unlink()

        got = breadcrumb.wait_for_publish_release(
            root, sleep=release_on_first_sleep, clock=fake.clock
        )
    assert got is True
    assert fake.slept < breadcrumb.DRAIN_WAIT_SECS


def test_swap_wait_outlasts_drain_wait_and_returns_true_on_release(root):
    fake = _FakeClock()
    with breadcrumb.hold_publish_marker(root, holder="t") as path:

        def release_after_swap(secs):
            fake.sleep(secs)
            if fake.slept > 5 * breadcrumb.DRAIN_WAIT_SECS:
                path.unlink()

        got = breadcrumb.wait_for_publish_swap(root, sleep=release_after_swap, clock=fake.clock)
    assert got is True
    assert fake.slept > breadcrumb.DRAIN_WAIT_SECS


def test_swap_wait_is_bounded_by_the_backstop_when_heartbeat_keeps_ticking(root):
    fake = _FakeClock()
    with breadcrumb.hold_publish_marker(root, holder="t") as path:
        got = breadcrumb.wait_for_publish_swap(
            root, sleep=fake.sleep, clock=fake.clock, wall=lambda: os.stat(path).st_mtime
        )
        assert path.exists()
    assert got is False
    assert breadcrumb.PUBLISH_SWAP_BACKSTOP_SECS - 1 <= fake.slept <= breadcrumb.PUBLISH_SWAP_BACKSTOP_SECS + 1e-9


def _age_marker(path, secs):
    old = time.time() - secs
    os.utime(path, (old, old))


def test_swap_wait_returns_at_once_on_live_holder_with_stale_heartbeat(root):
    fake = _FakeClock()
    with breadcrumb.hold_publish_marker(root, holder="t") as path:
        _age_marker(path, breadcrumb.PUBLISH_HEARTBEAT_STALE_SECS + 5)
        got = breadcrumb.wait_for_publish_swap(root, sleep=fake.sleep, clock=fake.clock)
        assert path.exists(), "the waiter must never unlink the marker"
    assert got is False
    assert fake.slept == 0


def test_swap_wait_keeps_waiting_on_a_fresh_heartbeat_then_drains(root):
    fake = _FakeClock()
    with breadcrumb.hold_publish_marker(root, holder="t") as path:
        _age_marker(path, breadcrumb.PUBLISH_HEARTBEAT_STALE_SECS - 10)

        def release(secs):
            fake.sleep(secs)
            if fake.slept > 1.0:
                path.unlink()

        got = breadcrumb.wait_for_publish_swap(root, sleep=release, clock=fake.clock)
    assert got is True


def test_marker_without_heartbeat_field_is_never_wedged(root):
    svc = breadcrumb.svc_dir(root)
    with breadcrumb.hold_publish_marker(root, holder="t") as path:
        record = json.loads(path.read_text(encoding="utf-8"))
        del record["heartbeat_secs"]
        path.write_text(json.dumps(record), encoding="utf-8")
        _age_marker(path, 600)
        assert breadcrumb.publish_marker_held(svc)
        assert breadcrumb.publish_marker_wedged(svc) is False


def test_holder_thread_refreshes_marker_mtime(root, monkeypatch):
    monkeypatch.setattr(breadcrumb, "PUBLISH_HEARTBEAT_SECS", 0.05)
    with breadcrumb.hold_publish_marker(root, holder="t") as path:
        _age_marker(path, 600)
        deadline = time.time() + 5
        while time.time() < deadline and breadcrumb.publish_marker_wedged(breadcrumb.svc_dir(root)):
            time.sleep(0.02)
        assert breadcrumb.publish_marker_wedged(breadcrumb.svc_dir(root)) is False


def test_wedge_remedy_names_holder_pid_and_marker_path(root):
    with breadcrumb.hold_publish_marker(root, holder="t") as path:
        remedy = breadcrumb.publish_wedge_remedy(root)
    assert str(os.getpid()) in remedy and str(path) in remedy

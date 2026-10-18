"""single_flight: marker check, exclusive lock, dead-holder and awaiting-finish takeover."""
import json
import os
import subprocess
import sys

import pytest

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

from coordinator_core.ceremony_common.run import single_flight as sf
from coordinator_core.win_portability import no_console_creationflags

TODAY = "2026-10-12"
YESTERDAY = "2026-10-11"


@pytest.fixture
def env(tmp_path):
    (tmp_path / ".git").mkdir()
    return tmp_path, tmp_path / "marker"


def _acq(env, session="s1", today=TODAY):
    root, marker = env
    return sf.acquire(root, ceremony="fixture-ceremony", marker_path=marker, today=today, session_id=session)


def _dead_pid():
    p = subprocess.Popen([sys.executable, "-c", "pass"], **no_console_creationflags())
    p.wait()
    return p.pid


def _write_lock(adm, **over):
    rec = json.loads(adm.lock_path.read_text())
    rec.update(over)
    adm.lock_path.write_text(json.dumps(rec))


def test_admits_then_second_is_in_flight(env):
    a = _acq(env)
    assert a.state == sf.ADMITTED and a.run_id
    b = _acq(env, "s2")
    assert b.state == sf.IN_FLIGHT and b.run_id == a.run_id


def test_no_marker_path_skips_the_once_per_day_check(env):
    root, _ = env
    first = sf.acquire(root, ceremony="fixture-ceremony", today=TODAY, session_id="s1")
    assert first.state == sf.ADMITTED
    assert sf.release(first)
    again = sf.acquire(root, ceremony="fixture-ceremony", today=TODAY, session_id="s2")
    assert again.state == sf.ADMITTED


def test_release_allows_next(env):
    a = _acq(env)
    assert sf.release(a)
    assert _acq(env, "s2").state == sf.ADMITTED


def test_release_by_other_run_id_leaves_lock(env):
    a = _acq(env)
    assert not sf.release_run(a.lock_path, "other")
    assert a.lock_path.exists()


def test_dead_pid_launching_lock_taken_over(env):
    a = _acq(env)
    _write_lock(a, pid=_dead_pid())
    b = _acq(env, "s2")
    assert b.state == sf.ADMITTED and b.run_id != a.run_id and b.abandoned_run_id == ""


def test_alive_holder_not_taken_over(env):
    a = _acq(env)
    _write_lock(a, pid=os.getpid())
    assert _acq(env, "s2").state == sf.IN_FLIGHT


def test_unreadable_lock_is_in_flight(env):
    a = _acq(env)
    a.lock_path.write_text("{not json")
    assert _acq(env, "s2").state == sf.IN_FLIGHT


def test_marker_today_is_already_ran(env):
    env[1].write_text(TODAY)
    a = _acq(env)
    assert a.state == sf.ALREADY_RAN and a.run_dir.name == TODAY


def test_marker_yesterday_admits(env):
    env[1].write_text(YESTERDAY)
    assert _acq(env).state == sf.ADMITTED


def test_awaiting_finish_today_dead_pid_is_in_flight(env):
    a = _acq(env)
    sf.mark_awaiting_finish(a)
    _write_lock(a, pid=_dead_pid())
    b = _acq(env, "s2")
    assert b.state == sf.IN_FLIGHT and b.run_id == a.run_id


def test_awaiting_finish_yesterday_taken_over_with_abandoned_id(env):
    a = _acq(env, today=YESTERDAY)
    sf.mark_awaiting_finish(a)
    b = _acq(env, "s2")
    assert b.state == sf.ADMITTED and b.abandoned_run_id == a.run_id


def test_gitfile_worktree_resolves_common_dir(tmp_path):
    common = tmp_path / "main" / ".git"
    (common / "worktrees" / "w").mkdir(parents=True)
    (common / "worktrees" / "w" / "commondir").write_text("../..")
    wt = tmp_path / "wt"
    wt.mkdir()
    (wt / ".git").write_text(f"gitdir: {common / 'worktrees' / 'w'}")
    a = sf.acquire(wt, ceremony="fixture-ceremony", marker_path=wt / "m", today=TODAY, session_id="s")
    assert a.state == sf.ADMITTED
    assert a.lock_path.resolve().parent == (common / "coordinator-sessions" / "fixture-ceremony-runs").resolve()

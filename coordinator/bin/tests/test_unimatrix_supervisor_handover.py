"""Supervisor stop-request channel: a request addressed to (pid, procStart) is honoured by that process only."""
import functools
import importlib.util
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

BIN = Path(__file__).resolve().parents[1]


def _load(filename, name):
    spec = importlib.util.spec_from_file_location(name, BIN / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


roster = _load("unimatrix-roster.py", "handover_roster")
autolink = _load("unimatrix-autolink.py", "handover_autolink")
REMOTE = "example-operator@machine-a.local"
MINI = "example-operator@macmini.local"


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path))
    (tmp_path / "unimatrix").mkdir()
    return tmp_path / "unimatrix"


def test_stop_request_path_is_per_pid_under_the_settings_home(home):
    assert roster.stop_request_path(77) == home / "stop-77.json"


def test_stop_request_carries_identity_and_is_written_atomically(home):
    roster.request_stop(os.getpid(), "start-A", "bridge", REMOTE)
    body = json.loads((home / f"stop-{os.getpid()}.json").read_text())
    assert {k: body[k] for k in ("pid", "procStart", "role", "remote")} == {
        "pid": os.getpid(), "procStart": "start-A", "role": "bridge", "remote": REMOTE}
    assert isinstance(body["requestedAt"], float)
    assert not list(home.glob("*.tmp"))


def test_stop_request_is_seen_by_the_addressed_process_only(home):
    roster.request_stop(os.getpid(), "start-A", "bridge", REMOTE)
    assert roster.stop_requested("start-A") is True
    # A replacement reusing the pid has a different procStart: it never reads the request as its own.
    assert roster.stop_requested("start-B") is False


def test_stop_request_for_another_pid_is_ignored(home):
    roster.request_stop(os.getpid() + 1, "start-A", "bridge", REMOTE)
    assert roster.stop_requested("start-A") is False


def test_no_request_or_corrupt_request_is_not_a_stop(home):
    assert roster.stop_requested("start-A") is False
    (home / f"stop-{os.getpid()}.json").write_text("{not json")
    assert roster.stop_requested("start-A") is False
    (home / f"stop-{os.getpid()}.json").write_text("[]")
    assert roster.stop_requested("start-A") is False


def test_clear_stop_removes_request_and_stamp_and_tolerates_absence(home):
    pid = os.getpid()
    roster.request_stop(pid, "start-A", "inbox", REMOTE)
    (home / f"supervisor-{pid}.json").write_text("{}")
    roster.clear_stop(pid)
    assert not list(home.iterdir())
    roster.clear_stop(pid)


def test_stamp_records_role_only_when_given(home, monkeypatch):
    monkeypatch.setattr(roster, "proc_start", lambda pid, platform=None: f"start-{pid}")
    stamp = home / f"supervisor-{os.getpid()}.json"
    roster.stamp_supervisor(REMOTE)
    assert "role" not in json.loads(stamp.read_text())
    roster.stamp_supervisor(REMOTE, "bridge")
    assert json.loads(stamp.read_text())["role"] == "bridge"


def test_wait_or_stop_sleeps_in_slices_and_returns_true_on_request(home):
    sleeps = []

    def sleep(s):
        sleeps.append(s)
        if len(sleeps) == 3:
            roster.request_stop(os.getpid(), "start-A", "bridge", REMOTE)

    assert roster.wait_or_stop(30, "start-A", sleep=sleep) is True
    assert sleeps == [roster.STOP_POLL_SECONDS] * 3


def test_wait_or_stop_runs_the_full_wait_without_a_request(home):
    sleeps = []
    assert roster.wait_or_stop(2, "start-A", sleep=sleeps.append) is False
    assert sleeps == [roster.STOP_POLL_SECONDS] * 4


def _stub_main_deps(monkeypatch, bridges_killed):
    monkeypatch.setattr(roster, "proc_start", lambda pid, platform=None: f"start-{pid}")
    monkeypatch.setattr(roster, "reap_orphaned_mirrors", lambda *a, **k: None)
    monkeypatch.setattr(roster, "link_probe", lambda *a, **k: (True, "ok"))
    monkeypatch.setattr(roster, "note_link", lambda link, *a, **k: link)
    monkeypatch.setattr(roster, "fetch_roster", lambda *a, **k: [])
    monkeypatch.setattr(roster, "far_rows", lambda *a, **k: [])
    monkeypatch.setattr(roster, "sync", lambda *a, **k: None)
    monkeypatch.setattr(roster, "reap_unbacked_agy_faces", lambda *a, **k: None)
    monkeypatch.setattr(roster, "tree_kill_many", lambda pids: bridges_killed.append(list(pids)))


def _argv(tmp_path):
    return [str(tmp_path / "roster.log"), "--remote", REMOTE, "--key", str(tmp_path / "key")]


def test_stop_request_main_returns_at_loop_top_and_clears_the_request(home, tmp_path, monkeypatch):
    killed = []
    _stub_main_deps(monkeypatch, killed)
    roster.request_stop(os.getpid(), f"start-{os.getpid()}", "bridge", REMOTE)
    monkeypatch.setattr(roster.time, "sleep", lambda s: pytest.fail("loop slept after a pending stop"))
    assert roster.main(_argv(tmp_path)) is None
    assert "STOPPED-BY-REQUEST" in (tmp_path / "roster.log").read_text()
    assert not (home / f"stop-{os.getpid()}.json").exists()
    assert not (home / f"supervisor-{os.getpid()}.json").exists()
    assert killed == [[]]


def test_stop_request_main_returns_within_one_slice_of_a_request_made_mid_poll(home, tmp_path, monkeypatch):
    killed = []
    _stub_main_deps(monkeypatch, killed)
    sleeps = []

    def sleep(s):
        sleeps.append(s)
        if len(sleeps) == 2:
            roster.request_stop(os.getpid(), f"start-{os.getpid()}", "bridge", REMOTE)

    monkeypatch.setattr(roster.time, "sleep", sleep)
    roster.main(_argv(tmp_path))
    assert sleeps == [roster.STOP_POLL_SECONDS] * 2
    assert "STOPPED-BY-REQUEST" in (tmp_path / "roster.log").read_text()
    assert killed == [[]]


def test_stop_request_for_a_previous_incarnation_does_not_stop_main(home, tmp_path, monkeypatch):
    killed = []
    _stub_main_deps(monkeypatch, killed)
    roster.request_stop(os.getpid(), "start-of-a-dead-process", "bridge", REMOTE)
    sleeps = []

    def sleep(s):
        sleeps.append(s)
        if len(sleeps) == 3:
            raise KeyboardInterrupt

    monkeypatch.setattr(roster.time, "sleep", sleep)
    with pytest.raises(KeyboardInterrupt):
        roster.main(_argv(tmp_path))
    assert (home / f"stop-{os.getpid()}.json").exists()
    assert killed == [[]]


LOOP = """
import importlib.util, os, sys, time
from pathlib import Path
roster_path, role, remote, mode, quit_flag = sys.argv[1:6]
spec = importlib.util.spec_from_file_location("loop_roster", roster_path)
r = importlib.util.module_from_spec(spec)
spec.loader.exec_module(r)
own = r.proc_start(os.getpid())
r.stamp_supervisor(remote, role)
deadline = time.time() + 120
while time.time() < deadline and not Path(quit_flag).exists():
    if mode == "honour" and r.wait_or_stop(0.5, own):
        r.clear_stop(os.getpid())
        sys.exit(0)
    if mode != "honour":
        time.sleep(0.2)
"""

PARENT = """
import subprocess, sys
kw = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
if sys.platform == "win32":
    kw["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(subprocess, "CREATE_NO_WINDOW", 0)
else:
    kw["start_new_session"] = True
subprocess.Popen([sys.executable] + sys.argv[1:], **kw)
"""


class Fleet:
    """Real supervisor stand-ins under a tmp settings home; every one exits on its own at teardown."""

    def __init__(self, home, tmp_path):
        self.home = home
        self.loop = tmp_path / "loop.py"
        self.loop.write_text(LOOP)
        self.quit_flag = tmp_path / "quit.flag"
        self.spawned = []
        self.started = 0

    def _args(self, remote, role, mode):
        return [str(self.loop), str(BIN / "unimatrix-roster.py"), role, remote, mode, str(self.quit_flag)]

    def orphan(self, remote, role, mode="honour"):
        """Start a supervisor through an intermediate parent that has already exited when this returns."""
        done = subprocess.run([sys.executable, "-c", PARENT, *self._args(remote, role, mode)], timeout=30)
        assert done.returncode == 0
        self.started += 1

    def ready(self, count):
        end = time.time() + 30
        while time.time() < end and len(self.stamps()) < count:
            time.sleep(0.1)
        assert len(self.stamps()) == count

    def stamps(self):
        out = []
        for path in sorted(self.home.glob("supervisor-*.json")):
            try:
                out.append(json.loads(path.read_text()))
            except ValueError:
                pass
        return out

    def alive(self, stamp):
        pid = stamp["pid"]
        return roster.pid_alive(pid) and roster.proc_start(pid) == stamp["procStart"]

    def live(self):
        """{(box_key, role): [pid]} over every stamped supervisor that is alive."""
        out = {}
        for stamp in self.stamps():
            if self.alive(stamp):
                key = (autolink.box_key(roster, stamp["remote"], {}), stamp["role"])
                out.setdefault(key, []).append(stamp["pid"])
        return out

    def record(self, remote, *roles):
        by_role = {s["role"]: s for s in self.stamps() if s["remote"] == remote}
        rec = {role: {"pid": by_role[role]["pid"], "procStart": by_role[role]["procStart"]} for role in roles}
        rec["box"] = autolink.box_key(roster, remote, {})
        roster.write_json_atomic(str(autolink.record_path(self.home, remote)), rec)

    def spawn(self, cmd):
        role = "bridge" if cmd[1].endswith("unimatrix-roster.py") else "inbox"
        remote = cmd[cmd.index("--remote") + 1]
        proc = subprocess.Popen([sys.executable, *self._args(remote, role, "honour")], stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.spawned.append(proc)
        return proc

    def close(self):
        self.quit_flag.write_text("")
        for proc in self.spawned:
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                pass
        end = time.time() + 15
        while time.time() < end and any(self.alive(s) for s in self.stamps()):
            time.sleep(0.1)


@pytest.fixture
def fleet(home, tmp_path, monkeypatch):
    f = Fleet(home, tmp_path)
    monkeypatch.setattr(autolink, "resolve_key", lambda *a, **k: "key")
    monkeypatch.setattr(autolink, "pinned_python", lambda *a, **k: sys.executable)
    f.link = functools.partial(autolink.link_peer, probe_fn=lambda *a, **k: None,
                               liveness=SimpleNamespace(note_probe=lambda *a, **k: None))
    yield f
    f.close()


def _two_boxes(fleet, mode_of=lambda remote, role: "honour"):
    for remote in (REMOTE, MINI):
        for role in ("bridge", "inbox"):
            fleet.orphan(remote, role, mode_of(remote, role))
    fleet.ready(4)
    fleet.record(REMOTE, "bridge")  # the machine-a inbox is stamp-only: no record names it
    fleet.record(MINI, "bridge", "inbox")


def test_restart_leaves_exactly_one_supervisor_per_remote_box(fleet, home):
    _two_boxes(fleet)
    machine_a, mini = autolink.box_key(roster, REMOTE, {}), autolink.box_key(roster, MINI, {})
    before = fleet.live()
    assert sorted(before) == [(mini, "bridge"), (mini, "inbox"), (machine_a, "bridge"), (machine_a, "inbox")]
    assert all(len(pids) == 1 for pids in before.values())
    out = []
    rc = autolink.cmd_restart_box(REMOTE, wait=15, link=fleet.link, spawn=fleet.spawn, say=out.append, roster=roster)
    assert rc == 0, out
    after = fleet.live()
    assert sorted(after) == sorted(before)
    assert all(len(pids) == 1 for pids in after.values()), after
    for role in ("bridge", "inbox"):
        assert after[(machine_a, role)] != before[(machine_a, role)]
        assert after[(mini, role)] == before[(mini, role)]
    assert not list(home.glob(f"stop-{before[(mini, 'bridge')][0]}.json"))
    assert not list(home.glob(f"stop-{before[(mini, 'inbox')][0]}.json"))
    for role in ("bridge", "inbox"):
        assert not roster.pid_alive(before[(machine_a, role)][0])


def test_a_supervisor_that_ignores_the_request_is_never_joined_by_a_replacement(fleet, home):
    _two_boxes(fleet, lambda remote, role: "ignore" if (remote, role) == (REMOTE, "bridge") else "honour")
    machine_a, mini = autolink.box_key(roster, REMOTE, {}), autolink.box_key(roster, MINI, {})
    before = fleet.live()
    out = []
    rc = autolink.cmd_restart_box(REMOTE, wait=2, link=fleet.link, spawn=fleet.spawn, say=out.append, roster=roster)
    assert rc == 1
    old_bridge = before[(machine_a, "bridge")][0]
    assert f"supervisor bridge pid {old_bridge} did not honour the stop request" in out
    after = fleet.live()
    assert after[(machine_a, "bridge")] == [old_bridge]
    assert (machine_a, "inbox") not in after
    assert fleet.spawned == []
    assert not roster.stop_request_path(old_bridge).exists()
    assert after[(mini, "bridge")] == before[(mini, "bridge")]
    assert after[(mini, "inbox")] == before[(mini, "inbox")]


def test_survivors_leave_no_pending_stop_request_for_a_late_honour(fleet, home):
    _two_boxes(fleet, lambda remote, role: "ignore" if remote == REMOTE else "honour")
    machine_a = autolink.box_key(roster, REMOTE, {})
    before = fleet.live()
    out = []
    rc = autolink.cmd_restart_box(REMOTE, wait=2, link=fleet.link, spawn=fleet.spawn, say=out.append, roster=roster)
    assert rc == 1
    for role in ("bridge", "inbox"):
        pid = before[(machine_a, role)][0]
        assert roster.pid_alive(pid)
        assert not roster.stop_request_path(pid).exists()
    assert fleet.live() == before


class _Roster:
    """The roster module with chosen attributes replaced, so a restart can be made to fail at a named step."""

    def __init__(self, **over):
        self._over = over

    def __getattr__(self, name):
        return self._over[name] if name in self._over else getattr(roster, name)


def _early_exit_fleet(fleet):
    _two_boxes(fleet)
    machine_a, mini = autolink.box_key(roster, REMOTE, {}), autolink.box_key(roster, MINI, {})
    before = fleet.live()
    peer = roster.stop_request_path(before[(mini, "bridge")][0])
    roster.request_stop(before[(mini, "bridge")][0], "peer-start", "bridge", MINI)
    return before, machine_a, peer, peer.read_bytes()


def _assert_withdrawn(before, machine_a, peer, peer_bytes, out):
    for role in ("bridge", "inbox"):
        pid = before[(machine_a, role)][0]
        assert roster.pid_alive(pid)
        assert not roster.stop_request_path(pid).exists(), out
    assert peer.read_bytes() == peer_bytes
    assert not list(peer.parent.glob("autolink-*.lock"))


def test_lock_refusal_writes_no_request_and_leaves_a_peer_request(fleet, home):
    before, machine_a, peer, peer_bytes = _early_exit_fleet(fleet)
    lock = autolink.state_dir(roster) / f"autolink-{autolink.slug(machine_a)}.lock"
    assert autolink.acquire_lock(lock)
    out = []
    rc = autolink.cmd_restart_box(REMOTE, wait=2, link=fleet.link, spawn=fleet.spawn, say=out.append, roster=roster)
    assert rc == 1 and any("mid-spawn" in line for line in out)
    for role in ("bridge", "inbox"):
        assert not roster.stop_request_path(before[(machine_a, role)][0]).exists()
    assert peer.read_bytes() == peer_bytes
    assert lock.exists()  # the refused call never owned the lock


def test_request_stop_failure_mid_loop_withdraws_the_requests_already_written(fleet, home):
    before, machine_a, peer, peer_bytes = _early_exit_fleet(fleet)
    calls = []

    def flaky(*args):
        calls.append(args)
        if len(calls) == 2:
            raise OSError("disk full")
        roster.request_stop(*args)

    out = []
    with pytest.raises(OSError):
        autolink.cmd_restart_box(REMOTE, wait=2, link=fleet.link, spawn=fleet.spawn, say=out.append,
                                 roster=_Roster(request_stop=flaky))
    assert len(calls) == 2
    _assert_withdrawn(before, machine_a, peer, peer_bytes, out)


def test_interrupt_during_the_wait_withdraws_every_request_of_this_call(fleet, home):
    before, machine_a, peer, peer_bytes = _early_exit_fleet(fleet)

    def interrupted(_seconds):
        raise KeyboardInterrupt

    out = []
    with pytest.raises(KeyboardInterrupt):
        autolink.cmd_restart_box(REMOTE, wait=2, link=fleet.link, spawn=fleet.spawn, say=out.append, roster=roster,
                                 sleep=interrupted)
    _assert_withdrawn(before, machine_a, peer, peer_bytes, out)


def test_survivor_timeout_withdraws_every_request_of_this_call(fleet, home):
    _two_boxes(fleet, lambda remote, role: "ignore" if remote == REMOTE else "honour")
    machine_a, mini = autolink.box_key(roster, REMOTE, {}), autolink.box_key(roster, MINI, {})
    before = fleet.live()
    roster.request_stop(before[(mini, "inbox")][0], "peer-start", "inbox", MINI)
    peer = roster.stop_request_path(before[(mini, "inbox")][0])
    peer_bytes = peer.read_bytes()
    out = []
    rc = autolink.cmd_restart_box(REMOTE, wait=2, link=fleet.link, spawn=fleet.spawn, say=out.append, roster=roster)
    assert rc == 1
    _assert_withdrawn(before, machine_a, peer, peer_bytes, out)


def test_a_request_already_pending_on_a_target_is_a_peers_and_survives_the_early_exit(fleet, home):
    _two_boxes(fleet, lambda remote, role: "ignore" if remote == REMOTE else "honour")
    machine_a = autolink.box_key(roster, REMOTE, {})
    before = fleet.live()
    pid = before[(machine_a, "bridge")][0]
    roster.request_stop(pid, "peer-start", "bridge", REMOTE)
    peer_bytes = roster.stop_request_path(pid).read_bytes()
    rc = autolink.cmd_restart_box(REMOTE, wait=2, link=fleet.link, spawn=fleet.spawn, say=lambda *_: None,
                                  roster=roster)
    assert rc == 1
    assert roster.stop_request_path(pid).read_bytes() == peer_bytes
    assert not roster.stop_request_path(before[(machine_a, "inbox")][0]).exists()

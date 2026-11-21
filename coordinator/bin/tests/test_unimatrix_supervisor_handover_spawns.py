"""Supervisor stop-request channel: a request addressed to (pid, procStart) is honoured by that process only."""
import functools
import importlib.util
import json
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

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

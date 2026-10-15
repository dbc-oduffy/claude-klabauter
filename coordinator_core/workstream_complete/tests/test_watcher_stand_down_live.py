"""Live acceptance of the watcher stand-down op against real watcher-shaped process trees.

Dummy group-em-watch / group-em-memory-governor scripts are launched through a trampoline parent that
exits (the orphan shape a background Bash launch leaves on Windows) and through a parent that stays
alive. Launch records come from the recorder's hook entry, never from hand-written store files. Every
tree is torn down by recorded (pid, ctime); nothing is ever killed by name.

The governor argv is the derived shape from the S0 verdict (f); no live Group EM governor was
capturable when this was written.
"""

import json
import os
import secrets
import subprocess
import sys
import time
from pathlib import Path

import pytest

from coordinator_core.bash_guards import _host_probe as hp
from coordinator_core.bash_guards import _process_kill as pk
from coordinator_core.bash_guards import _taskstop_launch_store as store
from coordinator_core.group_em import nomination
from coordinator_core.hooks import taskstop_launch_recorder as recorder
from coordinator_core.ops.watcher_stand_down import _stand_down_watchers

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

windows_only = pytest.mark.skipif(sys.platform != "win32", reason="Windows-only: orphaned launch trees")

OWN = "live-own-session"
PEER = "live-peer-session"
HOLDER_NAME = "gem-live"
BEYOND_WINDOW_S = 2.6
SELF_EXIT_S = 180

DUMMY = f"import time\nend = time.monotonic() + {SELF_EXIT_S}\nwhile time.monotonic() < end:\n    time.sleep(0.5)\n"
TRAMPOLINE = (
    "import subprocess, sys\n"
    "kw = {'stdin': subprocess.DEVNULL, 'stdout': subprocess.DEVNULL, 'stderr': subprocess.DEVNULL}\n"
    "if sys.platform == 'win32':\n"
    "    kw['creationflags'] = 0x08000000 | 0x00000008 | 0x00000200\n"
    "else:\n"
    "    kw['start_new_session'] = True\n"
    "subprocess.Popen([sys.executable] + sys.argv[1:], **kw)\n"
)
HOLDER = (
    "import subprocess, sys, time\n"
    "child = subprocess.Popen([sys.executable] + sys.argv[1:])\n"
    f"end = time.monotonic() + {SELF_EXIT_S}\n"
    "while time.monotonic() < end and child.poll() is None:\n"
    "    time.sleep(0.5)\n"
    "child.kill()\n"
)

_launched: list = []


class Tree:
    def __init__(self, task_id: str, command: str, token: str, since: int):
        self.task_id = task_id
        self.command = command
        self.token = token
        self.since = since
        self.procs: list = []


class _Noise(Exception):
    """A stranger's process sat in a launch window; attribution correctly refused. Retry fresh."""


def _tree_of(token: str, since: int) -> list:
    """Live (pid, ctime) of every process created after `since` whose command line carries token,
    plus its descendants."""
    rows = hp.snapshot() or []
    hit = {}
    for r in rows:
        if r.ctime <= since:
            continue
        text = pk.command_line(r.pid, r.ctime)
        if text and token in text:
            hit[r.pid] = (r.pid, r.ctime)
    grew = True
    while grew:
        grew = False
        for r in rows:
            if r.ppid in hit and r.pid not in hit and r.ctime >= hit[r.ppid][1]:
                hit[r.pid] = (r.pid, r.ctime)
                grew = True
    return list(hit.values())


def _newest_ctime() -> int:
    rows = hp.snapshot() or []
    return max((r.ctime for r in rows), default=0)


def _alive(procs) -> list:
    return [pc for pc in procs if hp.alive(*pc)]


def _launch(tmp: Path, script_name: str, flags: list, parent_script: str, task_id: str) -> Tree:
    """Start `python <script> <flags> <token>` under parent_script (a trampoline that exits, or a
    holder that stays). Returns once the dummy tree shows in the process table."""
    script = tmp / script_name
    script.write_text(DUMMY, encoding="utf-8")
    parent = tmp / ("trampoline.py" if parent_script is TRAMPOLINE else "holder.py")
    parent.write_text(parent_script, encoding="utf-8")
    token = f"live{secrets.randbelow(10**9):09d}"
    args = [str(script), *flags, token]
    command = " ".join([Path(sys.executable).stem.lower(), *args])
    since = _newest_ctime()
    outer = subprocess.Popen(
        [sys.executable, str(parent), *args],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    tree = Tree(task_id, command, token, since)
    _launched.append(tree)
    if parent_script is TRAMPOLINE:
        outer.wait(timeout=30)
    else:
        tree.holder = outer  # type: ignore[attr-defined]
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline and not tree.procs:
        tree.procs = [pc for pc in _tree_of(token, since) if pc != _outer_key(outer)]
        if not tree.procs:
            time.sleep(0.05)
    assert tree.procs, f"tree for {token} never appeared"
    return tree


def _outer_key(outer) -> tuple:
    ct = hp.creation_time(outer.pid) if outer.poll() is None else None
    return (outer.pid, ct)


def _record(tree: Tree, session: str) -> None:
    payload = {"payload": {
        "tool_name": "Bash", "session_id": session, "tool_use_id": "tu-" + tree.task_id,
        "tool_input": {"command": tree.command, "run_in_background": True},
        "tool_response": {"backgroundTaskId": tree.task_id},
    }}
    recorder._handler(payload)


def _call_op(repo_root: Path) -> dict:
    """Run the op handler under a spawn trap; the op must reach its verdict without any spawn."""
    def _trap(*a, **k):
        raise AssertionError("stand-down op spawned a process")

    saved = (subprocess.Popen, os.system)
    subprocess.Popen, os.system = _trap, _trap
    try:
        return _stand_down_watchers({"session_id": OWN, "repo_root": str(repo_root)})
    finally:
        subprocess.Popen, os.system = saved


def _teardown_all() -> list:
    leftover = []
    for tree in _launched:
        holder = getattr(tree, "holder", None)
        for pc in tree.procs + _tree_of(tree.token, tree.since):
            pk.terminate_verified(*pc)
        if holder is not None:
            holder.kill()
            holder.wait(timeout=10)
        leftover += _alive(tree.procs) + _tree_of(tree.token, tree.since)
    _launched.clear()
    return leftover


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "settings"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    yield
    leftover = _teardown_all()
    assert not leftover, f"teardown left test-launched processes alive: {leftover}"


def _registry(tmp_path: Path) -> None:
    sessions = tmp_path / "claude" / "sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    rows = {
        "holder": {"sessionId": OWN, "name": HOLDER_NAME, "pid": os.getpid(), "cwd": str(tmp_path)},
        "peer": {"sessionId": PEER, "name": "peer-live", "pid": os.getpid(), "cwd": str(tmp_path)},
        "unnamed": {"sessionId": "live-unnamed", "name": "", "pid": os.getpid(), "cwd": str(tmp_path)},
        "dead": {"sessionId": "live-dead", "name": "dead-live", "pid": 2**22 + 12345, "cwd": str(tmp_path)},
    }
    for key, row in rows.items():
        (sessions / f"{key}.json").write_text(json.dumps(row), encoding="utf-8")
    # The holder row would anchor descent; the registry pid is this pytest process, which is not a
    # claude image, so the op skips anchor descent rather than guessing.


def _hold(repo_root: Path) -> None:
    record = nomination._build_record(str(repo_root), OWN, HOLDER_NAME, None)
    nomination._write_claim(str(repo_root), record, None)


def _assert_standing_down(res: dict, repo_root: Path) -> None:
    assert res["stand_down"] and res["stand_down"]["ok"] is True, res["stand_down"]
    assert res["stand_down"]["exit_code"] == 0
    assert nomination.read_record(str(repo_root)) is None
    names = sorted(r["name"] for r in res["notice"]["recipients"])
    assert names == ["peer-live"], res["notice"]
    assert HOLDER_NAME in res["notice"]["message"]


def _orphan_scenario(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    _registry(tmp_path)
    _hold(repo)

    watch = _launch(tmp_path, "group-em-watch.py", ["--until-event"], TRAMPOLINE, "liveW" + secrets.token_hex(3))
    _record(watch, OWN)
    time.sleep(BEYOND_WINDOW_S)
    gov = _launch(
        tmp_path, "group-em-memory-governor.py", ["--interval", "30"], TRAMPOLINE,
        "liveG" + secrets.token_hex(3),
    )
    _record(gov, OWN)
    time.sleep(BEYOND_WINDOW_S)
    kept = _launch(
        tmp_path, "group-em-watch.py", ["--until-event"], HOLDER, "liveK" + secrets.token_hex(3)
    )
    _record(kept, OWN)
    time.sleep(BEYOND_WINDOW_S)
    peer = _launch(tmp_path, "group-em-watch.py", ["--until-event"], TRAMPOLINE, "liveP" + secrets.token_hex(3))
    _record(peer, PEER)

    for tree in (watch, gov, peer):
        if _alive(tree.procs) != tree.procs:
            raise _Noise

    res = _call_op(repo)

    noisy = [u for u in res["unattributed"] if u["task_id"] in (watch.task_id, gov.task_id) and not u["competing"]]
    if noisy:
        _teardown_all()
        raise _Noise

    stopped = {row["watcher_id"]: row for row in res["stopped"]}
    assert set(stopped) == {"group-em-watch", "group-em-memory-governor"}, res
    assert all(row["attribution"] == "launch-record" for row in res["stopped"])
    assert res["survivors"] == [], res["survivors"]
    assert "notify_when_idle" in res["expires_on_its_own"]

    for tree in (watch, gov):
        assert _alive(tree.procs) == [], f"{tree.task_id} survived"
        assert _tree_of(tree.token, tree.since) == [], f"{tree.task_id} left a child process"
        assert store.read_record(tree.task_id) is None
    killed = {tuple(k) for row in res["stopped"] for k in row["killed"]}
    assert set(watch.procs) | set(gov.procs) <= killed

    assert _alive(peer.procs) == peer.procs, "peer's identical watch must be untouched"
    assert store.read_record(peer.task_id) is not None

    assert _alive(kept.procs) == kept.procs, "a tree under a live parent is never killed"
    assert any(u["task_id"] == kept.task_id for u in res["unattributed"]), res["unattributed"]

    _assert_standing_down(res, repo)


@windows_only
def test_orphaned_watcher_trees_stopped_and_verified_gone_peer_spared(tmp_path):
    for _ in range(5):
        try:
            return _orphan_scenario(tmp_path)
        except _Noise:
            _teardown_all()
            for rec in store.all_records():
                store.delete_record(rec.task_id)
    pytest.fail("peer noise on every attempt")


def test_live_parent_tree_is_never_killed_and_holder_stands_down(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _registry(tmp_path)
    _hold(repo)
    kept = _launch(tmp_path, "group-em-watch.py", ["--until-event"], HOLDER, "liveK" + secrets.token_hex(3))
    _record(kept, OWN)

    res = _call_op(repo)

    assert res["stopped"] == [] and res["survivors"] == []
    assert _alive(kept.procs) == kept.procs
    _assert_standing_down(res, repo)

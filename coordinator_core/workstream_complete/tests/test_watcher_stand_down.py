"""Tests for the watcher stand-down op through stub snapshot, kill, store and registry seams."""

import ast
import subprocess
import sys
from pathlib import Path

import pytest

from coordinator_core.bash_guards import _taskstop_launch_store as store
from coordinator_core.bash_guards._heavy_admission_contract import ProcRow
from coordinator_core.bash_guards._taskstop_contract import FILETIME_UNIX_EPOCH, LaunchRecord
from coordinator_core.group_em import box_notice, nomination, session_registry
from coordinator_core.workstream_complete import watcher_stand_down as sd

MARK = FILETIME_UNIX_EPOCH + 10**15
S = 10_000_000
WATCH = "python bin/group-em-watch.py --until-event"
GOV = "python bin/group-em-memory-governor.py"


class World:
    """A mutable process table; the kill seam removes rows unless pinned."""

    def __init__(self, rows, cmds, immortal=()):
        self.rows = list(rows)
        self.cmds = dict(cmds)
        self.immortal = set(immortal)

    def snapshot(self):
        return list(self.rows)

    def terminate_verified(self, pid, ctime):
        if not any(r.pid == pid and r.ctime == ctime for r in self.rows):
            return False
        if pid not in self.immortal:
            self.rows = [r for r in self.rows if r.pid != pid]
        return True

    def command_line(self, pid, ctime):
        return self.cmds.get(pid)


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path))
    monkeypatch.setattr(nomination, "read_record", lambda repo_root, directory=None: None)
    monkeypatch.setattr(nomination, "box_holder", lambda *a, **k: None)
    monkeypatch.setattr(session_registry, "find_registry_row", lambda *a, **k: None)
    return tmp_path


def _install(monkeypatch, world):
    monkeypatch.setattr(sd, "_snapshot", world.snapshot)
    monkeypatch.setattr(sd, "_kill", world)


def _rec(task_id, session, command, mark=MARK):
    store.write_record(
        LaunchRecord(task_id, session, "tu", command, mark), now_s=(mark - FILETIME_UNIX_EPOCH) / 1e7
    )


def _watch_tree(base, pid):
    return (
        [ProcRow(pid, 1, base, "python.exe"), ProcRow(pid + 1, pid, base + 1, "python.exe")],
        {pid: WATCH, pid + 1: WATCH},
    )


def test_own_watch_killed_and_verified_peer_untouched(monkeypatch):
    own_rows, own_cmds = _watch_tree(MARK, 100)
    peer_rows, peer_cmds = _watch_tree(MARK + 1000 * S, 200)
    world = World(own_rows + peer_rows, {**own_cmds, **peer_cmds})
    _rec("own1", "s1", WATCH)
    _rec("peer1", "s2", WATCH, mark=MARK + 1000 * S)
    _install(monkeypatch, world)
    res = sd.stand_down_watchers("s1", "/repo")
    assert [s["watcher_id"] for s in res["stopped"]] == ["group-em-watch"]
    assert res["stopped"][0]["attribution"] == "launch-record"
    assert res["survivors"] == []
    assert {r.pid for r in world.rows} == {200, 201}
    assert store.read_record("own1") is None
    assert store.read_record("peer1") is not None
    assert res["expires_on_its_own"] == ["notify_when_idle"]


def test_ambiguous_window_is_unattributed_never_killed(monkeypatch):
    rows, cmds = _watch_tree(MARK, 100)
    world = World(rows, cmds)
    _install(monkeypatch, world)
    _rec("a", "s1", WATCH)
    _rec("b", "s1", WATCH, mark=MARK + S // 10)
    res = sd.stand_down_watchers("s1", "/repo")
    assert res["stopped"] == []
    assert {u["task_id"] for u in res["unattributed"]} == {"a", "b"}
    assert len(world.rows) == 2


def test_survivor_reported_when_kill_claims_success_but_process_remains(monkeypatch):
    rows, cmds = _watch_tree(MARK, 100)
    world = World(rows, cmds, immortal={100})
    _install(monkeypatch, world)
    _rec("own1", "s1", WATCH)
    res = sd.stand_down_watchers("s1", "/repo")
    assert res["stopped"] and 100 in [k[0] for k in res["stopped"][0]["killed"]]
    assert [s["pid"] for s in res["survivors"]] == [100]


def test_unreadable_resnapshot_reports_unverified_survivors_not_success(monkeypatch):
    rows, cmds = _watch_tree(MARK, 100)
    world = World(rows, cmds)
    _install(monkeypatch, world)
    calls = []

    def snap():
        calls.append(1)
        return world.snapshot() if len(calls) == 1 else None

    monkeypatch.setattr(sd, "_snapshot", snap)
    _rec("own1", "s1", WATCH)
    res = sd.stand_down_watchers("s1", "/repo")
    assert sorted(s["pid"] for s in res["survivors"]) == [100, 101]
    assert any("unreadable" in w for w in res["warnings"])
    assert store.read_record("own1") is not None


def test_anchor_descent_stops_governor_under_tool_shell_only(monkeypatch):
    rows = [
        ProcRow(10, 1, MARK, "claude.exe"),
        ProcRow(11, 10, MARK + 1, "bash.exe"),
        ProcRow(12, 11, MARK + 2, "python.exe"),
        ProcRow(13, 11, MARK + 3, "python.exe"),
        ProcRow(50, 1, MARK + 4, "python.exe"),
    ]
    world = World(rows, {12: GOV, 13: "python -m pytest x", 50: GOV})
    _install(monkeypatch, world)
    reg = session_registry.RegistryRow("s1", "me", 10, "/repo", "idle")
    monkeypatch.setattr(session_registry, "find_registry_row", lambda *a, **k: reg)
    res = sd.stand_down_watchers("s1", "/repo")
    assert [(s["watcher_id"], s["attribution"]) for s in res["stopped"]] == [
        ("group-em-memory-governor", "anchor-descent")
    ]
    assert {r.pid for r in world.rows} == {10, 11, 13, 50}
    assert res["survivors"] == []


def test_non_claude_registry_pid_skips_descent(monkeypatch):
    rows = [ProcRow(10, 1, MARK, "notepad.exe"), ProcRow(11, 10, MARK + 1, "bash.exe"),
            ProcRow(12, 11, MARK + 2, "python.exe")]
    world = World(rows, {12: GOV})
    _install(monkeypatch, world)
    reg = session_registry.RegistryRow("s1", "me", 10, "/repo", "idle")
    monkeypatch.setattr(session_registry, "find_registry_row", lambda *a, **k: reg)
    res = sd.stand_down_watchers("s1", "/repo")
    assert res["stopped"] == [] and len(world.rows) == 3


def _holder(monkeypatch, exit_code, record=None):
    calls = []
    monkeypatch.setattr(nomination, "read_record",
                        lambda repo_root, directory=None: {"session_id": "s1", "repo_root": repo_root})

    def fake_stand_down(repo_root, session_id, directory=None):
        calls.append((repo_root, session_id))
        return nomination.StandDownResult(exit_code == 0, exit_code, "msg", {"peer_name": "gem"})

    monkeypatch.setattr(nomination, "stand_down", fake_stand_down)
    monkeypatch.setattr(box_notice, "recipients_from_registry",
                        lambda sid, *a, **k: [{"name": "peer", "sessionId": "s9", "cwd": ""}])
    return calls


def test_holder_stood_down_and_notice_returned(monkeypatch):
    _install(monkeypatch, World([], {}))
    calls = _holder(monkeypatch, 0)
    res = sd.stand_down_watchers("s1", "/repo")
    assert calls == [("/repo", "s1")]
    assert res["stand_down"] == {"ok": True, "exit_code": 0, "message": "msg"}
    assert res["notice"]["recipients"][0]["name"] == "peer"
    assert "gem" in res["notice"]["message"] and "stood down" in res["notice"]["message"]


def test_not_holder_exit_5_empties_recipients(monkeypatch):
    _install(monkeypatch, World([], {}))
    _holder(monkeypatch, 5)
    res = sd.stand_down_watchers("s1", "/repo")
    assert res["stand_down"]["exit_code"] == 5
    assert res["notice"] == {"recipients": [], "message": ""}


def test_session_without_standing_gets_no_stand_down(monkeypatch):
    _install(monkeypatch, World([], {}))
    res = sd.stand_down_watchers("s1", "/repo")
    assert res["stand_down"] is None and res["notice"] is None


def test_box_record_found_from_another_repo(monkeypatch):
    _install(monkeypatch, World([], {}))
    calls = _holder(monkeypatch, 0)
    monkeypatch.setattr(nomination, "read_record", lambda *a, **k: None)
    monkeypatch.setattr(nomination, "box_holder", lambda *a, **k: {"session_id": "s1", "repo_root": "/other"})
    sd.stand_down_watchers("s1", "/repo")
    assert calls == [("/other", "s1")]


def test_unreadable_table_warns_and_never_claims_a_stop(monkeypatch):
    monkeypatch.setattr(sd, "_snapshot", lambda: None)
    res = sd.stand_down_watchers("s1", "/repo")
    assert res["stopped"] == [] and res["warnings"]


def test_op_rejects_bad_params():
    from coordinator_core.ops.watcher_stand_down import _stand_down_watchers

    assert "error" in _stand_down_watchers({})
    assert "error" in _stand_down_watchers({"session_id": "s", "repo_root": 3})


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_no_subprocess_or_psutil_in_op_sources():
    root = Path(sd.__file__).resolve().parents[1]
    for rel in ("workstream_complete/watcher_stand_down.py", "ops/watcher_stand_down.py"):
        tree = ast.parse((root / rel).read_text(encoding="utf-8"))
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                names.add(node.module.split(".")[0])
        assert not names & {"subprocess", "psutil"}
    probe = (
        "import sys; import coordinator_core.ops.watcher_stand_down; "
        "sys.exit(1 if 'psutil' in sys.modules else 0)"
    )
    done = subprocess.run(
        [sys.executable, "-I", "-c", f"import sys; sys.path.insert(0, {str(root.parent)!r}); {probe}"],
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert done.returncode == 0

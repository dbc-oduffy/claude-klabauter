"""Warn-not-deny corpus: each reported shape, driven through the real dispatch entry point,
must not deny and must not rewrite the command; an unbounded root find still denies.

The unbounded-root-find deny control pins the deny side of the same decision."""

from __future__ import annotations

import json

import pytest

from coordinator_core.bash_guards import dispatch
from coordinator_core.bash_guards import guard_heavy_command_admission as guard
from coordinator_core.bash_guards._heavy_admission_contract import (
    KEY_SESSION_BACKGROUND_CAP,
    ProcRow,
)
from coordinator_core.guard_messages.testing import envelope_of

BS = chr(92)

NOT_DENY = [
    pytest.param("find /e /f/dev -maxdepth 4 -iname '*x*'", id="ac1-bounded-root-find"),
    pytest.param(
        """coordinator-invoke plan.readiness_verdict '{"premise":"<evidence dir>"}'""",
        id="ac3-angle-brackets-in-json",
    ),
    pytest.param("mv a 'b<c'", id="ac3-real-write-angle-name"),
    pytest.param('python s.py "/c/Program Files/x" out.json', id="ac4-posix-read-path"),
    pytest.param(
        "cat <<'EOF'\nnotes about /c/Program Files and more\nEOF",
        id="ac4-heredoc-prose-drive-path",
    ),
    pytest.param(
        """coordinator-invoke.exe plan.readiness_verdict '{"sizing":"state/sizings/a.yaml"}'""",
        id="ac5-op-param-sizing-path",
    ),
    pytest.param(
        "cat <<'EOF'\nsee state/sizings/a.yaml for the register\nEOF",
        id="ac5-heredoc-prose-sizing-path",
    ),
]


def _results(out):
    if out is None:
        return []
    items = out if isinstance(out, list) else [out]
    return [envelope_of(i) for i in items if i]


def _run(cmd: str, tmp_path):
    payload = {
        "tool_name": "Bash",
        "tool_input": {"command": cmd},
        "session_id": "sess-warn-not-deny",
        "cwd": str(tmp_path),
    }
    return _results(dispatch.evaluate_payload_json(json.dumps(payload)))


def _denies(envs) -> bool:
    for e in envs:
        hso = e.get("hookSpecificOutput") or {}
        if hso.get("permissionDecision") == "deny" or e.get("decision") == "block":
            return True
    return False


def _rewrites(envs) -> bool:
    for e in envs:
        hso = e.get("hookSpecificOutput") or {}
        if "updatedInput" in hso or "updated_input" in e:
            return True
    return False


@pytest.mark.parametrize("cmd", NOT_DENY)
def test_reported_shape_warns_without_deny_or_rewrite(cmd, tmp_path):
    envs = _run(cmd, tmp_path)
    assert not _denies(envs), envs
    assert not _rewrites(envs), envs


def test_cd_git_show_pipeline_never_denies_and_rewrites_faithfully(tmp_path):
    envs = _run("cd sub && git show HEAD:f | head", tmp_path)
    assert not _denies(envs), envs
    rewrites = [
        e["hookSpecificOutput"]["updatedInput"]["command"]
        for e in envs
        if "updatedInput" in (e.get("hookSpecificOutput") or {})
    ]
    assert rewrites == ["git -C sub show HEAD:f | head"] or not rewrites, envs


@pytest.mark.parametrize(
    "cmd", ["find / -name x", "find /c -name x", "find / -maxdepth 999 -name x"]
)
def test_unbounded_root_find_still_denies(cmd, tmp_path):
    assert _denies(_run(cmd, tmp_path))


@pytest.fixture()
def _cap_host(tmp_path, monkeypatch):
    from coordinator_core.bash_guards import _heavy_lease_store as leases

    claude = ProcRow(pid=100, ppid=1, ctime=10, name="claude.exe")
    tool = ProcRow(pid=150, ppid=100, ctime=15, name="bash.exe")
    caller = ProcRow(pid=200, ppid=150, ctime=20, name="python.exe")
    shells = [
        ProcRow(pid=400 + i, ppid=claude.pid, ctime=40 + i, name="bash.exe") for i in range(3)
    ]
    rows = [claude, tool, caller] + shells

    class _Prims:
        def alive(self, pid, ctime):
            return True

        def creation_time(self, pid):
            return None

        def parent(self, pid):
            return None

        def image_name(self, pid):
            return None

        def snapshot(self):
            return rows

    from coordinator_core.bash_guards._heavy_admission_contract import MemoryReading

    monkeypatch.setattr(leases, "leases_dir", lambda: tmp_path / "leases")
    monkeypatch.setattr(guard, "_read_config", lambda: {KEY_SESSION_BACKGROUND_CAP: "3"})
    monkeypatch.setattr(guard, "_primitives", lambda: _Prims())
    monkeypatch.setattr(guard, "_read_available", lambda: MemoryReading(5000, True, "stub"))
    monkeypatch.setattr(guard, "_workflow_runs_of", lambda payload: [])
    monkeypatch.setattr(guard, "_read_box_hold", lambda: None)
    monkeypatch.setattr(guard, "_deny_log_path", lambda: tmp_path / "would-deny.jsonl")
    monkeypatch.setattr(guard, "_working_set_mb", lambda pid: None)
    monkeypatch.delenv("CLAUDE_PID", raising=False)


def test_session_cap_over_live_shells_warns_not_denies(_cap_host):
    payload = {
        "tool_name": "Bash",
        "tool_input": {"command": "sleep 600", "run_in_background": True},
        "session_id": "sess-heavy",
        "pid": 200,
    }
    envs = _results(guard.check(payload))
    assert not _denies(envs), envs
    assert not _rewrites(envs), envs

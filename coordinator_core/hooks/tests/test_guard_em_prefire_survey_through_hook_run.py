"""DR-147 door test: a PreToolUse event driven through coordinator/bin/hook-run.py `main` reaches
the em-prefire-survey guard and returns its deny on stdout with exit 0, with the argv DoE wires."""

from __future__ import annotations

import importlib.util
import io
import json
import os
import sys

import pytest

from coordinator_core import machine_profile, warp_fire
from coordinator_core.session import engine_route_state as ers
from coordinator_core.session import xs_lane_budget

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
HOOK_RUN_PATH = os.path.join(REPO_ROOT, "coordinator", "bin", "hook-run.py")
SID = "6f1c2a3e-8b4d-4e5f-9a60-1b2c3d4e5f60"

SEARCH_ARGV = ["hook-run", "--advisory", "hooks.preuse_search_dispatch"]
BASH_ARGV = ["hook-run", "hooks.preuse_bash_dispatch"]


def _load_hook_run():
    spec = importlib.util.spec_from_file_location("_hook_run_prefire_door", HOOK_RUN_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _line(rec: dict) -> str:
    return json.dumps(rec, separators=(",", ":")) + "\n"


def _user() -> str:
    return _line({"type": "user", "message": {"role": "user", "content": "go"}})


def _use(tid: str, name: str, inp: dict) -> str:
    return _line(
        {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "tool_use", "id": tid, "name": name, "input": inp}]}}
    )


def _read_input(i) -> dict:
    return {"file_path": f"src/f{i}.py"}


def _bash_input(i) -> dict:
    return {"command": f"ls dir{i}"}


_INPUTS = {"Read": _read_input, "Bash": _bash_input}


class Door:
    def __init__(self, tmp_path):
        self.repo = tmp_path / "repo"
        (self.repo / ".git").mkdir(parents=True)
        marker_dir = self.repo / ".git" / "coordinator-sessions" / SID
        marker_dir.mkdir(parents=True)
        (marker_dir / "em-role-asserted").write_text("1", encoding="utf-8")
        self.home = tmp_path / "sh"
        self.transcript = tmp_path / "t.jsonl"
        self.lines = [_user()]
        self.flush()

    def flush(self):
        self.transcript.write_text("".join(self.lines), encoding="utf-8")

    def history(self, n: int, name: str):
        for i in range(n):
            self.lines.append(_use(f"h{i}", name, _INPUTS[name](i)))
        self.flush()

    def event(self, name: str, inp: dict | None = None) -> dict:
        return {
            "session_id": SID,
            "transcript_path": str(self.transcript),
            "cwd": str(self.repo),
            "hook_event_name": "PreToolUse",
            "tool_name": name,
            "tool_input": inp if inp is not None else _INPUTS[name]("cur"),
            "tool_use_id": "cur",
        }

    def run(self, monkeypatch, capsys, name: str, inp: dict | None = None):
        argv = SEARCH_ARGV if name == "Read" else BASH_ARGV
        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(self.event(name, inp))))
        rc = _load_hook_run().main(list(argv))
        out = capsys.readouterr().out
        return rc, (json.loads(out) if out.strip() else {})


@pytest.fixture
def door(tmp_path, monkeypatch):
    from coordinator_core.ipc import allow_unstamped_dispatch

    allow_unstamped_dispatch()
    for mod in ("lib", "cc_invoke"):
        sys.modules.pop(mod, None)
    bin_dir = os.path.join(REPO_ROOT, "coordinator", "bin")
    for d in (bin_dir, os.path.join(bin_dir, "lib")):
        if d not in sys.path:
            sys.path.insert(0, d)
    monkeypatch.setenv("COORDINATOR_ENGINE_ROOT", REPO_ROOT)
    monkeypatch.setenv("COORDINATOR_DOOR_STDIN_MODE", "hook")
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "sh"))
    monkeypatch.delenv("COORDINATOR_HOOK_EM_PREFIRE_SURVEY_DISABLED", raising=False)
    monkeypatch.setattr(machine_profile, "guard_level", lambda name: "strict")
    monkeypatch.setattr(ers, "session_route_phase", lambda *a, **k: ers.RoutePhase(ers.PHASE_UNFIRED))
    return Door(tmp_path)


def _decision(body: dict) -> str:
    return (body.get("hookSpecificOutput") or {}).get("permissionDecision", "")


def _reason(body: dict) -> str:
    text = body["hookSpecificOutput"]["permissionDecisionReason"]
    marker = "[coordinator] "
    return text[len(marker):] if text.startswith(marker) else text


@pytest.mark.parametrize("name", ["Read", "Bash"])
def test_ninth_call_denies_through_the_door_with_fire_step(door, monkeypatch, capsys, name):
    door.history(8, name)
    rc, body = door.run(monkeypatch, capsys, name)
    assert rc == 0
    assert _decision(body) == "deny"
    assert _reason(body).startswith("[em-prefire-survey]")
    assert warp_fire.fire_step() in _reason(body)


@pytest.mark.parametrize("name", ["Read", "Bash"])
def test_eighth_call_allows_through_the_door(door, monkeypatch, capsys, name):
    door.history(7, name)
    rc, body = door.run(monkeypatch, capsys, name)
    assert rc == 0
    assert _decision(body) != "deny"


def test_exempt_path_is_not_counted(door, monkeypatch, capsys):
    door.history(8, "Read")
    rc, body = door.run(monkeypatch, capsys, "Read", {"file_path": "/r/state/warp/x/requirements.md"})
    assert rc == 0 and _decision(body) != "deny"


def test_fire_disarms(door, monkeypatch, capsys):
    door.history(8, "Read")
    door.lines.append(_use("fire", "Workflow", {"name": warp_fire.SUBMIT_STUB_NAME}))
    door.flush()
    rc, body = door.run(monkeypatch, capsys, "Read")
    assert rc == 0 and _decision(body) != "deny"


def test_open_xs_lane_disarms(door, monkeypatch, capsys):
    door.history(8, "Read")
    assert xs_lane_budget.open_xs_lane(door.repo, SID, "abc")
    rc, body = door.run(monkeypatch, capsys, "Read")
    assert rc == 0 and _decision(body) != "deny"


def test_phase_unknown_allows(door, monkeypatch, capsys):
    monkeypatch.setattr(ers, "session_route_phase", lambda *a, **k: ers.RoutePhase(ers.PHASE_UNKNOWN))
    door.history(10, "Read")
    rc, body = door.run(monkeypatch, capsys, "Read")
    assert rc == 0 and _decision(body) != "deny"

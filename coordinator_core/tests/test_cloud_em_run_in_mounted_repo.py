"""A cloud EM whose cwd is repo A drives a run held in repo B through the whole write-guard engine
and the whole bash dispatch chain. A refusal from any guard fails naming that guard's envelope; a
write into an unrelated registered repo is still refused."""

from __future__ import annotations

import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from coordinator_core import machine_resolver
from coordinator_core.ops.dispatch_emit import run_token
from coordinator_core.ops.dispatch_emit.ask_compose import _commit_stage_call
from coordinator_core.ops.dispatch_emit.terminal_commit_params_channel import (
    params_path,
    render_params_block,
)

_RUN = "ask-pin"
_SCRIPT = "scratch/warp/pin.workflow.mjs"
_SESSION = "11111111-2222-3333-4444-555555555555"
_AGENT = "deadbeef0123"
_SIZING = "state/sizings/2026-10-22-x.yaml"
_PLAN = "docs/plans/2026-10-22-p.md"
_HARNESS_HEADER = "[Workflow harness — computed task] The task text below was computed at runtime."


@pytest.fixture
def fleet(tmp_path, monkeypatch):
    from coordinator_core.bash_guards import block_reviewer_bash_outside_allowlist as roster_guard
    from coordinator_core.bash_guards import block_subagent_commit as commit_guard

    a, b, c = (tmp_path / n for n in ("A", "B", "C"))
    for repo in (a, b, c):
        (repo / ".git").mkdir(parents=True)
    monkeypatch.setattr(
        machine_resolver,
        "merged_flat_registry",
        lambda: {"repos.a": str(a), "repos.b": str(b), "repos.c": str(c)},
    )
    monkeypatch.setattr(commit_guard, "resolve_git_root", lambda cwd: str(a))
    monkeypatch.setattr(roster_guard, "is_confined_by_roster_absence", lambda effective_type: False)
    monkeypatch.setattr(commit_guard, "_resolve_subagent_identity", lambda raw, session: _AGENT)
    monkeypatch.setattr(commit_guard, "_read_backpointer_subagent_type", lambda root, agent_id: "")
    monkeypatch.setattr(commit_guard, "_git_commit_agent_may_commit", lambda *x, **k: (False, ""))
    token = run_token.mint(b, _RUN, _SCRIPT, session_id=_SESSION)
    return SimpleNamespace(a=a, b=b, c=c, token=token, tmp=tmp_path)


def _denied(out) -> bool:
    if isinstance(out, list):
        return any(_denied(o) for o in out)
    return isinstance(out, dict) and out.get("hookSpecificOutput", {}).get("permissionDecision") == "deny"


def _write(fleet, target: Path) -> object:
    from coordinator_core.write_guards import engine

    return engine.evaluate(
        {
            "tool_name": "Write",
            "tool_input": {"file_path": str(target), "content": "x"},
            "agent_id": _AGENT,
            "agent_type": "general-purpose",
            "session_id": _SESSION,
            "cwd": str(fleet.a),
        }
    )


def _bash(fleet, cmd: str, transcript: Path | None = None, agent_type: str = "general-purpose") -> object:
    from coordinator_core.bash_guards import dispatch

    payload = {
        "tool_name": "Bash",
        "tool_input": {"command": cmd},
        "session_id": _SESSION,
        "cwd": str(fleet.a),
        "agent_id": _AGENT,
        "agent_type": agent_type,
    }
    if transcript is not None:
        payload["transcript_path"] = str(transcript)
    return dispatch.evaluate_payload_json(json.dumps(payload))


def _transcript(fleet, prompt: str) -> Path:
    indented = _HARNESS_HEADER + "\n" + "\n".join("  " + ln for ln in prompt.splitlines())
    path = fleet.tmp / "proj" / "sess" / "subagents" / "workflows" / "wf_pin" / "agent-pin.jsonl"
    path.parent.mkdir(parents=True)
    record = {"type": "user", "message": {"role": "user", "content": indented}}
    path.write_text(json.dumps(record) + "\n", encoding="utf-8")
    return path


def test_run_agent_write_into_the_run_repo_is_admitted(fleet):
    out = _write(fleet, fleet.b / "docs" / "plans" / "p.md")
    assert not _denied(out), out


def test_write_into_a_third_registered_repo_is_still_denied(fleet):
    out = _write(fleet, fleet.c / "docs" / "plans" / "p.md")
    assert _denied(out), out
    assert "outside this run's repo (A)" in out["hookSpecificOutput"]["permissionDecisionReason"]


@pytest.mark.parametrize(
    "cmd",
    [
        f"coordinator-doc-new --type plan --sizing-object {_SIZING} --out {_PLAN}",
        f"python3 PLUGIN/bin/coordinator-doc-new.py --type plan --sizing-object {_SIZING} --out {_PLAN}",
        f"python3 PLUGIN/lib/plan_scaffold_markers.py {_PLAN}",
        f"python3 PLUGIN/bin/plan-spine-check.py {_PLAN}",
    ],
)
def test_plan_fill_bash_shapes_are_admitted(fleet, cmd):
    plugin = fleet.b / "coordinator"
    for rel in ("bin/coordinator-doc-new.py", "lib/plan_scaffold_markers.py", "bin/plan-spine-check.py"):
        (plugin / rel).parent.mkdir(parents=True, exist_ok=True)
        (plugin / rel).write_text("pass\n", encoding="utf-8")
    cmd = cmd.replace("PLUGIN", plugin.as_posix())
    out = _bash(fleet, cmd)
    assert not _denied(out), out


def test_composed_commit_command_is_admitted_and_params_land_in_b(fleet):
    call = _commit_stage_call(
        head="head", anchor=f"Repo root: {fleet.b.as_posix()}", repo_root=str(fleet.b),
        run_id=_RUN, script_path=_SCRIPT,
    )
    cmds = re.findall(r"'(coordinator-invoke dispatch\.land_run [^']*)'", call)
    assert len(cmds) == 1, call
    cmd = cmds[0]
    assert cmd.endswith(f"--repo {fleet.b.as_posix()}")

    params = {"script_path": _SCRIPT, "run_id": _RUN, "run_token": fleet.token, "incomplete_chunks": []}
    prompt = f"phase: commit\n\n{cmd}\n\nThe engine reads the params block below.\n\n{render_params_block(params)}\n"
    out = _bash(fleet, cmd, _transcript(fleet, prompt), "coordinator:git-commit-agent")
    assert not _denied(out), out

    landed = params_path(fleet.b, _RUN)
    assert json.loads(landed.read_text(encoding="utf-8"))["run_id"] == _RUN
    assert not params_path(fleet.a, _RUN).exists()

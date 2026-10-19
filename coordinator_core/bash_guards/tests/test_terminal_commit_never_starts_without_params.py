"""Dispatcher-level proof that the commit step never starts without its params.

Bug row state/bug-backlog/2026-10-17-terminal-commit-params-file-read-before-written.yaml:
run ask-20261017T133320-0f3e97 lost its commit because the commit agent typed
`cd <repo> && coordinator-invoke dispatch.terminal_commit --params-file <canonical>`;
block_subagent_commit refused the compound shape, guard_level warn demoted the refusal to
allow, and the params file was never materialized, so the op failed reading it.

Invariant, through the real chain (`evaluate_payload_json`, `apply_guard_level` included),
at strict/warn/off and bare / `cd &&` / `cd ;` / `mkdir -p &&` / `mkdir -p ;` shapes: the
verdict is a deny, or the canonical params file exists and equals the spawn prompt's params block.
The mkdir shapes are the trigger of run ask-20261019T195211-39a919: the sidecar clause's
`mkdir -p` was chained ahead of the commit argv."""

from __future__ import annotations

import json

import pytest

from coordinator_core import machine_profile
from coordinator_core.bash_guards import block_subagent_commit as guard
from coordinator_core.bash_guards import dispatch
from coordinator_core.ops.dispatch_emit import run_token
from coordinator_core.ops.dispatch_emit import terminal_commit_params_channel as chan

_COMMIT_AGENT = "coordinator:git-commit-agent"
_SCRIPT = "scratch/warp/a.workflow.mjs"
_RUN = "ask-1"
_LEVELS = ["strict", "warn", "off"]
_SHAPES = ["bare", "cd_and", "cd_semi", "mkdir_and", "mkdir_semi"]


@pytest.fixture
def repo(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()
    monkeypatch.setattr(guard, "resolve_git_root", lambda cwd: str(tmp_path))
    monkeypatch.setattr(guard, "_resolve_subagent_identity", lambda raw, session: "deadbeef0123")
    monkeypatch.setattr(guard, "_read_backpointer_subagent_type", lambda root, agent_id: "")
    monkeypatch.setattr(guard, "_git_commit_agent_may_commit", lambda *a, **k: (False, ""))
    return tmp_path


def _transcript(tmp_path, params: dict | None) -> str:
    d = tmp_path / "proj" / "subagents" / "workflows" / "wf_x"
    d.mkdir(parents=True, exist_ok=True)
    lines = ["Commit."]
    if params is not None:
        lines += chan.render_params_block(params).split("\n")
    prompt = "\n".join("  " + ln for ln in lines)
    f = d / "agent-a.jsonl"
    f.write_text(
        json.dumps({"type": "user", "message": {"role": "user", "content": prompt}}) + "\n",
        encoding="utf-8",
    )
    return str(f)


def _command(repo, shape: str) -> str:
    argv = f"coordinator-invoke dispatch.terminal_commit --params-file {chan.params_path(repo, _RUN).as_posix()}"
    if shape == "cd_and":
        return f"cd {repo.as_posix()} && {argv}"
    if shape == "cd_semi":
        return f"cd {repo.as_posix()}; {argv}"
    if shape == "mkdir_and":
        return f"mkdir -p {repo.as_posix()}/sidecar && {argv}"
    if shape == "mkdir_semi":
        return f"mkdir -p {repo.as_posix()}/sidecar; {argv}"
    return argv


def _verdict(repo, cmd: str, transcript: str, level: str, monkeypatch):
    monkeypatch.setenv("MACHINE_LOCAL_COORDINATOR_GUARD_LEVEL", level)
    machine_profile.reset_cache()
    payload = {
        "tool_name": "Bash",
        "tool_input": {"command": cmd},
        "session_id": "s",
        "cwd": str(repo),
        "agent_id": "deadbeef0123",
        "agent_type": _COMMIT_AGENT,
        "transcript_path": transcript,
    }
    return dispatch.evaluate_payload_json(json.dumps(payload))


def _is_deny(out) -> bool:
    if isinstance(out, list):
        return any(_is_deny(o) for o in out)
    if not isinstance(out, dict):
        return False
    hso = out.get("hookSpecificOutput") or {}
    return hso.get("permissionDecision") == "deny" or out.get("permissionDecision") == "deny"


@pytest.mark.parametrize("shape", _SHAPES)
@pytest.mark.parametrize("level", _LEVELS)
def test_a_non_deny_verdict_leaves_the_params_file_on_disk(repo, monkeypatch, level, shape):
    token = run_token.mint(repo, _RUN, _SCRIPT)
    params = {"script_path": _SCRIPT, "run_id": _RUN, "run_token": token, "incomplete_chunks": []}
    out = _verdict(repo, _command(repo, shape), _transcript(repo, params), level, monkeypatch)
    canonical = chan.params_path(repo, _RUN)
    if _is_deny(out):
        return
    assert canonical.exists(), f"{level}/{shape}: verdict {out!r} let the commit start without its params"
    assert json.loads(canonical.read_text(encoding="utf-8")) == params


@pytest.mark.parametrize("shape", _SHAPES)
@pytest.mark.parametrize("level", _LEVELS)
def test_no_params_block_never_fabricates_the_file(repo, monkeypatch, level, shape):
    """Residual named in the plan's Out of scope / Open question 1: with no params block the
    refusal is demoted at warn and off, the command runs, and no file exists. The main
    invariant is deliberately not asserted here; only that nothing is fabricated.
    The commit step can still start without its params on these legs until they are floored."""
    run_token.mint(repo, _RUN, _SCRIPT)
    _verdict(repo, _command(repo, shape), _transcript(repo, None), level, monkeypatch)
    assert not chan.params_path(repo, _RUN).exists()

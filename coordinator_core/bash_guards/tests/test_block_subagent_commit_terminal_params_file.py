"""The exact argv the run's commit stage emits (`ask_compose`: one bare
`coordinator-invoke dispatch.terminal_commit --params-file <posix path>`) passes
block_subagent_commit for the run's git-commit-agent, in both the commit and the halt-record
form; every other subagent and every deviation from that argv stays denied."""

from __future__ import annotations

import json

import pytest

from coordinator_core.bash_guards import block_subagent_commit as guard
from coordinator_core.ops.dispatch_emit import run_token
from coordinator_core.ops.dispatch_emit import terminal_commit_params_channel as chan

_COMMIT_AGENT = "coordinator:git-commit-agent"
_SCRIPT = "scratch/warp/a.workflow.mjs"
_RUN = "ask-1"


@pytest.fixture
def repo(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()
    monkeypatch.setattr(guard, "resolve_git_root", lambda cwd: str(tmp_path))
    monkeypatch.setattr(guard, "_resolve_subagent_identity", lambda raw, session: "deadbeef0123")
    monkeypatch.setattr(guard, "_read_backpointer_subagent_type", lambda root, agent_id: "")
    monkeypatch.setattr(guard, "_git_commit_agent_may_commit", lambda *a, **k: (False, ""))
    return tmp_path


def _transcript(tmp_path, params: dict) -> str:
    d = tmp_path / "proj" / "subagents" / "workflows" / "wf_x"
    d.mkdir(parents=True, exist_ok=True)
    block = chan.render_params_block(params)
    prompt = "\n".join("  " + ln for ln in ["Commit.", *block.split("\n")])
    f = d / "agent-a.jsonl"
    f.write_text(json.dumps({"type": "user", "message": {"role": "user", "content": prompt}}) + "\n", encoding="utf-8")
    return str(f)


def _emitted_argv(repo) -> str:
    return f"coordinator-invoke dispatch.terminal_commit --params-file {chan.params_path(repo, _RUN).as_posix()}"


def _check(cmd: str, transcript: str, agent_type: str = _COMMIT_AGENT):
    return guard.check(
        {
            "tool_name": "Bash",
            "tool_input": {"command": cmd},
            "session_id": "s",
            "cwd": None,
            "agent_id": "deadbeef0123",
            "agent_type": agent_type,
            "transcript_path": transcript,
        }
    )


@pytest.mark.parametrize("halted", [False, True])
def test_the_commit_stage_argv_is_admitted(repo, halted):
    token = run_token.mint(repo, _RUN, _SCRIPT)
    params = {"script_path": _SCRIPT, "run_id": _RUN, "run_token": token}
    params["halted_by"] = {"reason": "x"} if halted else None
    if not halted:
        params.pop("halted_by")
        params["incomplete_chunks"] = []
    assert _check(_emitted_argv(repo), _transcript(repo, params)) is None


@pytest.mark.parametrize("agent_type", ["coordinator:executor", "general-purpose", ""])
def test_the_same_argv_from_any_other_subagent_is_denied(repo, agent_type):
    token = run_token.mint(repo, _RUN, _SCRIPT)
    tp = _transcript(repo, {"script_path": _SCRIPT, "run_id": _RUN, "run_token": token})
    assert _check(_emitted_argv(repo), tp, agent_type=agent_type) is not None


@pytest.mark.parametrize(
    "suffix", [" ; git add -A", " && git commit -am x", " --extra"],
)
def test_a_deviation_from_the_emitted_argv_is_denied(repo, suffix):
    token = run_token.mint(repo, _RUN, _SCRIPT)
    tp = _transcript(repo, {"script_path": _SCRIPT, "run_id": _RUN, "run_token": token})
    assert _check(_emitted_argv(repo) + suffix, tp) is not None

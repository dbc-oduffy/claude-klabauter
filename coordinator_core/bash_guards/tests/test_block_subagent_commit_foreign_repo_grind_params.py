"""A queue-grind run's in-run terminal commit, in a consumer repo C other than the guard's cwd repo, passes
block_subagent_commit for the run's git-commit-agent.

The grind emit mints `kind="grind"` with no `session_id`, so `_run_root` must also resolve the run's
repo C from the token record the typed canonical path's run id names; otherwise the cwd git root E stays
the run root and the guard refuses at `leg:run-token-params-path`. A repo holding no token for the
named run stays refused. The land_run half is not the live mechanism: see test_grind_terminal_commit_run_token.py.
"""

from __future__ import annotations

import json

import pytest

from coordinator_core.bash_guards import block_subagent_commit as guard
from coordinator_core.ops.dispatch_emit import run_token
from coordinator_core.ops.dispatch_emit import terminal_commit_params_channel as chan

_COMMIT_AGENT = "coordinator:git-commit-agent"
_RUN = "20261113T025852Z"
_SESSION = "sess-commit"
_LEG = "leg:run-token-params-path"


@pytest.fixture
def repos(tmp_path, monkeypatch):
    engine, consumer = tmp_path / "engine", tmp_path / "consumer"
    for r in (engine, consumer):
        (r / ".git").mkdir(parents=True)
    monkeypatch.setattr(guard, "resolve_git_root", lambda cwd: str(engine))
    monkeypatch.setattr(guard, "_resolve_subagent_identity", lambda raw, session: "deadbeef0123")
    monkeypatch.setattr(guard, "_read_backpointer_subagent_type", lambda root, agent_id: "")
    monkeypatch.setattr(guard, "_git_commit_agent_may_commit", lambda *a, **k: (False, ""))
    return engine, consumer


def _grind_params(consumer, script_rel="scratch/lessons-custom.workflow.mjs") -> dict:
    script = (consumer / script_rel).as_posix()
    token = run_token.mint(consumer, _RUN, script, kind="grind")
    return {"script_path": script, "run_id": _RUN, "run_token": token}


def _prewrite(consumer, params: dict):
    path = chan.params_path(consumer, _RUN)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(params), encoding="utf-8")
    return path


def _transcript(tmp_path, params: dict | None) -> str:
    d = tmp_path / "proj" / "subagents" / "workflows" / "wf_x"
    d.mkdir(parents=True, exist_ok=True)
    lines = ["Commit."] + (chan.render_params_block(params).split("\n") if params else [])
    f = d / "agent-a.jsonl"
    prompt = "\n".join("  " + ln for ln in lines)
    f.write_text(json.dumps({"type": "user", "message": {"role": "user", "content": prompt}}) + "\n", encoding="utf-8")
    return str(f)


def _check(cmd: str, transcript: str, agent_type: str = _COMMIT_AGENT):
    return guard.check(
        {
            "tool_name": "Bash",
            "tool_input": {"command": cmd},
            "session_id": _SESSION,
            "cwd": None,
            "agent_id": "deadbeef0123",
            "agent_type": agent_type,
            "transcript_path": transcript,
        }
    )


def _cmd(path) -> str:
    return f"coordinator-invoke dispatch.land_run --params-file {path.as_posix()}"


def _refusal_note(path) -> str:
    note = path.with_name(chan.REFUSAL_FILE_NAME)
    return note.read_text(encoding="utf-8") if note.exists() else ""


def test_prewritten_grind_params_in_a_foreign_consumer_repo_are_admitted(repos, tmp_path):
    engine, consumer = repos
    path = _prewrite(consumer, _grind_params(consumer))
    assert _check(_cmd(path), _transcript(tmp_path, None)) is None, _refusal_note(path)
    assert _refusal_note(path) == ""


def test_prompt_carried_grind_params_in_a_foreign_consumer_repo_are_admitted(repos, tmp_path):
    _engine, consumer = repos
    params = _grind_params(consumer)
    path = chan.params_path(consumer, _RUN)
    path.parent.mkdir(parents=True, exist_ok=True)
    assert _check(_cmd(path), _transcript(tmp_path, params)) is None, _refusal_note(path)


def test_a_session_owned_ask_token_in_a_foreign_repo_stays_refused(repos, tmp_path):
    engine, consumer = repos
    script = (consumer / "scratch/a.workflow.mjs").as_posix()
    token = run_token.mint(consumer, _RUN, script, session_id="someone-else")
    path = _prewrite(consumer, {"script_path": script, "run_id": _RUN, "run_token": token})
    assert _check(_cmd(path), _transcript(tmp_path, None)) is not None


def test_a_token_minted_for_another_run_does_not_admit_this_runs_params(repos, tmp_path):
    engine, consumer = repos
    run_token.mint(consumer, "20261113T000000Z", (consumer / "scratch/o.workflow.mjs").as_posix(), kind="grind")
    params = {"script_path": "scratch/x.workflow.mjs", "run_id": _RUN, "run_token": "never-minted"}
    path = _prewrite(consumer, params)
    _check(_cmd(path), _transcript(tmp_path, None))
    assert _refusal_note(path) == guard._NOTE_GUARD_PREFIX + _LEG


def test_a_foreign_repo_with_no_token_for_the_run_stays_refused(repos, tmp_path):
    engine, consumer = repos
    params = {"script_path": "scratch/x.workflow.mjs", "run_id": _RUN, "run_token": "never-minted"}
    path = _prewrite(consumer, params)
    assert _check(_cmd(path), _transcript(tmp_path, None)) is not None


def test_the_cwd_repo_holding_the_grind_run_is_admitted(repos, tmp_path):
    engine, consumer = repos
    params = _grind_params(engine)
    path = _prewrite(engine, params)
    assert _check(_cmd(path), _transcript(tmp_path, None)) is None


def test_other_agent_types_stay_refused_for_a_foreign_grind_run(repos, tmp_path):
    engine, consumer = repos
    path = _prewrite(consumer, _grind_params(consumer))
    assert _check(_cmd(path), _transcript(tmp_path, None), agent_type="general-purpose") is not None

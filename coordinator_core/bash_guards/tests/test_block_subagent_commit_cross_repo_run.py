"""A git-commit-agent whose cwd is repo A commits a run held in repo B: the run-token leg resolves
the run root from the typed params file and the session's token, admits `--repo <B>`, and still
refuses any other repo, session or agent type."""

from __future__ import annotations

import json

import pytest

from coordinator_core.bash_guards import block_subagent_commit as guard
from coordinator_core.ops.dispatch_emit import run_token
from coordinator_core.ops.dispatch_emit import terminal_commit_params_channel as chan

_COMMIT_AGENT = "coordinator:git-commit-agent"
_SCRIPT = "scratch/warp/a.workflow.mjs"
_RUN = "ask-1"
_SESSION = "sess-1"


@pytest.fixture
def repos(tmp_path, monkeypatch):
    a, b = tmp_path / "A", tmp_path / "B"
    for r in (a, b):
        (r / ".git").mkdir(parents=True)
    monkeypatch.setattr(guard, "resolve_git_root", lambda cwd: str(a))
    monkeypatch.setattr(guard, "_resolve_subagent_identity", lambda raw, session: "deadbeef0123")
    monkeypatch.setattr(guard, "_read_backpointer_subagent_type", lambda root, agent_id: "")
    monkeypatch.setattr(guard, "_git_commit_agent_may_commit", lambda *a, **k: (False, ""))
    return a, b


def _transcript(tmp_path, params: dict) -> str:
    d = tmp_path / "proj" / "subagents" / "workflows" / "wf_x"
    d.mkdir(parents=True, exist_ok=True)
    prompt = "\n".join("  " + ln for ln in ["Commit.", *chan.render_params_block(params).split("\n")])
    f = d / "agent-a.jsonl"
    f.write_text(json.dumps({"type": "user", "message": {"role": "user", "content": prompt}}) + "\n", encoding="utf-8")
    return str(f)


def _argv(root, op="dispatch.land_run", repo=None) -> str:
    cmd = f"coordinator-invoke {op} --params-file {chan.params_path(root, _RUN).as_posix()}"
    return cmd + (f" --repo {repo.as_posix()}" if repo is not None else "")


def _check(cmd, transcript, agent_type=_COMMIT_AGENT, session=_SESSION):
    return guard.check(
        {
            "tool_name": "Bash",
            "tool_input": {"command": cmd},
            "session_id": session,
            "cwd": None,
            "agent_id": "deadbeef0123",
            "agent_type": agent_type,
            "transcript_path": transcript,
        }
    )


def _params(token):
    return {"script_path": _SCRIPT, "run_id": _RUN, "run_token": token, "incomplete_chunks": []}


@pytest.mark.parametrize("with_repo", [False, True])
def test_run_held_in_b_is_admitted_from_a(repos, tmp_path, with_repo):
    a, b = repos
    token = run_token.mint(b, _RUN, _SCRIPT, session_id=_SESSION)
    cmd = _argv(b, repo=b if with_repo else None)
    assert _check(cmd, _transcript(tmp_path, _params(token))) is None
    assert json.loads(chan.params_path(b, _RUN).read_text(encoding="utf-8"))["run_id"] == _RUN
    assert not chan.params_path(a, _RUN).exists()


def test_reconcile_rerun_after_redeem_with_only_a_spent_receipt_is_admitted(repos, tmp_path):
    a, b = repos
    token = run_token.mint(b, _RUN, _SCRIPT, session_id=_SESSION)
    assert run_token.redeem(b, _RUN, token, _SCRIPT)
    run_token.consume(b, _RUN, landed={"sha": "abc", "rows": 1, "paths": []})
    assert not run_token.session_holds_run(b, _SESSION)
    assert _check(_argv(b, repo=b), _transcript(tmp_path, _params(token))) is None


def test_repo_naming_the_cwd_repo_is_refused(repos, tmp_path):
    a, b = repos
    token = run_token.mint(b, _RUN, _SCRIPT, session_id=_SESSION)
    assert _check(_argv(b, repo=a), _transcript(tmp_path, _params(token))) is not None


def test_token_held_for_another_session_is_refused(repos, tmp_path):
    a, b = repos
    token = run_token.mint(b, _RUN, _SCRIPT, session_id="other")
    assert _check(_argv(b), _transcript(tmp_path, _params(token))) is not None


def test_params_file_in_a_repo_without_a_token_is_refused(repos, tmp_path):
    a, b = repos
    assert _check(_argv(b), _transcript(tmp_path, _params("never-minted"))) is not None
    assert not chan.params_path(b, _RUN).exists()


def test_repo_before_the_op_is_refused(repos, tmp_path):
    a, b = repos
    token = run_token.mint(b, _RUN, _SCRIPT, session_id=_SESSION)
    cmd = f"coordinator-invoke --repo {b.as_posix()} dispatch.land_run --params-file {chan.params_path(b, _RUN).as_posix()}"
    assert _check(cmd, _transcript(tmp_path, _params(token))) is not None


def test_other_agent_types_are_refused_unchanged(repos, tmp_path):
    a, b = repos
    token = run_token.mint(b, _RUN, _SCRIPT, session_id=_SESSION)
    tp = _transcript(tmp_path, _params(token))
    assert _check(_argv(b, repo=b), tp, agent_type="general-purpose") is not None


def test_same_repo_bare_form_is_still_admitted(repos, tmp_path):
    a, b = repos
    token = run_token.mint(a, _RUN, _SCRIPT, session_id=_SESSION)
    assert _check(_argv(a), _transcript(tmp_path, _params(token))) is None
    assert _check(_argv(a, repo=a), _transcript(tmp_path, _params(token))) is None

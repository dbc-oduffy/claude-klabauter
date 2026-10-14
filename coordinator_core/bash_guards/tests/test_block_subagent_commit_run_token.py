"""block_subagent_commit's run-token leg: a warp run's git-commit-agent lands its own terminal
commit; no other subagent, shape or token gets through."""

from __future__ import annotations

import json

import pytest

from coordinator_core.bash_guards import block_subagent_commit as guard
from coordinator_core.ops.dispatch_emit import run_token

_COMMIT_AGENT = "coordinator:git-commit-agent"
_SCRIPT = "scratch/warp/a.workflow.mjs"


@pytest.fixture
def repo(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()
    monkeypatch.setattr(guard, "resolve_git_root", lambda cwd: str(tmp_path))
    monkeypatch.setattr(guard, "_resolve_subagent_identity", lambda raw, session: "deadbeef0123")
    monkeypatch.setattr(guard, "_read_backpointer_subagent_type", lambda root, agent_id: "")
    monkeypatch.setattr(guard, "_git_commit_agent_may_commit", lambda *a, **k: (False, ""))
    return tmp_path


def _cmd(params: dict, *, head: str = "coordinator-invoke dispatch.terminal_commit") -> str:
    return f"{head} '{json.dumps(params)}'"


def _check(cmd: str, agent_type: str = _COMMIT_AGENT):
    return guard.check(
        {
            "tool_name": "Bash",
            "tool_input": {"command": cmd},
            "session_id": "s",
            "cwd": None,
            "agent_id": "deadbeef0123",
            "agent_type": agent_type,
        }
    )


def _params(token: str, run_id: str = "ask-1") -> dict:
    return {"script_path": _SCRIPT, "incomplete_chunks": [], "run_id": run_id, "run_token": token}


def test_minted_token_admits_the_commit_agents_terminal_commit(repo):
    token = run_token.mint(repo, "ask-1", _SCRIPT)
    assert _check(_cmd(_params(token))) is None


def test_heredoc_params_form_is_admitted(repo):
    token = run_token.mint(repo, "ask-1", _SCRIPT)
    cmd = "coordinator-invoke dispatch.terminal_commit --params-file - <<'JSON'\n" + json.dumps(_params(token)) + "\nJSON"
    assert _check(cmd) is None


@pytest.mark.parametrize("agent_type", ["coordinator:executor", "general-purpose", ""])
def test_any_other_subagent_with_the_token_is_denied(repo, agent_type):
    token = run_token.mint(repo, "ask-1", _SCRIPT)
    assert _check(_cmd(_params(token)), agent_type=agent_type) is not None


@pytest.mark.parametrize(
    "mutate",
    [
        lambda t: _cmd(_params("f" * 32)),
        lambda t: _cmd(_params(t, run_id="ask-2")),
        lambda t: _cmd({**_params(t), "script_path": "scratch/warp/other.mjs"}),
        lambda t: _cmd(_params(t)) + " ; git commit -am sweep",
        lambda t: _cmd(_params(t)) + " && git add -A",
        lambda t: "FOO=1 " + _cmd(_params(t)),
        lambda t: _cmd(_params(t), head="coordinator-invoke --repo /elsewhere dispatch.terminal_commit"),
        lambda t: _cmd(_params(t), head="coordinator-invoke ceremony.commit_v2"),
        lambda t: "bash -c " + json.dumps(_cmd(_params(t))),
    ],
)
def test_wrong_token_run_script_or_shape_is_denied(repo, mutate):
    token = run_token.mint(repo, "ask-1", _SCRIPT)
    assert _check(mutate(token)) is not None


def test_a_token_admits_one_invocation_only(repo):
    token = run_token.mint(repo, "ask-1", _SCRIPT)
    assert _check(_cmd(_params(token))) is None
    # The admit claimed the record: a concurrent second invocation, or a retry, is denied.
    assert _check(_cmd(_params(token))) is not None


def test_redeemed_token_no_longer_admits(repo):
    token = run_token.mint(repo, "ask-1", _SCRIPT)
    assert run_token.redeem(repo, "ask-1", token, _SCRIPT)
    assert _check(_cmd(_params(token))) is not None


def test_params_without_the_script_are_denied(repo):
    token = run_token.mint(repo, "ask-1", _SCRIPT)
    params = {k: v for k, v in _params(token).items() if k != "script_path"}
    assert _check(_cmd(params)) is not None


def test_expired_token_no_longer_admits(repo, monkeypatch):
    token = run_token.mint(repo, "ask-1", _SCRIPT)
    monkeypatch.setattr(run_token, "TTL_SECONDS", -1)
    assert _check(_cmd(_params(token))) is not None


def test_record_stores_no_plain_token_and_lives_in_the_git_dir(repo):
    token = run_token.mint(repo, "ask-1", _SCRIPT)
    files = list((repo / ".git").rglob("ask-1.json"))
    assert len(files) == 1 and token not in files[0].read_text(encoding="utf-8")

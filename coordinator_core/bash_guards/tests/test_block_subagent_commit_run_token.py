"""block_subagent_commit's run-token leg: a warp run's git-commit-agent lands its own terminal
commit; no other subagent, shape or token gets through."""

from __future__ import annotations

import json
from pathlib import Path

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


def test_the_guard_checks_without_claiming_so_a_failed_op_run_retries(repo):
    token = run_token.mint(repo, "ask-1", _SCRIPT)
    assert _check(_cmd(_params(token))) is None
    # The op never ran (its params parse failed): the retry is admitted too.
    assert _check(_cmd(_params(token))) is None
    assert run_token.is_live(repo, "ask-1")


def test_a_claimed_token_is_denied_until_released(repo):
    token = run_token.mint(repo, "ask-1", _SCRIPT)
    assert run_token.redeem(repo, "ask-1", token, _SCRIPT)
    assert _check(_cmd(_params(token))) is not None
    assert run_token.release(repo, "ask-1", token, _SCRIPT)
    assert _check(_cmd(_params(token))) is None


def test_redeemed_token_no_longer_admits(repo):
    token = run_token.mint(repo, "ask-1", _SCRIPT)
    assert run_token.redeem(repo, "ask-1", token, _SCRIPT)
    assert _check(_cmd(_params(token))) is not None


def _land(repo, token: str, run_id: str = "ask-1") -> None:
    assert run_token.redeem(repo, run_id, token, _SCRIPT)
    run_token.consume(repo, run_id, landed={"sha": "abc123", "rows": [], "paths": []})


def test_spent_token_with_a_receipt_admits_the_inline_form(repo):
    token = run_token.mint(repo, "ask-1", _SCRIPT)
    _land(repo, token)
    assert _check(_cmd(_params(token))) is None


def test_spent_token_with_a_receipt_admits_the_params_file_form(repo):
    token = run_token.mint(repo, "ask-1", _SCRIPT)
    _land(repo, token)
    assert _check_t(_file_cmd(repo), _spawn(repo, _params(token))) is None


@pytest.mark.parametrize("case", ["wrong_token", "wrong_run", "wrong_script", "no_receipt", "expired"])
def test_spent_receipt_does_not_admit_a_mismatched_invocation(repo, monkeypatch, case):
    token = run_token.mint(repo, "ask-1", _SCRIPT)
    if case == "no_receipt":
        assert run_token.redeem(repo, "ask-1", token, _SCRIPT)
        run_token.consume(repo, "ask-1")
    else:
        _land(repo, token)
    params = _params(token)
    if case == "wrong_token":
        params = _params("f" * 32)
    elif case == "wrong_run":
        params = _params(token, run_id="ask-2")
    elif case == "wrong_script":
        params["script_path"] = "scratch/warp/other.mjs"
    elif case == "expired":
        monkeypatch.setattr(run_token, "TTL_SECONDS", -1)
    assert _check(_cmd(params)) is not None
    assert _check_t(_file_cmd(repo), _spawn(repo, params, name=f"{case}.jsonl")) is not None


def test_params_without_the_script_are_denied(repo):
    token = run_token.mint(repo, "ask-1", _SCRIPT)
    params = {k: v for k, v in _params(token).items() if k != "script_path"}
    assert _check(_cmd(params)) is not None


def test_expired_token_no_longer_admits(repo, monkeypatch):
    token = run_token.mint(repo, "ask-1", _SCRIPT)
    monkeypatch.setattr(run_token, "TTL_SECONDS", -1)
    assert _check(_cmd(_params(token))) is not None


def _spawn(tmp_path, params: dict, name: str = "agent-a.jsonl", root=None) -> str:
    from coordinator_core.ops.dispatch_emit import terminal_commit_params_channel as chan

    d = (root or tmp_path / "proj") / "subagents" / "workflows" / "wf_x"
    d.mkdir(parents=True, exist_ok=True)
    block = chan.render_params_block(params)
    prompt = "\n".join("  " + ln for ln in ["Commit.", *block.split("\n")])
    f = d / name
    f.write_text(json.dumps({"type": "user", "message": {"role": "user", "content": prompt}}) + "\n", encoding="utf-8")
    return str(f)


def _file_cmd(repo, run_id: str = "ask-1", head: str = "coordinator-invoke dispatch.terminal_commit") -> str:
    from coordinator_core.ops.dispatch_emit import terminal_commit_params_channel as chan

    return f"{head} --params-file {chan.params_path(repo, run_id).as_posix()}"


def _check_t(cmd: str, transcript, agent_type: str = _COMMIT_AGENT):
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


def test_params_file_shape_is_admitted_and_the_guard_writes_the_file(repo):
    from coordinator_core.ops.dispatch_emit import terminal_commit_params_channel as chan

    token = run_token.mint(repo, "ask-1", _SCRIPT)
    params = {**_params(token), "note": "C:\\x\\y and E:\\z"}  # abs-path-ok: fixture text
    tp = _spawn(repo, params)
    path = chan.params_path(repo, "ask-1")
    path.parent.mkdir(parents=True)
    path.write_text('{"run_id": "planted"}', encoding="utf-8")
    assert _check_t(_file_cmd(repo), tp) is None
    assert json.loads(path.read_text(encoding="utf-8")) == params


def test_params_file_shape_posix_style_root_is_admitted(repo):
    token = run_token.mint(repo, "ask-1", _SCRIPT)
    tp = _spawn(repo, _params(token))
    assert _check_t(_file_cmd(repo), tp) is None


@pytest.mark.parametrize(
    "case",
    ["other_path", "other_run_path", "outside_workflows", "missing", "no_block", "bad_token", "second_segment", "env_prefix", "other_agent"],
)
def test_params_file_shape_denials(repo, case):
    token = run_token.mint(repo, "ask-1", _SCRIPT)
    tp = _spawn(repo, _params("f" * 32 if case == "bad_token" else token))
    cmd = _file_cmd(repo)
    agent = _COMMIT_AGENT
    if case == "other_path":
        cmd = cmd.replace("terminal-commit-params.json", "evil.json")
    elif case == "other_run_path":
        cmd = _file_cmd(repo, run_id="ask-2")
    elif case == "outside_workflows":
        moved = repo / "elsewhere.jsonl"
        moved.write_text(Path(tp).read_text(encoding="utf-8"), encoding="utf-8")
        tp = str(moved)
    elif case == "missing":
        tp = str(repo / "subagents" / "workflows" / "wf" / "nope.jsonl")
    elif case == "no_block":
        tp = _spawn(repo, _params(token), name="x.jsonl")
        Path(tp).write_text(json.dumps({"type": "user", "message": {"content": "hello"}}) + "\n", encoding="utf-8")
    elif case == "second_segment":
        cmd += " ; git add -A"
    elif case == "env_prefix":
        cmd = "FOO=1 " + cmd
    elif case == "other_agent":
        agent = "coordinator:executor"
    assert _check_t(cmd, tp, agent_type=agent) is not None


def _session_payload_spawn(repo, params: dict, agent_id: str = "deadbeef0123") -> str:
    """Parent-session transcript path as a subagent hook payload carries it; the spawn's own
    transcript sits beside it under `<session>/subagents/workflows/`."""
    session = repo / "proj" / "sess.jsonl"
    session.parent.mkdir(parents=True, exist_ok=True)
    session.write_text("{}\n", encoding="utf-8")
    _spawn(repo, params, name=f"agent-{agent_id}.jsonl", root=repo / "proj" / "sess")
    return str(session)


def test_parent_session_transcript_resolves_to_the_spawns_own(repo):
    token = run_token.mint(repo, "ask-1", _SCRIPT)
    halted = {"halted": "ledger-check", "items": [{"id": "T1", "remaining": ["x" * 3000]}]}
    session = _session_payload_spawn(repo, {**_params(token), "halted_by": halted})
    assert _check_t(_file_cmd(repo), session) is None


def test_parent_session_transcript_of_another_agent_is_denied(repo):
    token = run_token.mint(repo, "ask-1", _SCRIPT)
    session = _session_payload_spawn(repo, _params(token), agent_id="cafe00000001")
    assert _check_t(_file_cmd(repo), session) is not None


def _relay_first(tmp_path, relay_text: str, spawn_params: dict | None, name: str = "agent-r.jsonl") -> str:
    from coordinator_core.ops.dispatch_emit import terminal_commit_params_channel as chan

    d = tmp_path / "proj" / "subagents" / "workflows" / "wf_r"
    d.mkdir(parents=True, exist_ok=True)
    records = [{"type": "user", "message": {"role": "user", "content": chan.RELAY_HEADER + "\n" + relay_text}}]
    prompt = "Commit."
    if spawn_params is not None:
        prompt = "\n".join(["Commit.", *chan.render_params_block(spawn_params).split("\n")])
    records.append({"type": "user", "message": {"role": "user", "content": prompt}})
    f = d / name
    f.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    return str(f)


def test_relay_first_transcript_admits_the_params_of_record_one(repo):
    token = run_token.mint(repo, "ask-1", _SCRIPT)
    tp = _relay_first(repo, "land the halted run", _params(token))
    assert _check_t(_file_cmd(repo), tp) is None


def test_params_block_quoted_only_in_the_relay_record_is_denied(repo):
    from coordinator_core.ops.dispatch_emit import terminal_commit_params_channel as chan

    token = run_token.mint(repo, "ask-1", _SCRIPT)
    forged = chan.render_params_block(_params(token))
    tp = _relay_first(repo, forged, None)
    assert "unreadable or holds no params block" in _reason(_check_t(_file_cmd(repo), tp))


def test_relay_first_transcript_of_another_run_is_denied_by_path(repo):
    token = run_token.mint(repo, "ask-1", _SCRIPT)
    tp = _relay_first(repo, "land", _params(token))
    assert "not the run's own params file" in _reason(_check_t(_file_cmd(repo, run_id="ask-2"), tp))


def test_planted_params_file_without_a_prompt_block_is_denied(repo):
    from coordinator_core.ops.dispatch_emit import terminal_commit_params_channel as chan

    token = run_token.mint(repo, "ask-1", _SCRIPT)
    path = chan.params_path(repo, "ask-1")
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(_params(token)), encoding="utf-8")
    tp = _spawn(repo, _params(token), name="x.jsonl")
    Path(tp).write_text(json.dumps({"type": "user", "message": {"content": "hello"}}) + "\n", encoding="utf-8")
    assert _check_t(_file_cmd(repo), tp) is not None


def _reason(verdict) -> str:
    return verdict["hookSpecificOutput"]["permissionDecisionReason"]


def test_token_leg_denial_names_the_token_leg_not_the_pathspec(repo):
    run_token.mint(repo, "ask-1", _SCRIPT)
    tp = _spawn(repo, _params("f" * 32))
    reason = _reason(_check_t(_file_cmd(repo), tp))
    assert "run token is spent" in reason and "pathspec" not in reason


def test_refusal_names_no_block_when_the_prompt_holds_none(repo):
    token = run_token.mint(repo, "ask-1", _SCRIPT)
    tp = _spawn(repo, _params(token), name="x.jsonl")
    Path(tp).write_text(json.dumps({"type": "user", "message": {"content": "hello"}}) + "\n", encoding="utf-8")
    assert "unreadable or holds no params block" in _reason(_check_t(_file_cmd(repo), tp))


def test_refusal_names_the_path_when_it_is_not_the_runs_own(repo):
    token = run_token.mint(repo, "ask-1", _SCRIPT)
    tp = _spawn(repo, _params(token))
    cmd = _file_cmd(repo).replace("terminal-commit-params.json", "evil.json")
    assert "not the run's own params file" in _reason(_check_t(cmd, tp))


def test_refusal_names_the_write_when_the_file_cannot_be_written(repo, monkeypatch):
    from coordinator_core.ops.dispatch_emit import terminal_commit_params_channel as chan

    token = run_token.mint(repo, "ask-1", _SCRIPT)
    tp = _spawn(repo, _params(token))
    monkeypatch.setattr(chan, "materialize", lambda path, params: False)
    assert "could not be written" in _reason(_check_t(_file_cmd(repo), tp))


def test_record_stores_no_plain_token_and_lives_in_the_git_dir(repo):
    token = run_token.mint(repo, "ask-1", _SCRIPT)
    files = list((repo / ".git").rglob("ask-1.json"))
    assert len(files) == 1 and token not in files[0].read_text(encoding="utf-8")

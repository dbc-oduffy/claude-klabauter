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
from coordinator_core.guard_messages.testing import envelope_of


def _enveloped(check):
    return lambda *args, **kwargs: envelope_of(check(*args, **kwargs))

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
    return _enveloped(guard.check)(
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


def test_the_land_run_argv_is_admitted(repo):
    token = run_token.mint(repo, _RUN, _SCRIPT)
    tp = _transcript(repo, {"script_path": _SCRIPT, "run_id": _RUN, "run_token": token})
    argv = _emitted_argv(repo).replace("dispatch.terminal_commit", "dispatch.land_run")
    assert _check(argv, tp) is None
    assert json.loads(chan.params_path(repo, _RUN).read_text(encoding="utf-8"))["run_id"] == _RUN


def _cd(repo, sep: str) -> str:
    return f"cd {repo.as_posix()} {sep} "


@pytest.mark.parametrize("sep", ["&&", ";"])
def test_a_cd_prefixed_argv_is_denied_but_its_params_file_is_written(repo, sep):
    token = run_token.mint(repo, _RUN, _SCRIPT)
    params = {"script_path": _SCRIPT, "run_id": _RUN, "run_token": token, "incomplete_chunks": []}
    verdict = _check(_cd(repo, sep) + _emitted_argv(repo), _transcript(repo, params))
    assert verdict is not None
    assert verdict["hookSpecificOutput"]["permissionDecision"] == "deny"
    canonical = chan.params_path(repo, _RUN)
    assert json.loads(canonical.read_text(encoding="utf-8")) == params


@pytest.mark.parametrize("sep", ["&&", ";"])
def test_a_cd_prefixed_argv_with_a_missing_token_writes_nothing_and_names_the_token_leg(repo, sep):
    params = {"script_path": _SCRIPT, "run_id": _RUN, "run_token": "never-minted", "incomplete_chunks": []}
    verdict = _check(_cd(repo, sep) + _emitted_argv(repo), _transcript(repo, params))
    assert verdict is not None
    assert not chan.params_path(repo, _RUN).exists()
    assert "the run token is spent, expired or not the one minted" in (
        verdict["hookSpecificOutput"]["permissionDecisionReason"]
    )


def test_a_cd_prefixed_relative_params_path_writes_nothing(repo):
    token = run_token.mint(repo, _RUN, _SCRIPT)
    params = {"script_path": _SCRIPT, "run_id": _RUN, "run_token": token, "incomplete_chunks": []}
    cmd = f"cd {repo.as_posix()} && coordinator-invoke dispatch.terminal_commit --params-file scratch/x.json"
    assert _check(cmd, _transcript(repo, params)) is not None
    assert not chan.params_path(repo, _RUN).exists()


@pytest.mark.parametrize(
    "build",
    [
        lambda a, r: a + " && git commit -am x",
        lambda a, r: a + " ; git add -A",
        lambda a, r: "git status && " + a,
        lambda a, r: f"cd {r.as_posix()} && git status && " + a,
        lambda a, r: f"cd {r.as_posix()} extra && " + a,
    ],
)
def test_any_extra_segment_other_than_a_bare_cd_or_mkdir_p_never_writes_the_params_file(repo, build):
    token = run_token.mint(repo, _RUN, _SCRIPT)
    params = {"script_path": _SCRIPT, "run_id": _RUN, "run_token": token, "incomplete_chunks": []}
    assert _check(build(_emitted_argv(repo), repo), _transcript(repo, params)) is not None
    assert not chan.params_path(repo, _RUN).exists()


@pytest.mark.parametrize("sep", ["&&", ";"])
def test_a_mkdir_p_prefixed_argv_is_denied_but_its_params_file_is_written(repo, sep):
    token = run_token.mint(repo, _RUN, _SCRIPT)
    params = {"script_path": _SCRIPT, "run_id": _RUN, "run_token": token, "incomplete_chunks": []}
    cmd = f"mkdir -p {repo.as_posix()}/sidecar/ {sep} " + _emitted_argv(repo)
    verdict = _check(cmd, _transcript(repo, params))
    assert verdict is not None
    assert verdict["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert json.loads(chan.params_path(repo, _RUN).read_text(encoding="utf-8")) == params


@pytest.mark.parametrize(
    "prefix",
    [
        "mkdir {r}/s && ",
        "mkdir -p -m 700 {r}/s && ",
        "mkdir -p && ",
        "mkdir -pv {r}/s && ",
        "mkdir -p {r}/s -m 700 && ",
    ],
)
def test_a_malformed_mkdir_prefix_never_writes_the_params_file(repo, prefix):
    token = run_token.mint(repo, _RUN, _SCRIPT)
    params = {"script_path": _SCRIPT, "run_id": _RUN, "run_token": token, "incomplete_chunks": []}
    cmd = prefix.format(r=repo.as_posix()) + _emitted_argv(repo)
    assert _check(cmd, _transcript(repo, params)) is not None
    assert not chan.params_path(repo, _RUN).exists()


def test_a_mkdir_p_prefixed_relative_params_path_writes_nothing(repo):
    token = run_token.mint(repo, _RUN, _SCRIPT)
    params = {"script_path": _SCRIPT, "run_id": _RUN, "run_token": token, "incomplete_chunks": []}
    cmd = f"mkdir -p {repo.as_posix()}/s && coordinator-invoke dispatch.terminal_commit --params-file scratch/x.json"
    assert _check(cmd, _transcript(repo, params)) is not None
    assert not chan.params_path(repo, _RUN).exists()


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

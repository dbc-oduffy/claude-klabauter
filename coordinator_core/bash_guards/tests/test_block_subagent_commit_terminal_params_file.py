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


def _reason(verdict) -> str:
    return verdict["hookSpecificOutput"]["permissionDecisionReason"]


def _no_block_transcript(tmp_path) -> str:
    d = tmp_path / "proj2" / "subagents" / "workflows" / "wf_y"
    d.mkdir(parents=True, exist_ok=True)
    f = d / "agent-a.jsonl"
    f.write_text(json.dumps({"type": "user", "message": {"role": "user", "content": "Commit."}}) + "\n", encoding="utf-8")
    return str(f)


@pytest.mark.parametrize("verb", ["dispatch.land_run", "dispatch.terminal_commit"])
def test_an_existing_minted_params_file_is_admitted_without_a_prompt_block(repo, verb):
    token = run_token.mint(repo, _RUN, _SCRIPT)
    path = chan.params_path(repo, _RUN)
    assert chan.materialize(path, {"script_path": _SCRIPT, "run_id": _RUN, "run_token": token})
    argv = _emitted_argv(repo).replace("dispatch.terminal_commit", verb)
    assert _check(argv, _no_block_transcript(repo)) is None


def test_an_existing_params_file_with_an_unminted_token_is_refused(repo):
    path = chan.params_path(repo, _RUN)
    assert chan.materialize(path, {"script_path": _SCRIPT, "run_id": _RUN, "run_token": "never-minted"})
    verdict = _check(_emitted_argv(repo), _no_block_transcript(repo))
    assert verdict is not None
    assert "the run token is spent, expired or not the one minted" in _reason(verdict)


def test_a_remint_clears_the_prior_mints_params_file(repo):
    # e9d482 (2026-10-28): a refire re-minted while the hung first attempt's params file stayed,
    # and the refire's no-block land_run read the old token back and was refused.
    path = chan.params_path(repo, _RUN)
    assert chan.materialize(path, {"script_path": _SCRIPT, "run_id": _RUN, "run_token": run_token.mint(repo, _RUN, _SCRIPT)})
    run_token.mint(repo, _RUN, _SCRIPT)
    assert not path.exists()


def test_an_existing_minted_params_file_outside_the_canonical_path_is_refused(repo):
    token = run_token.mint(repo, _RUN, _SCRIPT)
    other = repo / "elsewhere.json"
    assert chan.materialize(other, {"script_path": _SCRIPT, "run_id": _RUN, "run_token": token})
    argv = f"coordinator-invoke dispatch.land_run --params-file {other.as_posix()}"
    assert _check(argv, _no_block_transcript(repo)) is not None


def test_a_missing_params_file_without_a_prompt_block_is_refused_naming_the_path(repo):
    path = chan.params_path(repo, _RUN)
    verdict = _check(_emitted_argv(repo), _no_block_transcript(repo))
    assert verdict is not None
    assert "missing" in _reason(verdict)
    assert path.as_posix() in _reason(verdict)


@pytest.mark.parametrize("content", ["{not json", "[]", '{"run_id": "ask-1"}'])
def test_an_unparseable_params_file_without_a_prompt_block_is_refused_naming_the_path(repo, content):
    path = chan.params_path(repo, _RUN)
    path.parent.mkdir(parents=True)
    path.write_text(content, encoding="utf-8")
    verdict = _check(_emitted_argv(repo), _no_block_transcript(repo))
    assert verdict is not None
    assert "unparseable" in _reason(verdict)
    assert path.as_posix() in _reason(verdict)


def _cross_repo(tmp_path, monkeypatch):
    cwd_repo = tmp_path / "cwd_repo"
    other = tmp_path / "other_repo"
    for r in (cwd_repo, other):
        (r / ".git").mkdir(parents=True)
    monkeypatch.setattr(guard, "resolve_git_root", lambda cwd: str(cwd_repo))
    monkeypatch.setattr(guard, "_resolve_subagent_identity", lambda raw, session: "deadbeef0123")
    monkeypatch.setattr(guard, "_read_backpointer_subagent_type", lambda root, agent_id: "")
    monkeypatch.setattr(guard, "_git_commit_agent_may_commit", lambda *a, **k: (False, ""))
    return cwd_repo, other


def _cross_argv(other) -> str:
    return (
        f"coordinator-invoke dispatch.land_run --params-file {chan.params_path(other, _RUN).as_posix()}"
        f" --repo {other.as_posix()}"
    )


def test_a_cross_repo_land_run_params_file_is_admitted_without_a_prompt_block(tmp_path, monkeypatch):
    _, other = _cross_repo(tmp_path, monkeypatch)
    token = run_token.mint(other, _RUN, _SCRIPT)
    assert chan.materialize(chan.params_path(other, _RUN), {"script_path": _SCRIPT, "run_id": _RUN, "run_token": token})
    assert _check(_cross_argv(other), _no_block_transcript(tmp_path)) is None


def test_a_cross_repo_missing_params_file_is_refused_naming_the_path(tmp_path, monkeypatch):
    _, other = _cross_repo(tmp_path, monkeypatch)
    verdict = _check(_cross_argv(other), _no_block_transcript(tmp_path))
    assert verdict is not None
    assert chan.params_path(other, _RUN).as_posix() in _reason(verdict)


def _layered_transcript(tmp_path, params: dict, *, relay: str, attachment_bytes: int) -> str:
    """Relay record, an `attachment` record (optionally over the read cap), then the computed task."""
    d = tmp_path / "proj" / "subagents" / "workflows" / "wf_x"
    d.mkdir(parents=True, exist_ok=True)
    prompt = "\n".join("  " + ln for ln in ["Commit.", *chan.render_params_block(params).split("\n")])
    records = [
        {"type": "user", "message": {"role": "user", "content": relay}},
        {"type": "attachment", "attachment": {"type": "instructions", "content": "x" * attachment_bytes}},
        {"type": "user", "message": {"role": "user", "content": prompt}},
    ]
    f = d / "agent-a.jsonl"
    f.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
    return str(f)


@pytest.mark.parametrize("attachment_bytes", [30_000, chan.READ_CAP_BYTES + 5_000])
def test_the_halt_commit_land_run_argv_is_admitted_behind_an_attachment_record(repo, attachment_bytes):
    token = run_token.mint(repo, _RUN, _SCRIPT)
    params = {"script_path": _SCRIPT, "run_id": _RUN, "run_token": token, "halted_by": {"reason": "x"}}
    tp = _layered_transcript(repo, params, relay=f"{chan.RELAY_HEADER} relayed", attachment_bytes=attachment_bytes)
    argv = _emitted_argv(repo).replace("dispatch.terminal_commit", "dispatch.land_run") + f" --repo {repo.as_posix()}"
    assert _check(argv, tp) is None
    assert json.loads(chan.params_path(repo, _RUN).read_text(encoding="utf-8"))["run_id"] == _RUN


def test_a_params_block_quoted_only_in_the_relay_record_is_still_refused(repo):
    token = run_token.mint(repo, _RUN, _SCRIPT)
    params = {"script_path": _SCRIPT, "run_id": _RUN, "run_token": token}
    d = repo / "proj" / "subagents" / "workflows" / "wf_x"
    d.mkdir(parents=True)
    relay = f"{chan.RELAY_HEADER}\n" + chan.render_params_block(params)
    f = d / "agent-a.jsonl"
    f.write_text(
        json.dumps({"type": "user", "message": {"content": relay}}) + "\n"
        + json.dumps({"type": "attachment", "attachment": {}}) + "\n",
        encoding="utf-8",
    )
    assert _check(_emitted_argv(repo), str(f)) is not None
    assert not chan.params_path(repo, _RUN).exists()


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


def _refused_note(repo):
    return chan.params_path(repo, _RUN).with_name(chan.REFUSAL_FILE_NAME)


def _land_run_argv(repo) -> str:
    return _emitted_argv(repo).replace("dispatch.terminal_commit", "dispatch.land_run") + f" --repo {repo.as_posix()}"


def test_a_token_refusal_leaves_a_note_the_missing_file_diagnosis_names(repo):
    # 272860: the refusal was demoted to allow and its text dropped, so the op could only say
    # "never written" with no cause.
    params = {"script_path": _SCRIPT, "run_id": _RUN, "run_token": "0" * 32, "halted_by": {"reason": "x"}}
    path = chan.params_path(repo, _RUN)
    path.parent.mkdir(parents=True)
    assert _check(_land_run_argv(repo), _transcript(repo, params)) is not None
    assert not path.exists()
    assert _refused_note(repo).read_text(encoding="utf-8") == guard._LEG_PARAMS_TOKEN
    assert guard._LEG_PARAMS_TOKEN in chan.missing_file_diagnosis(str(path))


def test_a_halt_commit_whose_live_token_was_consumed_mid_run_is_refused_and_noted(repo):
    # 272860 (2026-10-29): both spawn prompts carried a well-formed params block and the typed
    # path was canonical; the only refusal left was the token leg, because a peer
    # (`warp_live._record_live` ended-evidence consume) had spent the live record while the
    # run was still working.
    token = run_token.mint(repo, _RUN, _SCRIPT)
    params = {"script_path": _SCRIPT, "run_id": _RUN, "run_token": token, "halted_by": {"reason": "x"}}
    path = chan.params_path(repo, _RUN)
    path.parent.mkdir(parents=True)
    tp = _transcript(repo, params)
    run_token.consume(repo, _RUN)
    assert _check(_land_run_argv(repo), tp) is not None
    assert not path.exists()
    assert _refused_note(repo).read_text(encoding="utf-8") == guard._LEG_PARAMS_TOKEN
    run_token.mint(repo, _RUN, _SCRIPT)
    assert _check(_land_run_argv(repo), tp) is not None


def test_an_admit_clears_the_note_a_prior_refusal_left(repo):
    path = chan.params_path(repo, _RUN)
    path.parent.mkdir(parents=True)
    bad = {"script_path": _SCRIPT, "run_id": _RUN, "run_token": "0" * 32}
    assert _check(_land_run_argv(repo), _transcript(repo, bad)) is not None
    assert _refused_note(repo).exists()
    good = {**bad, "run_token": run_token.mint(repo, _RUN, _SCRIPT), "halted_by": {"reason": "x"}}
    assert _check(_land_run_argv(repo), _transcript(repo, good)) is None
    assert not _refused_note(repo).exists()
    assert json.loads(path.read_text(encoding="utf-8")) == good


def test_a_refusal_off_the_canonical_path_leaves_no_note(repo):
    other = repo / "elsewhere" / "terminal-commit-params.json"
    other.parent.mkdir()
    tp = _transcript(repo, {"script_path": _SCRIPT, "run_id": _RUN, "run_token": "0" * 32})
    argv = f"coordinator-invoke dispatch.land_run --params-file {other.as_posix()}"
    assert _check(argv, tp) is not None
    assert not (other.parent / chan.REFUSAL_FILE_NAME).exists()


def test_the_diagnosis_without_a_note_keeps_the_generic_cause(repo):
    text = chan.missing_file_diagnosis(str(chan.params_path(repo, _RUN)))
    assert "never written" in text and "did not run or refused" in text

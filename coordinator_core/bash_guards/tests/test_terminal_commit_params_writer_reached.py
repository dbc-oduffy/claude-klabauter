"""Every git-commit-agent `dispatch.terminal_commit --params-file` Bash call either materializes
the run's params file or leaves a refusal note naming `block_subagent_commit` and its leg, which the
op's never-written error then quotes. One case per payload shape that can skip the writer."""

from __future__ import annotations

import json

import pytest

pytestmark = pytest.mark.complete

from coordinator_core.bash_guards import block_subagent_commit as guard
from coordinator_core.guard_messages.testing import envelope_of
from coordinator_core.ops.dispatch_emit import run_token
from coordinator_core.ops.dispatch_emit import terminal_commit_params_channel as chan

_COMMIT_AGENT = "coordinator:git-commit-agent"
_SCRIPT = "scratch/warp/a.workflow.mjs"
_RUN = "ask-1"
_GUARD = "block_subagent_commit"


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
    prompt = "\n".join("  " + ln for ln in ["Commit.", *chan.render_params_block(params).split("\n")])
    f = d / "agent-a.jsonl"
    f.write_text(json.dumps({"type": "user", "message": {"role": "user", "content": prompt}}) + "\n", encoding="utf-8")
    return str(f)


def _argv(repo, tail: str = "") -> str:
    return f"coordinator-invoke dispatch.terminal_commit --params-file {chan.params_path(repo, _RUN).as_posix()}{tail}"


def _params(repo) -> dict:
    return {"script_path": _SCRIPT, "run_id": _RUN, "run_token": run_token.mint(repo, _RUN, _SCRIPT)}


def _call(cmd: str, transcript: str, agent_type: str = _COMMIT_AGENT):
    return envelope_of(
        guard.check(
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
    )


def _assert_written_or_named(repo, params: dict) -> None:
    """The file holds the prompt's params, or the note names this guard and a leg and the op quotes it."""
    path = chan.params_path(repo, _RUN)
    if path.exists():
        assert json.loads(path.read_text(encoding="utf-8")) == params
        return
    note = path.with_name(chan.REFUSAL_FILE_NAME)
    assert note.exists(), "params file not written and no refusal note left"
    leg = note.read_text(encoding="utf-8")
    assert _GUARD in leg and "leg:" in leg
    assert leg.strip() in chan.missing_file_diagnosis(str(path))


def test_agent_type_present_writes_the_params_file(repo):
    params = _params(repo)
    assert _call(_argv(repo), _transcript(repo, params)) is None
    _assert_written_or_named(repo, params)
    assert chan.params_path(repo, _RUN).exists()


def test_a_backpointer_only_git_commit_agent_reaches_the_writer(repo, monkeypatch):
    monkeypatch.setattr(guard, "_read_backpointer_subagent_type", lambda root, agent_id: _COMMIT_AGENT)
    params = _params(repo)
    _call(_argv(repo), _transcript(repo, params), agent_type="")
    _assert_written_or_named(repo, params)


def test_a_backpointer_only_git_commit_agent_is_still_not_admitted_by_the_raw_type_path(repo, monkeypatch):
    monkeypatch.setattr(guard, "_read_backpointer_subagent_type", lambda root, agent_id: _COMMIT_AGENT)
    monkeypatch.setattr(guard, "_git_commit_agent_may_commit", lambda *a, **k: pytest.fail("raw-type admit entered"))
    params = _params(repo)
    assert _call(_argv(repo), _transcript(repo, params), agent_type="") is not None


@pytest.mark.parametrize(
    "leg",
    [
        guard._LEG_SKIP_NOT_SUBAGENT,
        guard._LEG_SKIP_NO_GIT_ROOT,
        guard._LEG_SKIP_PAYLOAD_SHAPE,
        guard._LEG_SKIP_COMMAND_SHAPE,
    ],
)
def test_every_skip_leg_is_noted_beside_the_params_file(repo, leg):
    chan.params_path(repo, _RUN).parent.mkdir(parents=True, exist_ok=True)
    guard._note_params_writer_skipped(_argv(repo), str(repo), leg)
    note = chan.params_path(repo, _RUN).with_name(chan.REFUSAL_FILE_NAME)
    assert note.read_text(encoding="utf-8") == _GUARD + ": " + leg


def test_an_unresolved_git_root_leaves_a_note_naming_the_guard_and_leg(repo, monkeypatch):
    monkeypatch.setattr(guard, "resolve_git_root", lambda cwd: None)
    params = _params(repo)
    chan.params_path(repo, _RUN).parent.mkdir(parents=True, exist_ok=True)
    _call(_argv(repo), _transcript(repo, params))
    _assert_written_or_named(repo, params)


def test_a_relative_params_path_from_below_the_root_writes_or_names_the_leg(repo, monkeypatch):
    sub = repo / "sub"
    sub.mkdir()
    monkeypatch.chdir(sub)
    params = _params(repo)
    chan.params_path(repo, _RUN).parent.mkdir(parents=True, exist_ok=True)
    cmd = f"coordinator-invoke dispatch.terminal_commit --params-file {chan.RUN_DIR_ROOT}/{_RUN}/{chan.PARAMS_FILE_NAME}"
    _call(cmd, _transcript(repo, params))
    _assert_written_or_named(repo, params)


def test_a_trailing_stderr_redirect_still_writes_or_names_the_leg(repo):
    params = _params(repo)
    assert _call(_argv(repo, " 2>&1"), _transcript(repo, params)) is not None
    _assert_written_or_named(repo, params)

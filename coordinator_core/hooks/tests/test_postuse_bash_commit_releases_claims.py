"""Tests for R03 (docs/plans/2026-09-26-inbox-blitz-claude-klabauter-fixes-fyi-rest.md):
a sanctioned bash `git commit -- <paths>` PostToolUse(Bash) event releases the
committing session's claim on the named paths via
`_release_claims_on_bash_commit_sync`.

Fixture shape (pinned per the row's Step 1, revised by IBMFR review
2026-09-26): a real PostToolUse(Bash) event's `_handler` params carry seven
fields (session_id, transcript_path, agent_id, tool_name, file_path, content,
command -- see `test_handler_reads_no_params_field_beyond_the_seven_mapped_fields`
in test_postuse_advisory_dispatch.py, a contract test outside this row's own
writes scope), so this leg reads the Bash command directly from
params["command"] (`tool_input.command`, forwarded by the harness) rather
than re-deriving it from the transcript tail.

Claude Code's own Bash tool result carries no numeric exit code (`{stdout,
stderr, interrupted, isImage}`) -- there is no exit-status field available
from the payload today, so `_bash_commit_landed` is exercised here primarily
via its git-state fallback; the explicit-exit-code branch is forward defense
only, pinned directly against the function rather than via a payload field
that does not exist in production.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from coordinator_core.hooks import postuse_advisory_dispatch as pad  # noqa: E402

SESSION = "sess-r03-bash-commit"


def _init_repo(repo_root: Path) -> None:
    import subprocess

    subprocess.run(["git", "init", "-q"], cwd=str(repo_root), check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=str(repo_root),
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"], cwd=str(repo_root), check=True
    )


# ---------------------------------------------------------------------------
# _extract_git_commit_pathspec
# ---------------------------------------------------------------------------


def test_extract_pathspec_from_commit_with_trailing_dashdash():
    paths = pad._extract_git_commit_pathspec(
        'git commit -m "msg" -- foo.py bar.py'
    )
    assert paths == ["foo.py", "bar.py"]


def test_extract_pathspec_none_when_no_dashdash():
    assert pad._extract_git_commit_pathspec('git commit -m "msg"') is None


def test_extract_pathspec_none_when_not_a_commit():
    assert pad._extract_git_commit_pathspec("git status") is None


def test_extract_pathspec_none_on_empty_dashdash():
    # `git commit -- ` with nothing after is git's own "no pathspec given"
    # form, not a scoped commit -- see _extract_commit_trailing_pathspecs's
    # own docstring for why this must stay not-applicable.
    assert pad._extract_git_commit_pathspec('git commit -m "msg" --') is None


# ---------------------------------------------------------------------------
# _bash_commit_landed
# ---------------------------------------------------------------------------


def test_landed_via_explicit_exit_code_zero(tmp_path):
    assert pad._bash_commit_landed("0", ["foo.py"], str(tmp_path)) is True


def test_not_landed_via_explicit_exit_code_nonzero(tmp_path):
    assert pad._bash_commit_landed("1", ["foo.py"], str(tmp_path)) is False


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_landed_via_git_state_fallback_when_committed(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    target = repo / "foo.py"
    target.write_text("x = 1\n", encoding="utf-8")
    import subprocess

    subprocess.run(["git", "add", "foo.py"], cwd=str(repo), check=True)
    subprocess.run(
        ["git", "commit", "-m", "msg", "--", "foo.py"], cwd=str(repo), check=True
    )
    assert pad._bash_commit_landed("", ["foo.py"], str(repo)) is True


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_not_landed_via_git_state_fallback_when_still_staged(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    target = repo / "foo.py"
    target.write_text("x = 1\n", encoding="utf-8")
    import subprocess

    subprocess.run(["git", "add", "foo.py"], cwd=str(repo), check=True)
    # No commit landed -- the path is still staged (diverges from HEAD).
    assert pad._bash_commit_landed("", ["foo.py"], str(repo)) is False


def test_not_landed_via_git_state_fallback_with_no_paths(tmp_path):
    assert pad._bash_commit_landed("", [], str(tmp_path)) is False


# ---------------------------------------------------------------------------
# _release_claims_on_bash_commit_sync
# ---------------------------------------------------------------------------


def test_landed_commit_releases_claim(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(
        "coordinator_core.session.scope.release_committed_claims",
        lambda sid, paths, cwd=None: calls.append((sid, paths, cwd)),
    )
    monkeypatch.setattr(pad, "_bash_commit_landed", lambda *a, **k: True)
    pad._release_claims_on_bash_commit_sync(
        SESSION, "Bash", 'git commit -m "msg" -- foo.py bar.py', str(tmp_path)
    )
    assert calls == [(SESSION, ["foo.py", "bar.py"], str(tmp_path))]


def test_failed_commit_releases_nothing(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(
        "coordinator_core.session.scope.release_committed_claims",
        lambda sid, paths, cwd=None: calls.append((sid, paths, cwd)),
    )
    monkeypatch.setattr(pad, "_bash_commit_landed", lambda *a, **k: False)
    pad._release_claims_on_bash_commit_sync(
        SESSION, "Bash", 'git commit -m "msg" -- foo.py', str(tmp_path)
    )
    assert calls == []


def test_non_commit_command_spawns_nothing_and_releases_nothing(
    tmp_path, monkeypatch
):
    calls = []
    monkeypatch.setattr(
        "coordinator_core.session.scope.release_committed_claims",
        lambda sid, paths, cwd=None: calls.append((sid, paths, cwd)),
    )
    spawns = []
    monkeypatch.setattr("subprocess.run", lambda *a, **k: spawns.append((a, k)))
    pad._release_claims_on_bash_commit_sync(SESSION, "Bash", "ls -la", None)
    assert calls == []
    assert spawns == []


def test_non_bash_tool_name_is_a_no_op(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(
        "coordinator_core.session.scope.release_committed_claims",
        lambda sid, paths, cwd=None: calls.append((sid, paths, cwd)),
    )
    pad._release_claims_on_bash_commit_sync(
        SESSION, "Write", 'git commit -m "msg" -- foo.py', None
    )
    assert calls == []


def test_no_session_id_is_a_no_op(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(
        "coordinator_core.session.scope.release_committed_claims",
        lambda sid, paths, cwd=None: calls.append((sid, paths, cwd)),
    )
    pad._release_claims_on_bash_commit_sync(
        "", "Bash", 'git commit -m "msg" -- foo.py', None
    )
    assert calls == []


def test_no_command_is_a_no_op(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "coordinator_core.session.scope.release_committed_claims",
        lambda sid, paths, cwd=None: calls.append((sid, paths, cwd)),
    )
    pad._release_claims_on_bash_commit_sync(SESSION, "Bash", "", None)
    assert calls == []


def test_no_dashdash_pathspec_is_a_no_op_no_spawn(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(
        "coordinator_core.session.scope.release_committed_claims",
        lambda sid, paths, cwd=None: calls.append((sid, paths, cwd)),
    )
    spawns = []
    monkeypatch.setattr("subprocess.run", lambda *a, **k: spawns.append((a, k)))
    pad._release_claims_on_bash_commit_sync(
        SESSION, "Bash", 'git commit -m "msg"', None
    )
    assert calls == []
    assert spawns == []


def test_release_error_fails_open(tmp_path, monkeypatch):
    def _boom(sid, paths, cwd=None):
        raise RuntimeError("boom")

    monkeypatch.setattr(
        "coordinator_core.session.scope.release_committed_claims", _boom
    )
    monkeypatch.setattr(pad, "_bash_commit_landed", lambda *a, **k: True)
    # Must not raise.
    pad._release_claims_on_bash_commit_sync(
        SESSION, "Bash", 'git commit -m "msg" -- foo.py', str(tmp_path)
    )


# ---------------------------------------------------------------------------
# Handler-level wiring: fixture PostToolUse(Bash) event shape (Step 1,
# revised) -- the seven params fields `_handler` ever reads, `command`
# included.
# ---------------------------------------------------------------------------


def _landed_event(command: str) -> dict:
    return {
        "session_id": SESSION,
        "tool_name": "Bash",
        "command": command,
    }


def test_handler_calls_release_on_landed_commit_event(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(
        "coordinator_core.session.scope.release_committed_claims",
        lambda sid, paths, cwd=None: calls.append((sid, paths, cwd)),
    )
    # Silence every other universal leg so this test pins only the leg under
    # test's own wiring through _handler, not the other checks' own
    # behaviour.
    monkeypatch.setattr(pad, "_check_context_pressure_sync", lambda *a, **k: "")
    monkeypatch.setattr(pad, "_check_runtime_tripwire_sync", lambda *a, **k: "")
    monkeypatch.setattr(pad, "_check_first_agent_dispatch_sync", lambda *a, **k: "")
    monkeypatch.setattr(pad, "_check_group_em_watch_arm_sync", lambda *a, **k: "")
    monkeypatch.setattr(pad, "_capture_workflow_run_record_sync", lambda *a, **k: None)
    monkeypatch.setattr(pad, "_bash_commit_landed", lambda *a, **k: True)

    import asyncio

    result = asyncio.run(
        pad._handler(_landed_event('git commit -m "msg" -- foo.py'))
    )
    assert calls == [(SESSION, ["foo.py"], None)]
    # Bookkeeping-only leg -- never surfaces as advisory text.
    assert result == pad.no_advisory()


def test_handler_releases_nothing_on_failed_commit_event(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(
        "coordinator_core.session.scope.release_committed_claims",
        lambda sid, paths, cwd=None: calls.append((sid, paths, cwd)),
    )
    monkeypatch.setattr(pad, "_check_context_pressure_sync", lambda *a, **k: "")
    monkeypatch.setattr(pad, "_check_runtime_tripwire_sync", lambda *a, **k: "")
    monkeypatch.setattr(pad, "_check_first_agent_dispatch_sync", lambda *a, **k: "")
    monkeypatch.setattr(pad, "_check_group_em_watch_arm_sync", lambda *a, **k: "")
    monkeypatch.setattr(pad, "_capture_workflow_run_record_sync", lambda *a, **k: None)
    monkeypatch.setattr(pad, "_bash_commit_landed", lambda *a, **k: False)

    import asyncio

    asyncio.run(pad._handler(_landed_event('git commit -m "msg" -- foo.py')))
    assert calls == []


def _doe_wrapper_mapped_event(tool_name: str, command: str | None = None) -> dict:
    """The params shape `postuse-advisory-dispatch.py` (DoE-claude) actually
    builds today, NOT the seven-field fixture assumed above.

    Verified against DoE-claude coordinator/hooks/scripts/postuse-advisory-
    dispatch.py (2026-09-26): it maps session_id/transcript_path/agent_id/
    tool_name unconditionally, `file_path`/`content` ONLY when
    `tool_name == "Write"`, and `tool_response` ONLY when
    `tool_name == "Workflow"` -- there is no `command` mapping for any
    tool_name, Bash included. `command` here is accepted only to let a
    caller probe "what if it were mapped" against the same real-shape
    fixture; production omits the key entirely.
    """
    event = {
        "session_id": SESSION,
        "transcript_path": "",
        "agent_id": "",
        "tool_name": tool_name,
    }
    if command is not None:
        event["command"] = command
    return event


def test_real_doe_wrapper_shape_never_forwards_command_for_bash(monkeypatch):
    """Pins the gap the wrapper trace found: DoE's dispatcher never maps
    `tool_input.command` into params for ANY tool_name, so under the actual
    production params shape this leg's `command = field(params, "command")`
    always reads "" for a Bash event and the leg no-ops on `not command`,
    regardless of `_bash_commit_landed`/`release_committed_claims` --
    the claim-release leg is currently dormant, not merely untested.

    Also worth naming (not asserted here, since it is a matcher fact, not a
    params-mapping one): DoE-claude's hooks.json registers this dispatcher
    under matcher `Write|Edit|MultiEdit|NotebookEdit|Agent|Workflow` --
    `Bash` is not in that set, so in production the hook process is never
    even invoked on a Bash PostToolUse event in the first place.
    """
    calls = []
    monkeypatch.setattr(
        "coordinator_core.session.scope.release_committed_claims",
        lambda sid, paths, cwd=None: calls.append((sid, paths, cwd)),
    )
    monkeypatch.setattr(pad, "_check_context_pressure_sync", lambda *a, **k: "")
    monkeypatch.setattr(pad, "_check_runtime_tripwire_sync", lambda *a, **k: "")
    monkeypatch.setattr(pad, "_check_first_agent_dispatch_sync", lambda *a, **k: "")
    monkeypatch.setattr(pad, "_check_group_em_watch_arm_sync", lambda *a, **k: "")
    monkeypatch.setattr(pad, "_capture_workflow_run_record_sync", lambda *a, **k: None)
    monkeypatch.setattr(pad, "_bash_commit_landed", lambda *a, **k: True)

    import asyncio

    asyncio.run(pad._handler(_doe_wrapper_mapped_event("Bash")))
    assert calls == []

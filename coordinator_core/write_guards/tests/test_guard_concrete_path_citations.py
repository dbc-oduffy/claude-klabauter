from __future__ import annotations

import subprocess
import tempfile
import uuid
from pathlib import Path

import pytest

from coordinator_core.write_guards.guard_concrete_path_citations import CLASS, check
from coordinator_core.win_portability import no_console_passthrough_kwargs

pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]


def _sid() -> str:
    return f"test-session-{uuid.uuid4().hex}"


def _write_payload(target: str, content: str, session_id: str | None = None) -> dict:
    return {
        "tool_name": "Write",
        "tool_input": {"file_path": target, "content": content},
        "session_id": session_id or _sid(),
    }


def _edit_payload(target: str, old_string: str, new_string: str, session_id: str | None = None) -> dict:
    return {
        "tool_name": "Edit",
        "tool_input": {
            "file_path": target,
            "old_string": old_string,
            "new_string": new_string,
        },
        "session_id": session_id or _sid(),
    }


@pytest.fixture()
def tmp_target():
    with tempfile.TemporaryDirectory() as d:
        yield str(Path(d) / "doc.md")


@pytest.fixture()
def repo_root():
    with tempfile.TemporaryDirectory() as d:
        subprocess.run(["git", "init", "-q", d], check=True, **no_console_passthrough_kwargs())
        yield Path(d)


def test_loud_tier_new_write_introducing_a_concrete_path_is_advisory(repo_root: Path) -> None:
    target = repo_root / "coordinator" / "skills" / "doc.md"
    target.parent.mkdir(parents=True)
    offending = "the repo lives at " + "X:" + r"\example-game-workbench-repo"
    out = check(_write_payload(str(target), offending))
    assert out is not None
    assert "permissionDecision" not in out["hookSpecificOutput"]
    context = out["hookSpecificOutput"]["additionalContext"]
    assert context
    assert "concrete-path-citation guard" in context
    assert "fix-concrete-path-citations" in context
    assert "coordinator/bin/" not in context


def test_loud_tier_fires_every_time_no_session_memoization(repo_root: Path) -> None:
    target = repo_root / "coordinator" / "agents" / "one.md"
    target.parent.mkdir(parents=True)
    offending = "the repo lives at " + "X:" + r"\example-game-workbench-repo"
    first = check(_write_payload(str(target), offending, session_id="same-session"))
    second = check(_write_payload(str(target), offending, session_id="same-session"))
    assert first is not None and "additionalContext" in first["hookSpecificOutput"]
    assert second is not None and "additionalContext" in second["hookSpecificOutput"]
    assert "permissionDecision" not in first["hookSpecificOutput"]
    assert "permissionDecision" not in second["hookSpecificOutput"]


def test_loud_tier_by_extension_outside_the_named_directories(repo_root: Path) -> None:
    target = repo_root / "some" / "random" / "script.py"
    target.parent.mkdir(parents=True)
    offending = "PATH = '" + "X:" + r"\example-game-workbench-repo" + "'"
    first = check(_write_payload(str(target), offending, session_id="ext-session"))
    second = check(_write_payload(str(target), offending, session_id="ext-session"))
    assert first is not None and "permissionDecision" not in first["hookSpecificOutput"]
    assert second is not None and "permissionDecision" not in second["hookSpecificOutput"]


def test_root_claude_md_is_loud_tier(repo_root: Path) -> None:
    target = repo_root / "CLAUDE.md"
    offending = "see " + "X:" + r"\example-game-workbench-repo" + " for details"
    first = check(_write_payload(str(target), offending, session_id="claude-md-session"))
    second = check(_write_payload(str(target), offending, session_id="claude-md-session"))
    assert first is not None and "permissionDecision" not in first["hookSpecificOutput"]
    assert second is not None and "permissionDecision" not in second["hookSpecificOutput"]


def test_local_md_anywhere_is_loud_tier(repo_root: Path) -> None:
    target = repo_root / "nested" / "dir" / "coordinator.local.md"
    target.parent.mkdir(parents=True)
    offending = "see " + "X:" + r"\example-game-workbench-repo" + " for details"
    first = check(_write_payload(str(target), offending, session_id="local-md-session"))
    second = check(_write_payload(str(target), offending, session_id="local-md-session"))
    assert first is not None and "permissionDecision" not in first["hookSpecificOutput"]
    assert second is not None and "permissionDecision" not in second["hookSpecificOutput"]


def test_quiet_tier_outside_repo_is_advisory(tmp_target: str) -> None:
    offending = "the repo lives at " + "X:" + r"\example-game-workbench-repo"
    out = check(_write_payload(tmp_target, offending))
    assert out is not None
    assert "permissionDecision" not in out["hookSpecificOutput"]
    assert "WARN" in out["hookSpecificOutput"]["additionalContext"]
    assert "fix-concrete-path-citations" in out["hookSpecificOutput"]["additionalContext"]


def test_quiet_tier_inside_repo_scratch_dir_is_advisory(repo_root: Path) -> None:
    target = repo_root / "scratch" / "notes.md"
    target.parent.mkdir(parents=True)
    offending = "the repo lives at " + "X:" + r"\example-game-workbench-repo"
    out = check(_write_payload(str(target), offending))
    assert out is not None
    assert "permissionDecision" not in out["hookSpecificOutput"]
    assert "additionalContext" in out["hookSpecificOutput"]


def test_quiet_tier_dir_beats_loud_tier_extension(repo_root: Path) -> None:
    target_a = repo_root / "scratch" / "probe.py"
    target_a.parent.mkdir(parents=True, exist_ok=True)
    offending = "PATH = '" + "X:" + r"\example-game-workbench-repo" + "'"
    first = check(_write_payload(str(target_a), offending, session_id="scratch-py-session"))
    assert first is not None
    assert "permissionDecision" not in first["hookSpecificOutput"]
    assert "WARN" in first["hookSpecificOutput"]["additionalContext"]

    target_b = repo_root / "scratch" / "probe2.py"
    target_b.parent.mkdir(parents=True, exist_ok=True)
    second = check(_write_payload(str(target_b), offending, session_id="scratch-py-session"))
    assert second is None


def test_quiet_tier_fires_at_most_once_per_session(repo_root: Path) -> None:
    offending = "the repo lives at " + "X:" + r"\example-game-workbench-repo"

    target_a = repo_root / "scratch" / "a.md"
    target_a.parent.mkdir(parents=True)
    first = check(_write_payload(str(target_a), offending, session_id="warn-once-session"))
    assert first is not None
    assert "additionalContext" in first["hookSpecificOutput"]

    # A second, DIFFERENT file, same quiet-tier, same session -- still
    target_b = repo_root / "scratch" / "b.md"
    target_b.parent.mkdir(parents=True, exist_ok=True)
    second = check(_write_payload(str(target_b), offending, session_id="warn-once-session"))
    assert second is None

    target_c = repo_root / "scratch" / "c.md"
    target_c.parent.mkdir(parents=True, exist_ok=True)
    third = check(_write_payload(str(target_c), offending, session_id="a-different-session"))
    assert third is not None
    assert "additionalContext" in third["hookSpecificOutput"]


def test_quiet_tier_outside_the_repo_also_fires_at_most_once_per_session(tmp_target: str) -> None:
    """The cap has to be REACHABLE for out-of-repo targets, which are the
    QUIET tier's own headline case.

    It previously was not: the claim short-circuited to "always warn"
    whenever no repo root resolved, i.e. for exactly those targets, so a
    session writing repeatedly under a tempdir got re-warned every write. The
    sentinel now falls back to the system temp dir when there is no git
    common dir to hang it on."""
    offending = "the repo lives at " + "X:" + r"\example-game-workbench-repo"
    session = _sid()
    first = check(_write_payload(tmp_target, offending, session_id=session))
    assert first is not None
    assert "additionalContext" in first["hookSpecificOutput"]

    with tempfile.TemporaryDirectory() as d:
        other = str(Path(d) / "another.md")
        second = check(_write_payload(other, offending, session_id=session))
    assert second is None


def test_quiet_tier_hint_does_not_claim_a_commit_time_failure(tmp_target: str) -> None:
    offending = "the repo lives at " + "X:" + r"\example-game-workbench-repo"
    out = check(_write_payload(tmp_target, offending))
    assert out is not None
    reason = out["hookSpecificOutput"]["additionalContext"]
    assert "commit-time sweep" not in reason
    assert "machine-local get" in reason
    assert f"--apply {tmp_target}" in reason
    assert "abs-path-ok:" in reason


def test_quiet_tier_scratch_hint_does_not_claim_a_commit_time_failure(repo_root: Path) -> None:
    target = repo_root / "scratch" / "brief.md"
    target.parent.mkdir(parents=True)
    offending = "the repo lives at " + "X:" + r"\example-game-workbench-repo"
    out = check(_write_payload(str(target), offending))
    assert out is not None
    reason = out["hookSpecificOutput"]["additionalContext"]
    assert "commit-time sweep" not in reason
    assert "fix-concrete-path-citations" in reason


def test_loud_tier_hint_keeps_the_commit_time_sweep_wording(repo_root: Path) -> None:
    target = repo_root / "coordinator" / "skills" / "doc.md"
    target.parent.mkdir(parents=True)
    offending = "the repo lives at " + "X:" + r"\example-game-workbench-repo"
    out = check(_write_payload(str(target), offending))
    assert out is not None
    assert "commit-time sweep will fail" in out["hookSpecificOutput"]["additionalContext"]


def test_state_dir_is_loud_tier_not_quiet(repo_root: Path) -> None:
    target = repo_root / "state" / "handoffs" / "note.md"
    target.parent.mkdir(parents=True)
    offending = "the repo lives at " + "X:" + r"\example-game-workbench-repo"
    first = check(_write_payload(str(target), offending, session_id="state-session"))
    second = check(_write_payload(str(target), offending, session_id="state-session"))
    assert first is not None and "permissionDecision" not in first["hookSpecificOutput"]
    assert second is not None and "permissionDecision" not in second["hookSpecificOutput"]


def test_archive_dir_is_loud_tier_not_quiet(repo_root: Path) -> None:
    target = repo_root / "archive" / "bug-backlog" / "note.yaml"
    target.parent.mkdir(parents=True)
    offending = "the repo lives at " + "X:" + r"\example-game-workbench-repo"
    first = check(_write_payload(str(target), offending, session_id="archive-session"))
    second = check(_write_payload(str(target), offending, session_id="archive-session"))
    assert first is not None and "permissionDecision" not in first["hookSpecificOutput"]
    assert second is not None and "permissionDecision" not in second["hookSpecificOutput"]


def test_subagent_share_sidecar_quoting_its_own_finding_is_allowed(repo_root: Path) -> None:
    target = repo_root / "state" / "subagent-share" / "some-session" / "coordinatorcode-reviewer-abc.md"
    target.parent.mkdir(parents=True)
    offending = "Finding: drive-letter citation -- `" + "X:" + r"\Users\realperson\notes.txt`"
    out = check(_write_payload(str(target), offending))
    assert out is None


def test_review_trail_diff_transcript_is_allowed(repo_root: Path) -> None:
    target = repo_root / "state" / "review-trail" / "diffs" / "corpus-path-sweep.diff"
    target.parent.mkdir(parents=True)
    offending = "-legacy: " + "X:" + r"\some-repo" + "\n+legacy: repo-alias:some-repo\n"
    out = check(_write_payload(str(target), offending))
    assert out is None


def test_clean_write_is_allowed(tmp_target: str) -> None:
    out = check(_write_payload(tmp_target, "nothing offending here, just prose"))
    assert out is None


def test_marked_line_is_allowed(repo_root: Path) -> None:
    target = repo_root / "coordinator" / "skills" / "doc.md"
    target.parent.mkdir(parents=True)
    offending = (
        "the repo lives at " + "X:" + r"\example-game-workbench-repo"
        + "  # abs-path-ok: quoting the incident"
    )
    out = check(_write_payload(str(target), offending))
    assert out is None


def test_edit_reintroducing_a_pre_existing_legacy_citation_unchanged_is_allowed(
    repo_root: Path,
) -> None:
    target = repo_root / "coordinator" / "skills" / "doc.md"
    target.parent.mkdir(parents=True)
    legacy = "legacy: " + "X:" + r"\some-repo" + "\n"
    target.write_text(legacy, encoding="utf-8")
    out = check(_edit_payload(str(target), "some-repo", "some-repo (renamed note)"))
    assert out is None


def test_edit_introducing_a_new_citation_is_advisory(repo_root: Path) -> None:
    target = repo_root / "coordinator" / "skills" / "doc.md"
    target.parent.mkdir(parents=True)
    target.write_text("clean file\n", encoding="utf-8")
    new_line = "\nnew: " + "/Users/" + "realperson" + "/x\n"
    out = check(_edit_payload(str(target), "clean file\n", "clean file\n" + new_line))
    assert out is not None
    assert "permissionDecision" not in out["hookSpecificOutput"]
    assert "additionalContext" in out["hookSpecificOutput"]


def test_loud_tier_message_names_the_matched_citation(repo_root: Path) -> None:
    target = repo_root / "coordinator" / "skills" / "doc.md"
    target.parent.mkdir(parents=True)
    offending = "the repo lives at " + "X:" + r"\example-game-workbench-repo"
    out = check(_write_payload(str(target), offending))
    assert out is not None
    reason = out["hookSpecificOutput"]["additionalContext"]
    assert "drive-letter" in reason
    assert r"X:\example-game-workbench-repo" in reason


def test_quiet_tier_message_names_the_matched_citation(tmp_target: str) -> None:
    offending = "the repo lives at " + "X:" + r"\example-game-workbench-repo"
    out = check(_write_payload(tmp_target, offending))
    assert out is not None
    reason = out["hookSpecificOutput"]["additionalContext"]
    assert "drive-letter" in reason
    assert r"X:\example-game-workbench-repo" in reason


def test_advisory_names_the_written_file_in_the_runnable_fixer_command(repo_root: Path) -> None:
    target = repo_root / "coordinator" / "skills" / "doc.md"
    target.parent.mkdir(parents=True)
    offending = "the repo lives at " + "X:" + r"\example-game-workbench-repo"
    out = check(_write_payload(str(target), offending))
    assert out is not None
    reason = out["hookSpecificOutput"]["additionalContext"]
    assert "fix-concrete-path-citations" in reason
    assert f"--apply {target}" in reason
    assert "abs-path-ok:" in reason


def test_class_is_advisory_and_no_path_returns_permission_decision(repo_root: Path, tmp_target: str) -> None:
    assert CLASS == "advisory"

    loud_target = repo_root / "coordinator" / "skills" / "doc.md"
    loud_target.parent.mkdir(parents=True)
    offending = "the repo lives at " + "X:" + r"\example-game-workbench-repo"
    loud_out = check(_write_payload(str(loud_target), offending, session_id="class-loud-session"))
    assert loud_out is not None
    assert "permissionDecision" not in loud_out["hookSpecificOutput"]
    assert "permissionDecisionReason" not in loud_out["hookSpecificOutput"]

    quiet_out = check(_write_payload(tmp_target, offending, session_id=_sid()))
    assert quiet_out is not None
    assert "permissionDecision" not in quiet_out["hookSpecificOutput"]
    assert "permissionDecisionReason" not in quiet_out["hookSpecificOutput"]


def test_non_guarded_tool_is_allowed() -> None:
    assert check({"tool_name": "Bash", "tool_input": {"command": "ls"}}) is None


def test_missing_target_is_allowed() -> None:
    assert check({"tool_name": "Write", "tool_input": {"content": "X:\\foo"}}) is None

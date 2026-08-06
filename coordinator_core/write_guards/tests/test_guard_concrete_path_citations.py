"""coordinator_core.write_guards.tests.test_guard_concrete_path_citations

Exercises the write-time leg directly through `check(payload)` (the same
entrypoint `engine.evaluate` calls) -- the live end-to-end proof that the
guard actually fires against a real Write/Edit payload is the module's own
`abs-path-ok:`-marked test fixtures in
`coordinator_core/ops/session/tests/test_guard_concrete_path_citations.py`,
which were denied by THIS guard (via the real PreToolUse hook chain, not a
mock) before the markers were added -- see that file's own write history.

Tier coverage (PM ruling 2026-08-05: warn, never deny -- see the guard
module's own docstring for the full "warn, never deny" rationale). The guard
is `CLASS = "advisory"` on every tier now; LOUD/QUIET decides only how OFTEN
the advisory repeats, not whether the write is blocked. LOUD targets get a
real git repo fixture (`repo_root`) so a repo-relative path actually
resolves and lands in the LOUD table (`coordinator/skills/...`); QUIET
targets use either a bare tempdir outside any git repo (the "outside the
repo" QUIET case, `tmp_target`) or a `scratch/`-prefixed path inside the
repo fixture. Because both tiers now return the identical advisory shape,
the loud-vs-quiet distinction is observable ONLY through repeat behavior --
LOUD fires on every offending write in a session, QUIET fires at most once
per session -- so tier tests are pinned by calling `check()` twice with the
SAME `session_id` and asserting on the second call's presence/absence.
"""
from __future__ import annotations

import subprocess
import tempfile
import uuid
from pathlib import Path

import pytest

from coordinator_core.write_guards.guard_concrete_path_citations import CLASS, check


def _sid() -> str:
    """A session id no other test (or earlier run of this file) has used.

    The QUIET tier's once-per-session claim now reaches out-of-repo targets
    too, and their sentinel lives under the shared system temp dir rather
    than a per-test git fixture -- so a hardcoded session id would suppress
    the second test to use it, and every run after the first.
    """
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
    """A target path OUTSIDE any git repo -- the "outside the repo" QUIET case."""
    with tempfile.TemporaryDirectory() as d:
        yield str(Path(d) / "doc.md")


@pytest.fixture()
def repo_root():
    """A real, empty git repo -- so repo-relative LOUD/QUIET table lookups
    actually resolve (git init here runs as a subprocess from test code, not
    through an interactive shell -- unaffected by the interactive-session
    destructive-git guard)."""
    with tempfile.TemporaryDirectory() as d:
        subprocess.run(["git", "init", "-q", d], check=True)
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
    """`.py` is a LOUD-tier extension repo-wide, not just under the named
    prefix directories -- executable code anywhere is load-bearing."""
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
    """A throwaway `.py` probe under a QUIET directory stays quiet (fires at
    most once per session), not loud.

    Pins the ORDER of the two checks, which is the whole reason the tiering
    exists: `.py` is a LOUD extension repo-wide, but a script under
    `scratch/` is nothing but its author's scratch work. Without this test
    the precedence is unpinned -- every tier test passed under the opposite
    ordering too. Distinguished from LOUD by repeat behavior: a LOUD `.py`
    fires every write, this one fires once per session.
    """
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
    # suppressed. The once-per-session claim is session-wide, not per-file.
    target_b = repo_root / "scratch" / "b.md"
    target_b.parent.mkdir(parents=True, exist_ok=True)
    second = check(_write_payload(str(target_b), offending, session_id="warn-once-session"))
    assert second is None

    # A different session gets its own warn slot.
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
    """A file that will never be committed cannot fail the commit-time sweep,
    so the QUIET advisory must not say it will -- while still leading with the
    full alternative (`machine-local get`, the runnable fixer, the marker)."""
    offending = "the repo lives at " + "X:" + r"\example-game-workbench-repo"
    out = check(_write_payload(tmp_target, offending))
    assert out is not None
    reason = out["hookSpecificOutput"]["additionalContext"]
    assert "commit-time sweep" not in reason
    assert "machine-local get" in reason
    assert f"--apply {tmp_target}" in reason
    assert "abs-path-ok:" in reason


def test_quiet_tier_scratch_hint_does_not_claim_a_commit_time_failure(repo_root: Path) -> None:
    """Same for an in-repo but known-transient surface (`scratch/`) -- the
    observed false positive was a session scratch file deleted at end of run."""
    target = repo_root / "scratch" / "brief.md"
    target.parent.mkdir(parents=True)
    offending = "the repo lives at " + "X:" + r"\example-game-workbench-repo"
    out = check(_write_payload(str(target), offending))
    assert out is not None
    reason = out["hookSpecificOutput"]["additionalContext"]
    assert "commit-time sweep" not in reason
    assert "fix-concrete-path-citations" in reason


def test_loud_tier_hint_keeps_the_commit_time_sweep_wording(repo_root: Path) -> None:
    """Detection and stakes are unchanged on committable surfaces: the sweep
    consequence is TRUE there and stays named."""
    target = repo_root / "coordinator" / "skills" / "doc.md"
    target.parent.mkdir(parents=True)
    offending = "the repo lives at " + "X:" + r"\example-game-workbench-repo"
    out = check(_write_payload(str(target), offending))
    assert out is not None
    assert "commit-time sweep will fail" in out["hookSpecificOutput"]["additionalContext"]


def test_state_dir_is_loud_tier_not_quiet(repo_root: Path) -> None:
    """`state/` is always-on load-bearing substrate (lessons, handoffs read
    and trusted at session start) -- an earlier cut of the tier table wrongly
    bucketed it with genuine scratch. A concrete-path citation here fires on
    every write, not just once per session."""
    target = repo_root / "state" / "handoffs" / "note.md"
    target.parent.mkdir(parents=True)
    offending = "the repo lives at " + "X:" + r"\example-game-workbench-repo"
    first = check(_write_payload(str(target), offending, session_id="state-session"))
    second = check(_write_payload(str(target), offending, session_id="state-session"))
    assert first is not None and "permissionDecision" not in first["hookSpecificOutput"]
    assert second is not None and "permissionDecision" not in second["hookSpecificOutput"]


def test_archive_dir_is_loud_tier_not_quiet(repo_root: Path) -> None:
    """`archive/` is closed-out substrate, not scratch -- same tiering fix
    as `state/`."""
    target = repo_root / "archive" / "bug-backlog" / "note.yaml"
    target.parent.mkdir(parents=True)
    offending = "the repo lives at " + "X:" + r"\example-game-workbench-repo"
    first = check(_write_payload(str(target), offending, session_id="archive-session"))
    second = check(_write_payload(str(target), offending, session_id="archive-session"))
    assert first is not None and "permissionDecision" not in first["hookSpecificOutput"]
    assert second is not None and "permissionDecision" not in second["hookSpecificOutput"]


def test_subagent_share_sidecar_quoting_its_own_finding_is_allowed(repo_root: Path) -> None:
    """A reviewer/integrator sidecar under `state/subagent-share/` quoting an
    offending line as evidence for its own finding must not be flagged by the
    guard whose finding it's quoting -- the evidence-artifact exemption
    (module docstring) applies at detection time via `filename`, not just at
    tiering time."""
    target = repo_root / "state" / "subagent-share" / "some-session" / "coordinatorreview-integrator-abc.md"
    target.parent.mkdir(parents=True)
    offending = "Finding: drive-letter citation -- `" + "X:" + r"\Users\realperson\notes.txt`"
    out = check(_write_payload(str(target), offending))
    assert out is None


def test_review_trail_diff_transcript_is_allowed(repo_root: Path) -> None:
    """A frozen diff under `state/review-trail/diffs/` legitimately still
    contains a pre-rewrite literal on its `-` line -- exempt for the same
    reason as a sidecar."""
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
    """Review finding (coordinatorcode-reviewer-54284751, Finding 1, P2):
    the advisory message must name WHICH citation tripped the guard (rule +
    matched text), not just which file -- otherwise the operator is forced
    to re-scan the whole diff. Pins the detail the 220-byte compression pass
    had dropped while still threading `violations` through unread."""
    target = repo_root / "coordinator" / "skills" / "doc.md"
    target.parent.mkdir(parents=True)
    offending = "the repo lives at " + "X:" + r"\example-game-workbench-repo"
    out = check(_write_payload(str(target), offending))
    assert out is not None
    reason = out["hookSpecificOutput"]["additionalContext"]
    assert "drive-letter" in reason
    assert r"X:\example-game-workbench-repo" in reason  # abs-path-ok: quoting the matched citation under test


def test_quiet_tier_message_names_the_matched_citation(tmp_target: str) -> None:
    offending = "the repo lives at " + "X:" + r"\example-game-workbench-repo"
    out = check(_write_payload(tmp_target, offending))
    assert out is not None
    reason = out["hookSpecificOutput"]["additionalContext"]
    assert "drive-letter" in reason
    assert r"X:\example-game-workbench-repo" in reason  # abs-path-ok: quoting the matched citation under test


def test_advisory_names_the_written_file_in_the_runnable_fixer_command(repo_root: Path) -> None:
    """The whole point of the loud/quiet demotion (module docstring): a
    denied write never reached disk, so the deny message's fixer offer had
    no file to act on. Now the write always lands, so the advisory must name
    the ACTUAL file just written, immediately after `--apply`, so the
    fixer invocation is copy-paste runnable -- not just a bare mention of
    the fixer's name. Also pins the `abs-path-ok:` marker escape is
    mentioned as the alternative for citations with no mechanical rewrite."""
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
    """`CLASS == "advisory"` is the module-level contract this whole
    dispatch exists to enforce -- pin it directly, plus that neither a LOUD
    nor a QUIET firing can smuggle a `permissionDecision` key back in."""
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
    assert check({"tool_name": "Write", "tool_input": {"content": "X:\\foo"}}) is None  # abs-path-ok: synthetic test fixture, no file_path given

"""Tests for coordinator_core.ops.research_dir_restructure.

Coverage contract (Wave-3 settlement A5 + CC-4/CC-7,
docs/plans/2026-07-22-wave-3-design-settlements-15-design-bear.md): a named
test per four-state-table entry and per crash-window state is an acceptance
criterion —

  - fresh restructure: both steps pending → both renamed
  - per-step skip: src absent + dest present → step already done
  - crash between the two renames: rerun skips step 1, completes step 2
  - both present → structured error naming both paths, ZERO writes (CC-7)
  - both absent → structured error (substrate missing; CC-7)
  - CC-4 double invocation: full rerun after success →
    {restructured: false, steps_applied: []} with new_dir populated

All filesystem work happens in tmp_path throwaway trees; the git common dir is
a plain ``.git`` directory (main_worktree_root only takes ``.parent``) — no git
subprocess anywhere.
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops import research_dir_restructure as mod
from coordinator_core.ops.session import safe_commit_offer
from coordinator_core.session import core as session_core

# Declared, not excused: this file's `_mk_git_worktree`-based tests build a REAL git
# repo because `safe_commit_offer.compute_offer` genuinely shells out to `git diff`/
# `git ls-files` to compute the dirty-tree side of a session's scope -- no mock
# stands in for that real diff/ls-files behaviour. Only the tests exercising that
# offer path use it, each via its own tmp_path repo, so there is no shared state to
# hoist. The spawn ratchet's `_BASELINE` is shrink-only pre-existing residue and is
# explicitly not the route for this file --
# coordinator_core/tests/test_no_new_spawning_tests.py Rule 2.
pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]
from coordinator_core.session import scope as session_scope
from coordinator_core.win_portability import no_console_passthrough_kwargs

TOPIC = "warp-core-ejection"
DATE = "2026-07-22"
RESULT_NAME = f"{DATE}-{TOPIC}.md"
TRAIL_NAME = f"{DATE}-{TOPIC}"
RUN = f"{DATE}-{TOPIC}"


def _call(params: dict, repo_root) -> dict:
    return asyncio.run(mod._handler(params, repo_root))


def _mk_worktree(tmp_path: Path) -> Path:
    (tmp_path / ".git").mkdir()
    (tmp_path / "docs" / "research" / "archive").mkdir(parents=True)
    return tmp_path


def _mk_substrate(worktree: Path):
    """Pre-restructure layout: dated result md + archived paper-trail dir."""
    research = worktree / "docs" / "research"
    result_md = research / RESULT_NAME
    result_md.write_text("# synthesis\n", encoding="utf-8")
    trail = research / "archive" / TRAIL_NAME
    (trail / "prompts").mkdir(parents=True)
    (trail / "decisions.md").write_text("decisions\n", encoding="utf-8")
    return result_md, trail


def _params(worktree: Path) -> dict:
    return {
        "topic_slug": TOPIC,
        "dated_result_path": str(worktree / "docs" / "research" / RESULT_NAME),
        "archived_paper_trail_dir": str(
            worktree / "docs" / "research" / "archive" / TRAIL_NAME
        ),
    }


def _mk_real_git_worktree(tmp_path: Path) -> Path:
    """Real ``git init`` worktree (unlike ``_mk_worktree``'s throwaway
    ``.git`` dir) — needed only by the claim-restatement test below, which
    asserts through ``safe_commit_offer.compute_offer`` and therefore needs
    a real ``git status`` to run against."""
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True, **no_console_passthrough_kwargs())
    subprocess.run(
        ["git", "config", "user.email", "t@example.com"], cwd=tmp_path, check=True,
        **no_console_passthrough_kwargs(),
    )
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True, **no_console_passthrough_kwargs())
    (tmp_path / "docs" / "research" / "archive").mkdir(parents=True)
    (tmp_path / "README.md").write_text("x\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True, **no_console_passthrough_kwargs())
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=tmp_path, check=True, **no_console_passthrough_kwargs())
    return tmp_path


def test_claimed_result_and_trail_descendant_restated_at_new_paths(tmp_path, monkeypatch):
    """Both step SHAPES restate at their new path: the dated-result FILE
    claim (single-path restatement, ``_restate_single_file_claim``) and a
    claimed descendant nested inside the paper-trail DIRECTORY
    (``restate_touched_tree``) both land in ``compute_offer``'s
    ``safe_paths`` after the op runs — proving neither helper was skipped
    for its shape. Modeled on coordinator_core/ops/
    test_migrate_cross_repo_layout.py::
    test_untracked_move_with_live_session_relocates_touch_claim."""
    worktree = _mk_real_git_worktree(tmp_path)
    result_md, trail = _mk_substrate(worktree)
    result_rel = str(result_md.relative_to(worktree))
    decisions_rel = str((trail / "decisions.md").relative_to(worktree))

    session_core.init("mine", cwd=str(worktree))
    session_scope.touch("mine", result_rel, cwd=str(worktree))
    session_scope.touch("mine", decisions_rel, cwd=str(worktree))
    monkeypatch.setenv("COORDINATOR_SESSION_ID", "mine")

    result = _call(_params(worktree), worktree / ".git")
    assert result["exit_code"] == 0
    assert result["restructured"] is True
    assert result["steps_applied"] == ["dated_result", "paper_trail"]

    run = worktree / "docs" / "research" / TOPIC / RUN
    new_result_rel = (run / "synthesis.md").relative_to(worktree).as_posix()
    new_decisions_rel = (run / "workdir" / "decisions.md").relative_to(worktree).as_posix()

    offer = safe_commit_offer.compute_offer("mine", cwd=str(worktree))
    assert new_result_rel in offer["safe_paths"]
    assert new_decisions_rel in offer["safe_paths"]


def test_unresolvable_session_id_still_renames_without_claim_bookkeeping(tmp_path, monkeypatch):
    """No live/resolvable session (the boot-sweep/no-session shape) → the
    restatement helpers are never reached (both degrade to a no-op per
    their own docstrings), but the renames themselves proceed
    unconditionally — mirrors migrate_cross_repo_layout._move_one's
    documented fallback for a falsy session_id."""
    monkeypatch.delenv("COORDINATOR_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    worktree = _mk_worktree(tmp_path)
    _mk_substrate(worktree)
    result = _call(_params(worktree), worktree / ".git")
    assert result["exit_code"] == 0
    assert result["restructured"] is True
    assert result["steps_applied"] == ["dated_result", "paper_trail"]


def test_fresh_restructure_applies_both_steps(tmp_path):
    worktree = _mk_worktree(tmp_path)
    result_md, trail = _mk_substrate(worktree)
    result = _call(_params(worktree), worktree / ".git")
    run = worktree / "docs" / "research" / TOPIC / RUN
    assert result["exit_code"] == 0
    assert result["restructured"] is True
    assert result["steps_applied"] == ["dated_result", "paper_trail"]
    assert result["new_dir"] == str(run)
    assert not result_md.exists()
    assert not trail.exists()
    assert (run / "synthesis.md").read_text(encoding="utf-8") == "# synthesis\n"
    assert (run / "workdir" / "decisions.md").exists()
    assert (run / "workdir" / "prompts").is_dir()


def test_destinations_classify_as_run_child(tmp_path):
    from coordinator_core.ops import _research_layout as layout

    worktree = _mk_worktree(tmp_path)
    _mk_substrate(worktree)
    _call(_params(worktree), worktree / ".git")
    research = worktree / "docs" / "research"
    for dest in (research / TOPIC / RUN / "synthesis.md", research / TOPIC / RUN / "workdir"):
        assert layout.classify(dest.relative_to(research).as_posix()) == layout.RUN_CHILD


def test_topic_slug_outside_layout_is_refused_with_zero_writes(tmp_path):
    worktree = _mk_worktree(tmp_path)
    result_md, trail = _mk_substrate(worktree)
    params = _params(worktree)
    params["topic_slug"] = "evidence"
    result = _call(params, worktree / ".git")
    assert result["exit_code"] == 1
    assert "run-dir child" in result["error"]
    assert result_md.exists() and trail.exists()
    assert not (worktree / "docs" / "research" / "evidence").exists()


def test_double_invocation_full_rerun_is_noop(tmp_path):
    """CC-4: second identical call → restructured false, steps_applied []."""
    worktree = _mk_worktree(tmp_path)
    _mk_substrate(worktree)
    first = _call(_params(worktree), worktree / ".git")
    assert first["restructured"] is True
    second = _call(_params(worktree), worktree / ".git")
    run = worktree / "docs" / "research" / TOPIC / RUN
    assert second["exit_code"] == 0
    assert second["restructured"] is False
    assert second["steps_applied"] == []
    assert second["new_dir"] == str(run)
    assert (run / "synthesis.md").exists()


def test_crash_between_renames_rerun_completes_pending_step(tmp_path):
    """Settlement A5 crash window: step 1 done, step 2 pending → rerun completes 2."""
    worktree = _mk_worktree(tmp_path)
    result_md, trail = _mk_substrate(worktree)
    # Simulate the crash: step 1 (dated result) already landed, step 2 did not.
    run = worktree / "docs" / "research" / TOPIC / RUN
    run.mkdir(parents=True)
    result_md.rename(run / "synthesis.md")
    result = _call(_params(worktree), worktree / ".git")
    assert result["exit_code"] == 0
    assert result["restructured"] is True
    assert result["steps_applied"] == ["paper_trail"]
    assert not trail.exists()
    assert (run / "workdir" / "decisions.md").exists()
    assert (run / "synthesis.md").exists()


def test_step_both_present_is_structured_error_with_zero_writes(tmp_path):
    """CC-7: src+dest both exist on a step → error naming both; other step untouched."""
    worktree = _mk_worktree(tmp_path)
    result_md, trail = _mk_substrate(worktree)
    run = worktree / "docs" / "research" / TOPIC / RUN
    run.mkdir(parents=True)
    dup = run / "synthesis.md"
    dup.write_text("divergent copy\n", encoding="utf-8")
    result = _call(_params(worktree), worktree / ".git")
    assert result["exit_code"] == 1
    assert result["step"] == "dated_result"
    # Use the structured src/dest fields, not substring-matching the
    # human-readable `error` message: that message embeds the paths via
    # !r, which on Windows doubles each backslash separator, so a plain
    # str(path) containment check against it spuriously fails.
    assert result["src"] == str(result_md)
    assert result["dest"] == str(dup)
    # zero writes: both sides of the erroring step intact, pending step NOT applied
    assert result_md.exists() and dup.exists()
    assert trail.exists()
    assert not (run / "workdir").exists()


def test_step_both_absent_is_structured_error(tmp_path):
    """CC-7: neither src nor dest on a step → substrate-missing error."""
    worktree = _mk_worktree(tmp_path)
    _mk_substrate(worktree)
    params = _params(worktree)
    params["archived_paper_trail_dir"] = str(
        worktree / "docs" / "research" / "archive" / "2026-01-01-never-existed"
    )
    result = _call(params, worktree / ".git")
    assert result["exit_code"] == 1
    assert result["step"] == "paper_trail"
    assert "substrate missing" in result["error"]
    # classification precedes mutation: the pending dated_result step NOT applied
    assert (worktree / "docs" / "research" / RESULT_NAME).exists()
    assert not (worktree / "docs" / "research" / TOPIC).exists()


def test_undated_basename_is_structured_error(tmp_path):
    worktree = _mk_worktree(tmp_path)
    _mk_substrate(worktree)
    research = worktree / "docs" / "research"
    undated = research / "no-date-result.md"
    undated.write_text("x", encoding="utf-8")
    params = _params(worktree)
    params["dated_result_path"] = str(undated)
    result = _call(params, worktree / ".git")
    assert result["exit_code"] == 1
    assert "YYYY-MM-DD" in result["error"]


def test_path_escaping_research_tree_is_rejected(tmp_path):
    worktree = _mk_worktree(tmp_path)
    _mk_substrate(worktree)
    outside = tmp_path / "outside" / f"{DATE}-{TOPIC}.md"
    outside.parent.mkdir()
    outside.write_text("x", encoding="utf-8")
    params = _params(worktree)
    params["dated_result_path"] = str(outside)
    result = _call(params, worktree / ".git")
    assert result["exit_code"] == 1
    assert "escapes" in result["error"]
    assert outside.exists()


def test_worktree_relative_paths_accepted(tmp_path):
    worktree = _mk_worktree(tmp_path)
    _mk_substrate(worktree)
    params = {
        "topic_slug": TOPIC,
        "dated_result_path": f"docs/research/{RESULT_NAME}",
        "archived_paper_trail_dir": f"docs/research/archive/{TRAIL_NAME}",
    }
    result = _call(params, worktree / ".git")
    assert result["exit_code"] == 0
    assert result["restructured"] is True
    assert result["steps_applied"] == ["dated_result", "paper_trail"]


def test_missing_params_and_unsafe_slug_rejected(tmp_path):
    worktree = _mk_worktree(tmp_path)
    assert _call({}, worktree / ".git")["exit_code"] == 1
    bad_slug = _call(
        {"topic_slug": "a/b", "dated_result_path": "x", "archived_paper_trail_dir": "y"},
        worktree / ".git",
    )
    assert bad_slug["exit_code"] == 1
    assert "safe path segment" in bad_slug["error"]


def test_repo_root_arg_required(tmp_path):
    worktree = _mk_worktree(tmp_path)
    _mk_substrate(worktree)
    result = _call(_params(worktree), None)
    assert result["exit_code"] == 1
    assert "repo_root" in result["error"]

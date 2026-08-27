"""
coordinator_core.tests.test_close_ceremony_leaves_no_orphans — C6.

Purpose: prove the composed AC7 claim end to end for the one reachable close
ceremony (`/quick-wrap`, delivered by C5): a session that produced artifacts
through the two now-declaring producers -- `receipt_emit.py` (C2) and
`review_trail_write.py` (C3) -- leaves ZERO untracked paths under its own
artifact directories after `quick_wrap_assemble.brief()` runs.

No earlier chunk's test surface proves this composed claim. C5's own suite
proves quick-wrap invokes the commit op, non-blocking on failure (AC5); C4's
proves peer isolation and the degraded/empty/failure cases (AC6). Neither
mints a real session, writes real declared artifacts through the two C2/C3
producers, and then runs the real close ceremony end to end. This module is
that composition -- the only one in the plan that exercises C2 through C5
together.

NARROWED 2026-08-20 (see the C6 dispatch brief): AC7 originally named "the two
reachable close ceremonies" (quick-wrap + workstream-complete). Only
`/quick-wrap` is actually wired (C5, `14a80d54a`); `/workstream-complete`'s
half is split out as C7, blocked on a PM budget ruling. This module asserts
AC7 for `/quick-wrap` ONLY -- a `/workstream-complete` leg lands with C7, not
here; a test against unwired code would be red-by-construction or a stub
proving nothing, and both are worse than an absent test.

This is a git-spawning integration test: `compute_offer` (reached via
`quick_wrap_assemble.brief()` -> `commit_session_offer_async` ->
`safe_commit_offer.compute_offer`) shells out to `git diff`/`git ls-files` for
its dirty scan, and the fixture itself needs a real `git init` repo (mirrors
`coordinator_core/ops/test_research_dir_restructure.py` and
`coordinator_core/ops/tests/test_review_trail_write_declares.py`'s identical
git-repo fixture shape). Marked `spawns_process` + `cadence` per the repo's
two-file marker-registration requirement (both markers are already registered
in `pyproject.toml` and `coordinator_core/pytest.ini`) -- an unmarked
git-spawning test trips the spawn ratchet
(`coordinator_core/tests/test_no_unbatched_per_item_git_spawn.py`).

Negative-spec: this test asserts `git status --porcelain -- <session's own
artifact dirs>` only. It never runs a bare/global `git status` clean check --
on this shared branch that would fail from unrelated peer traffic and be a
permanently red test.

Spec backlink:
  state/dispatch-briefs/2026-08-20-the-close-ceremony-commits-what-the-session-wrote/C6.md
  docs/plans/2026-08-20-the-close-ceremony-commits-what-the-session-wrote.md § AC7
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core import quick_wrap_assemble as qwa
from coordinator_core.ops.ceremony import receipt_emit
from coordinator_core.ops.ceremony.pipeline_context import PipelineContext
from coordinator_core.ops.review_trail_write import write_review_trail_entry
from coordinator_core.session import core as session_core

# Real `git init` fixture + real `git diff`/`git status` scans reached through
# quick_wrap_assemble.brief() -> commit_session_offer_async -> compute_offer.
# Spawn ratchet: coordinator_core/tests/test_no_unbatched_per_item_git_spawn.py
pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def _make_repo(tmp_path: Path) -> Path:
    """A real, empty git repo with one initial commit (mirrors
    test_review_trail_write_declares.py's `git_repo` fixture, plus the
    initial commit `test_safe_commit_offer.py::_make_repo` also carries --
    `commit_session_offer_async`'s own commit path needs a resolvable HEAD)."""
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "config", "user.email", "t@example.com"], cwd=tmp_path, check=True
    )
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True)
    (tmp_path / "README.md").write_text("x\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=tmp_path, check=True)
    return tmp_path


def _untracked_under(repo: Path, rel_dir: str) -> list[str]:
    """Every path `git status --porcelain` reports under *rel_dir* -- scoped
    to the session's own artifact directory, never a bare/global status
    check (see module Negative-spec)."""
    proc = subprocess.run(
        ["git", "status", "--porcelain", "--", rel_dir],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=True,
    )
    return [line for line in proc.stdout.splitlines() if line.strip()]


def test_quick_wrap_close_commits_declared_artifacts_leaving_no_orphans(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _make_repo(tmp_path)
    sid = "sess-close-ceremony-c6-01"
    session_core.init(sid, "test goal", str(repo))
    monkeypatch.setenv("COORDINATOR_SESSION_ID", sid)

    # --- C2 producer: emit a ceremony receipt; self-declares via
    # session_scope.touch_written_path (receipt_emit.py's own contract). ---
    ctx = PipelineContext(ceremony="wsc", scope_mode="auto")
    receipt_path, _op_tail = receipt_emit.emit_receipt(
        ctx, repo_root=repo, sid=sid, emitted_at="2026-08-20T00:00:00Z"
    )
    assert receipt_path.is_file()

    # --- C3 producer: write a review-trail entry; self-declares via the same
    # mechanism (review_trail_write.py's own contract). ---
    trail_result = write_review_trail_entry(
        sha_range="abc1234",
        reviewer="waived",
        scope="chain",
        verdict="waived",
        diff_loc=0,
        scope_kind="plan",
        session_id=sid,
        caller_worktree=repo,
    )
    trail_path = Path(trail_result["out_path"])
    assert trail_path.is_file()

    # Sanity check on the fixture itself: before the ceremony runs, both
    # artifact directories are genuinely dirty (untracked).
    before = _untracked_under(repo, "state/ceremony") + _untracked_under(
        repo, "state/review-trail"
    )
    assert before, "fixture setup did not actually leave dirty declared artifacts"

    # --- Run the one reachable close ceremony (C5): /quick-wrap. ---
    envelope = qwa.brief(worktree_root=repo)
    assert envelope["gates"]["commit_outcome"]["status"] in (
        "committed",
        "empty",
        "partial",
    ), envelope["gates"]["commit_outcome"]

    # --- AC7: zero untracked paths remain under this session's OWN artifact
    # directories after the close ceremony ran. ---
    after = _untracked_under(repo, "state/ceremony") + _untracked_under(
        repo, "state/review-trail"
    )
    assert after == [], f"orphaned paths survived the close ceremony: {after!r}"

    # The receipt and trail entry are genuinely committed, not merely absent
    # from `git status` because they were deleted.
    assert receipt_path.is_file()
    assert trail_path.is_file()
    log = subprocess.run(
        ["git", "log", "--name-only", "--pretty=format:"],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    receipt_rel = str(receipt_path.relative_to(repo)).replace("\\", "/")
    trail_rel = str(trail_path.relative_to(repo)).replace("\\", "/")
    assert receipt_rel in log, f"{receipt_rel!r} was never committed"
    assert trail_rel in log, f"{trail_rel!r} was never committed"

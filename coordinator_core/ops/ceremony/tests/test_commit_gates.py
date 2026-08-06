"""
coordinator_core.ops.ceremony.tests.test_commit_gates

Tests for commit_gates.py -- the native ports of the deleted
`check-workstream-complete-deletion-blocks.sh` and `dirty-tree-gate.sh`
(the C3 chunk of the `wsc_tail` rebuild,
docs/plans/2026-07-16-wsc-pure-python-tail-rebuild.md).

Coverage (parity-oracle assertions, per the deleted
`tests/wsc-asic/test-wsc-commit-parity.sh` recovered from
`example-doctrine-repo:85006468^:coordinator/tests/wsc-asic/test-wsc-commit-parity.sh`):
  (c)  deletion_block_gate passes on a well-formed message (Kept block only,
       no staged deletions).
  (c2) deletion_block_gate fails on a malformed message (Deleted-claimed path
       that was never staged for deletion).
  (e)  deletion_block_gate does NOT false-positive on a concurrent sibling's
       staged deletion that falls OUTSIDE gate_paths -- the F3 inverse check
       is scoped, not whole-index.

Plus native additions beyond the recovered oracle (the oracle only exercised
the bash CLI end-to-end; these pin the Python module's own unit-level
contract):
  skip_gate_when_empty         -- empty gate_paths + no Step 2.67 block skips
                                   the gate entirely (never scored ambiguous-pass).
  kept_claim_missing           -- Kept-claimed path absent from HEAD and staged
                                   set is a mismatch.
  kept_line_malformed          -- a Kept-block line with no em-dash separator
                                   is flagged, not silently treated as a path.
  parse_step267_blocks_*       -- blank-line-inside-block grouping, block-header
                                   termination.
  dirty_tree_gate porcelain classification -- staged skip, EOL-phantom skip,
                                   known-concurrent-owner (handoff scope:) skip,
                                   unattributable report, rename destination-only
                                   handling.

Spec backlink: docs/plans/2026-07-16-wsc-pure-python-tail-rebuild.md § C3 (AC10).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.ceremony.commit_gates import (
    DirtyTreeOutcome,
    GateOutcome,
    ParsedBlocks,
    deletion_block_gate,
    dirty_tree_gate,
    has_step267_block,
    parse_step267_blocks,
)

_EM_DASH = " — "


def _git(args, cwd) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True)


def _init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "-q"], repo)
    _git(["config", "user.email", "t@t.example"], repo)
    _git(["config", "user.name", "t"], repo)
    return repo


def _seed_file(repo: Path, rel_path: str, content: str) -> None:
    p = repo / rel_path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


# ---------------------------------------------------------------------------
# parse_step267_blocks / has_step267_block
# ---------------------------------------------------------------------------


def test_parse_blocks_deleted_and_kept():
    msg = (
        "subject\n"
        "\n"
        "Deleted (Step 2.67):\n"
        "a/one.md\n"
        "a/two.md\n"
        "\n"
        "Kept (Step 2.67):\n"
        f"a/three.md{_EM_DASH}still needed\n"
        "--- end Step 2.67 blocks ---\n"
    )
    parsed = parse_step267_blocks(msg)
    assert parsed == ParsedBlocks(
        deleted_claimed=["a/one.md", "a/two.md"],
        kept_claimed=["a/three.md"],
        kept_malformed=[],
    )
    assert has_step267_block(msg) is True


def test_parse_blocks_blank_line_inside_block_does_not_terminate():
    msg = (
        "subject\n"
        "\n"
        "Deleted (Step 2.67):\n"
        "a/one.md\n"
        "\n"
        "a/two.md\n"
        "--- end Step 2.67 blocks ---\n"
    )
    parsed = parse_step267_blocks(msg)
    assert parsed.deleted_claimed == ["a/one.md", "a/two.md"]


def test_parse_blocks_malformed_kept_line_flagged():
    msg = (
        "subject\n"
        "\n"
        "Kept (Step 2.67):\n"
        "a/no-separator.md\n"
        "--- end Step 2.67 blocks ---\n"
    )
    parsed = parse_step267_blocks(msg)
    assert parsed.kept_claimed == []
    assert parsed.kept_malformed == ["a/no-separator.md"]


def test_has_step267_block_false_on_plain_message():
    assert has_step267_block("subject\n\nprose only\n") is False


# ---------------------------------------------------------------------------
# deletion_block_gate -- assertion (c): PASS on well-formed message
# ---------------------------------------------------------------------------


def test_deletion_gate_assertion_c_passes_well_formed_no_staged_deletions(tmp_path):
    repo = _init_repo(tmp_path)
    _seed_file(repo, "tasks/my-feature/todo.md", "content")
    _git(["add", "--", "tasks/my-feature/todo.md"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)

    msg = (
        "workstream-complete: fixture-check\n"
        "\n"
        "Some prose.\n"
        "\n"
        "Kept (Step 2.67):\n"
        f"tasks/my-feature/todo.md{_EM_DASH}still load-bearing for active sibling workstream\n"
        "--- end Step 2.67 blocks ---\n"
    )

    outcome = deletion_block_gate(msg, gate_paths=["tasks/my-feature/todo.md"], cwd=repo)
    assert outcome.passed is True
    assert outcome.diagnostics == []


# ---------------------------------------------------------------------------
# deletion_block_gate -- assertion (c2): FAIL on malformed message
# ---------------------------------------------------------------------------


def test_deletion_gate_assertion_c2_fails_unstaged_deleted_claim(tmp_path):
    repo = _init_repo(tmp_path)
    _seed_file(repo, "tasks/my-feature/todo.md", "content")
    _git(["add", "--", "tasks/my-feature/todo.md"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)

    msg = (
        "workstream-complete: fixture-check\n"
        "\n"
        "Deleted (Step 2.67):\n"
        "tasks/nonexistent-path.md\n"
        "\n"
        "--- end Step 2.67 blocks ---\n"
    )

    outcome = deletion_block_gate(msg, gate_paths=["tasks/nonexistent-path.md"], cwd=repo)
    assert outcome.passed is False
    assert any("NOT staged for deletion" in d for d in outcome.diagnostics)


# ---------------------------------------------------------------------------
# deletion_block_gate -- Assertion-1 recognizes a staged RENAME source as
# "staged for deletion" (2026-08-06 fix, live incident: commit `64acc1254`,
# a move-set of changelog/review-trail files into an archive directory,
# refused with "Deleted-claim NOT staged for deletion" for every moved path).
# See `commit_gates._parse_name_status_rename_sources`'s own docstring for
# the full root-cause writeup.
# ---------------------------------------------------------------------------


def test_deletion_gate_assertion_1_recognizes_staged_rename_source(tmp_path):
    repo = _init_repo(tmp_path)
    content = "content block\n" * 40  # long/repetitive enough for git's rename detector
    _seed_file(repo, "week-changelog/2026-07-20.md", content)
    _git(["add", "-A"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)

    old_path = repo / "week-changelog/2026-07-20.md"
    old_path.unlink()
    new_path = repo / "archive/week-changelog/2026-07-20.md"
    new_path.parent.mkdir(parents=True)
    new_path.write_text(content, encoding="utf-8")
    _git(
        [
            "add",
            "--",
            "week-changelog/2026-07-20.md",
            "archive/week-changelog/2026-07-20.md",
        ],
        repo,
    )

    # Confirm git itself paired this into a rename, not a D+A pair -- the
    # precondition this test exists to cover.
    name_status = subprocess.run(
        ["git", "diff", "--cached", "--name-status", "-M"],
        cwd=str(repo), capture_output=True, text=True, check=True,
    ).stdout
    assert name_status.startswith("R")

    msg = (
        "archive changelog\n"
        "\n"
        "Deleted (Step 2.67):\n"
        "week-changelog/2026-07-20.md\n"
        "--- end Step 2.67 blocks ---\n"
    )
    gate_paths = ["week-changelog/2026-07-20.md", "archive/week-changelog/2026-07-20.md"]
    outcome = deletion_block_gate(msg, gate_paths=gate_paths, cwd=repo)
    assert outcome.passed is True
    assert outcome.diagnostics == []


def test_deletion_gate_assertion_3_unrelated_rename_still_needs_no_block(tmp_path):
    """The widened Assertion-1 set must NOT leak into Assertion-3 (F3): an
    ordinary content-preserving rename, staged with no Deleted claim at all
    in the message, must still pass -- exactly as it did before this fix
    (see `commit_gates` module docstring's negative-spec, "Rename lines are
    intentionally excluded", which stays true for Assertion-3)."""
    repo = _init_repo(tmp_path)
    content = "content block\n" * 40
    _seed_file(repo, "a.md", content)
    _git(["add", "-A"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)

    (repo / "a.md").unlink()
    (repo / "b.md").write_text(content, encoding="utf-8")
    _git(["add", "-A"], repo)

    msg = "rename a.md to b.md\n"
    outcome = deletion_block_gate(msg, gate_paths=["a.md", "b.md"], cwd=repo)
    assert outcome.passed is True
    assert outcome.diagnostics == []


# ---------------------------------------------------------------------------
# deletion_block_gate -- assertion (e): scoped F3, sibling deletion outside
# gate_paths never trips the gate
# ---------------------------------------------------------------------------


def test_deletion_gate_assertion_e_sibling_deletion_outside_scope_not_tripped(tmp_path):
    repo = _init_repo(tmp_path)
    sibling_del_file = "tasks/sibling-session/scratch.md"
    e_commit_file = "state/lessons/e-fixture.yaml"

    _seed_file(repo, sibling_del_file, "sibling scratch")
    _seed_file(repo, e_commit_file, "lesson content")
    _git(["add", "--", sibling_del_file, e_commit_file], repo)
    _git(["commit", "-q", "-m", "seed: e-fixture files"], repo)

    _git(["rm", "-q", sibling_del_file], repo)
    _seed_file(repo, e_commit_file, "updated lesson")
    _git(["add", "--", e_commit_file], repo)

    # No Step 2.67 block at all (subject-only session) -- the message the
    # deleted parity test's wsc-commit.sh caller would compose when only
    # --subject is passed.
    msg = "workstream-complete: e-fixture\n"

    outcome = deletion_block_gate(msg, gate_paths=[e_commit_file], cwd=repo)
    assert outcome.passed is True
    assert outcome.diagnostics == []


# ---------------------------------------------------------------------------
# deletion_block_gate -- native unit additions
# ---------------------------------------------------------------------------


def test_deletion_gate_skip_when_empty_gate_paths_and_no_block(tmp_path):
    repo = _init_repo(tmp_path)
    _seed_file(repo, "README.md", "x")
    _git(["add", "--", "README.md"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)

    outcome = deletion_block_gate("subject only\n", gate_paths=[], cwd=repo)
    assert outcome.passed is True
    assert outcome.skipped is True


def test_deletion_gate_kept_claim_missing_is_mismatch(tmp_path):
    repo = _init_repo(tmp_path)
    _seed_file(repo, "README.md", "x")
    _git(["add", "--", "README.md"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)

    msg = (
        "subject\n"
        "\n"
        "Kept (Step 2.67):\n"
        f"nowhere/ghost.md{_EM_DASH}never existed\n"
        "--- end Step 2.67 blocks ---\n"
    )
    outcome = deletion_block_gate(msg, gate_paths=["nowhere/ghost.md"], cwd=repo)
    assert outcome.passed is False
    assert any("does not exist at HEAD or in staged set" in d for d in outcome.diagnostics)


def test_deletion_gate_malformed_kept_line_is_mismatch(tmp_path):
    repo = _init_repo(tmp_path)
    _seed_file(repo, "README.md", "x")
    _git(["add", "--", "README.md"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)

    msg = (
        "subject\n"
        "\n"
        "Kept (Step 2.67):\n"
        "README.md\n"
        "--- end Step 2.67 blocks ---\n"
    )
    outcome = deletion_block_gate(msg, gate_paths=["README.md"], cwd=repo)
    assert outcome.passed is False
    assert any("no em-dash separator" in d for d in outcome.diagnostics)


def test_deletion_gate_f3_inverse_check_trips_on_own_unblocked_deletion(tmp_path):
    repo = _init_repo(tmp_path)
    _seed_file(repo, "a/gone.md", "x")
    _git(["add", "--", "a/gone.md"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)
    _git(["rm", "-q", "a/gone.md"], repo)

    outcome = deletion_block_gate("subject only\n", gate_paths=["a/gone.md"], cwd=repo)
    assert outcome.passed is False
    assert any("no Step 2.67 block" in d for d in outcome.diagnostics)


# ---------------------------------------------------------------------------
# dirty_tree_gate -- porcelain classification
# ---------------------------------------------------------------------------


def test_dirty_tree_gate_staged_path_skipped(tmp_path):
    repo = _init_repo(tmp_path)
    _seed_file(repo, "README.md", "x")
    _git(["add", "--", "README.md"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)

    _seed_file(repo, "new.txt", "new content")
    _git(["add", "--", "new.txt"], repo)

    outcome = dirty_tree_gate(repo)
    assert outcome.passed is True
    assert outcome.unattributable == []


def test_dirty_tree_gate_untracked_unattributable(tmp_path):
    repo = _init_repo(tmp_path)
    _seed_file(repo, "README.md", "x")
    _git(["add", "--", "README.md"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)

    _seed_file(repo, "orphan.txt", "unstaged orphan")

    outcome = dirty_tree_gate(repo)
    assert outcome.passed is False
    assert outcome.unattributable == ["orphan.txt"]


def test_dirty_tree_gate_eol_phantom_skipped(tmp_path):
    repo = _init_repo(tmp_path)
    _seed_file(repo, "phantom.txt", "same content")
    _git(["add", "--", "phantom.txt"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)

    # Re-write identical content -- a tracked-unstaged worktree entry whose
    # `git diff --quiet` exits 0 (content equals index): the EOL-phantom case.
    (repo / "phantom.txt").write_text("same content", encoding="utf-8")

    outcome = dirty_tree_gate(repo)
    assert outcome.passed is True
    assert outcome.unattributable == []


def test_dirty_tree_gate_known_concurrent_owner_skipped(tmp_path):
    repo = _init_repo(tmp_path)
    _seed_file(repo, "README.md", "x")
    # Seed both directories as tracked (a placeholder each) so git status
    # reports individual file paths below rather than collapsing an
    # entirely-untracked directory into a single "dirname/" porcelain line.
    _seed_file(repo, "peers/.keep", "x")
    _seed_file(repo, "state/handoffs/.keep", "x")
    _git(["add", "--", "README.md", "peers/.keep", "state/handoffs/.keep"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)

    handoff = repo / "state" / "handoffs" / "2026-07-16_120000_peer.md"
    handoff.write_text(
        "---\n"
        "status: open\n"
        "claimed_by: peer-session-id\n"
        "scope:\n"
        "  - peers/owned-file.txt\n"
        "category: workstream\n"
        "---\n"
        "\n# peer handoff\n",
        encoding="utf-8",
    )
    _git(["add", "--", "state/handoffs/2026-07-16_120000_peer.md"], repo)
    _git(["commit", "-q", "-m", "seed: peer handoff"], repo)

    _seed_file(repo, "peers/owned-file.txt", "peer-owned unstaged content")
    _seed_file(repo, "orphan.txt", "no owner")

    outcome = dirty_tree_gate(repo)
    # peers/owned-file.txt is skipped (case b, in scope of a claimed
    # handoff); orphan.txt has no attributable owner (case c).
    assert outcome.passed is False
    assert "peers/owned-file.txt" not in outcome.unattributable
    assert outcome.unattributable == ["orphan.txt"]


def test_dirty_tree_gate_rename_destination_only(tmp_path):
    repo = _init_repo(tmp_path)
    _seed_file(repo, "old-name.txt", "renameable content that is long enough to be detected")
    _git(["add", "--", "old-name.txt"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)

    _git(["mv", "old-name.txt", "new-name.txt"], repo)

    outcome = dirty_tree_gate(repo)
    assert outcome.passed is True
    assert outcome.unattributable == []


# ---------------------------------------------------------------------------
# dirty_tree_gate -- gate_paths scoping (2026-07-22: sibling repo's
# ceremony.wsc_tail dogfood run on a shared branch reported ~33 unattributable
# peer paths, none inside the caller's own stage_paths -- see module
# docstring/negative-spec for the full incident)
# ---------------------------------------------------------------------------


def test_dirty_tree_gate_peer_path_outside_scope_not_tripped(tmp_path):
    repo = _init_repo(tmp_path)
    _seed_file(repo, "README.md", "x")
    _git(["add", "--", "README.md"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)

    # A peer session's in-flight file, no owning handoff -- the exact shape
    # the 2026-07-22 incident reported: unattributable, but outside this
    # caller's own gate_paths.
    _seed_file(repo, "peer-session-file.txt", "peer's in-flight work")

    outcome = dirty_tree_gate(repo, gate_paths=["tasks/my-feature/todo.md"])
    assert outcome.passed is True
    assert outcome.unattributable == []


def test_dirty_tree_gate_in_scope_path_still_tripped_fail_closed_guard(tmp_path):
    """NOT a demonstration of a live signal at today's single call site --
    `commit_pipeline.run_commit_pipeline` can never actually produce a
    `gate_paths` entry that reaches case (c) here, because every path it
    stages is caught by case (a) first and the one path shape that CAN
    reach case (c) (an unstaged `deleted_paths` claim) is already blocked
    by `deletion_block_gate` Assertion-1 with a better diagnostic -- see
    module negative-spec. This test guards the FAIL-CLOSED property itself
    (scoping narrows, it does not blind) against a future widening of
    `compute_gate_paths` or a future caller hand-building `gate_paths`.
    """
    repo = _init_repo(tmp_path)
    _seed_file(repo, "README.md", "x")
    # Seed the directory as tracked (a placeholder) so git status reports the
    # individual file path below rather than collapsing an entirely-untracked
    # directory into a single "dirname/" porcelain line.
    _seed_file(repo, "tasks/my-feature/.keep", "x")
    _git(["add", "--", "README.md", "tasks/my-feature/.keep"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)

    _seed_file(repo, "tasks/my-feature/forgot-to-stage.md", "content")
    _seed_file(repo, "peer-session-file.txt", "peer's in-flight work")

    outcome = dirty_tree_gate(repo, gate_paths=["tasks/my-feature/forgot-to-stage.md"])
    assert outcome.passed is False
    assert outcome.unattributable == ["tasks/my-feature/forgot-to-stage.md"]


def test_dirty_tree_gate_none_gate_paths_preserves_unfiltered_behaviour(tmp_path):
    """`gate_paths=None` (the default) -- unfiltered, matching the original
    pre-2026-07-22 behaviour and the unscoped `ops.dirty_tree_gate` CLI
    trampoline's shape.
    """
    repo = _init_repo(tmp_path)
    _seed_file(repo, "README.md", "x")
    _git(["add", "--", "README.md"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)

    _seed_file(repo, "orphan.txt", "no owner")

    outcome = dirty_tree_gate(repo, gate_paths=None)
    assert outcome.passed is False
    assert outcome.unattributable == ["orphan.txt"]


def test_dirty_tree_gate_empty_gate_paths_scopes_to_nothing_and_passes(tmp_path):
    """`gate_paths=[]` (empty, non-None) is DELIBERATELY NOT the same as
    `None` -- it scopes to nothing, so every otherwise-unattributable path
    is excluded and the gate always passes. This is the sentinel
    correction that closes the original 2026-07-22 incident all the way:
    `commit_pipeline.compute_gate_paths` legitimately returns `[]` (not
    `None`) whenever a caller supplies empty `stage_paths`/`deleted_paths`
    -- exactly a `/workstream-complete` invocation with no local changes of
    its own on a shared branch.
    """
    repo = _init_repo(tmp_path)
    _seed_file(repo, "README.md", "x")
    _git(["add", "--", "README.md"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)

    _seed_file(repo, "orphan.txt", "no owner")

    outcome = dirty_tree_gate(repo, gate_paths=[])
    assert outcome.passed is True
    assert outcome.unattributable == []

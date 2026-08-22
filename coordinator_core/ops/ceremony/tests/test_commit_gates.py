"""
coordinator_core.ops.ceremony.tests.test_commit_gates

Tests for commit_gates.py -- the native ports of the deleted
`check-workstream-complete-deletion-blocks.sh` and `dirty-tree-gate.sh`
(the C3 chunk of the `wsc_tail` rebuild,
docs/plans/2026-07-16-wsc-pure-python-tail-rebuild.md).

Coverage (parity-oracle assertions, per the deleted
`tests/wsc-asic/test-wsc-commit-parity.sh` recovered from
`DoE:85006468^:coordinator/tests/wsc-asic/test-wsc-commit-parity.sh`):
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

Spec backlink: pln-rebuild-the-wsc-commit-ceremon-f7c2a0 § C3 (AC10).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.ceremony import commit_gates as _cg
from coordinator_core.ops.ceremony.commit_gates import (
    DirtyTreeOutcome,
    GateOutcome,
    ParsedBlocks,
    carry_gate,
    deletion_block_gate,
    dirty_tree_gate,
    has_step267_block,
    op_scope_coverage_gate,
    parse_step267_blocks,
)
from coordinator_core.ops.ceremony.git_native import status_porcelain as _status_porcelain

# Real-git spawn is load-bearing: dirty_tree_gate classifies real porcelain
# output (staged/EOL-phantom/rename-destination-only), which a mocked git
# cannot faithfully reproduce. Per-test repo fixtures since these tests
# mutate the index and HEAD.
pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

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


def _make_conflicted_repo(tmp_path: Path) -> Path:
    """A repo whose index carries an unmerged (stage != 0) entry -- an
    ordinary mid-merge-conflict state, not a malformed index. `read_index`
    raises `IndexParseError` on this by contract (git_state.py:47-50); F1
    fixture repos exercise that this is a live, unresolved-merge condition,
    never a crash."""
    repo = _init_repo(tmp_path)
    _seed_file(repo, "conflict.md", "base\n")
    _git(["add", "--", "conflict.md"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)
    base_branch = subprocess.run(
        ["git", "branch", "--show-current"],
        cwd=str(repo), capture_output=True, text=True, check=True,
    ).stdout.strip()

    _git(["checkout", "-q", "-b", "side"], repo)
    _seed_file(repo, "conflict.md", "side change\n")
    _git(["commit", "-q", "-am", "side change"], repo)

    _git(["checkout", "-q", base_branch], repo)
    _seed_file(repo, "conflict.md", "main change\n")
    _git(["commit", "-q", "-am", "main change"], repo)

    # Left deliberately unresolved -- `git merge` exits non-zero here, which
    # is the point: the index now carries stage-1/2/3 entries for
    # conflict.md, never committed or resolved.
    subprocess.run(["git", "merge", "-q", "side"], cwd=str(repo), capture_output=True, text=True)
    return repo


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


def test_deletion_gate_unmerged_index_entry_refuses_not_crashes(tmp_path):
    """F1 (code-review, P1): `read_index` raises `IndexParseError` on ANY
    unmerged (stage != 0) index entry -- an ordinary mid-merge-conflict repo
    state. Assertion-2's index read (only reached when the message carries a
    Kept-claim) must degrade to a refusal, never propagate the raise up
    through `commit_pipeline.commit()` and crash the op."""
    repo = _make_conflicted_repo(tmp_path)

    msg = (
        "subject\n"
        "\n"
        "Kept (Step 2.67):\n"
        f"conflict.md{_EM_DASH}still needed\n"
        "--- end Step 2.67 blocks ---\n"
    )
    outcome = deletion_block_gate(msg, gate_paths=["conflict.md"], cwd=repo)
    assert outcome.passed is False
    assert any("staged index unreadable" in d for d in outcome.diagnostics)


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


def test_dirty_tree_gate_unmerged_index_entry_refuses_not_crashes(tmp_path):
    """F1 (code-review, P1): `read_index` raises `IndexParseError` on ANY
    unmerged (stage != 0) index entry -- an ordinary mid-merge-conflict repo
    state. This gate's read is unconditional (unlike deletion_block_gate's
    Kept-claim-gated read), so this is the more directly reachable of the
    two sites. Must degrade to the gate's own "unattributable" verdict, never
    propagate the raise and crash the commit op."""
    repo = _make_conflicted_repo(tmp_path)

    outcome = dirty_tree_gate(repo)
    assert outcome.passed is False
    assert len(outcome.unattributable) == 1
    assert "index unreadable" in outcome.unattributable[0]


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


def test_dirty_tree_gate_batched_phantom_filter_preserves_per_path_resolution(tmp_path):
    """C1 (docs/plans/2026-08-07-n-plus-one-git-spawn-class-and-
    amplification-gate.md): the EOL-phantom filter now runs ONE batched
    `git diff --name-only` over every tracked-unstaged candidate instead of
    one `git diff --quiet` per porcelain line. This pins that the batch
    still resolves per-path correctly: a real edit right next to a
    same-content rewrite (phantom) in the SAME batch must not cross-
    contaminate -- the real edit stays unattributable, the phantom stays
    skipped."""
    repo = _init_repo(tmp_path)
    _seed_file(repo, "phantom.txt", "same content")
    _seed_file(repo, "real-edit.txt", "original content")
    _git(["add", "--", "phantom.txt", "real-edit.txt"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)

    # phantom.txt: rewritten with IDENTICAL content -- a phantom, absent from
    # `git diff --name-only`'s output.
    (repo / "phantom.txt").write_text("same content", encoding="utf-8")
    # real-edit.txt: genuinely changed -- present in `git diff --name-only`'s
    # output, and has no attributable owner.
    (repo / "real-edit.txt").write_text("changed content", encoding="utf-8")

    outcome = dirty_tree_gate(repo)
    assert outcome.passed is False
    assert outcome.unattributable == ["real-edit.txt"]


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


# ---------------------------------------------------------------------------
# carry_gate
# ---------------------------------------------------------------------------


def _seed_handoff(repo: Path, rel_path: str, frontmatter_body: str) -> None:
    _seed_file(
        repo,
        rel_path,
        f"---\nstatus: open\n{frontmatter_body}---\n\n# handoff\n",
    )


def test_carry_gate_skips_when_no_handoff_in_gate_paths(tmp_path):
    """AC5: an empty filtered set (no `state/handoffs/*.md` in `gate_paths`)
    skips entirely -- no file read at all."""
    repo = _init_repo(tmp_path)
    outcome = carry_gate(repo, gate_paths=["a/one.md", "state/other/x.md"])
    assert outcome == GateOutcome(passed=True, skipped=True, diagnostics=[])


def test_carry_gate_empty_gate_paths_skips(tmp_path):
    repo = _init_repo(tmp_path)
    outcome = carry_gate(repo, gate_paths=[])
    assert outcome == GateOutcome(passed=True, skipped=True, diagnostics=[])


def test_carry_gate_well_formed_carried_items_passes(tmp_path):
    """AC4: well-formed carried_items (non-terminal `carried`, plus a
    terminal `closed` WITH a disposition_detail) commits normally."""
    repo = _init_repo(tmp_path)
    _seed_handoff(
        repo,
        "state/handoffs/2026-08-10-x.md",
        "carried_items:\n"
        "  - carry_id: c1\n"
        "    description: still open\n"
        "    disposition: carried\n"
        "  - carry_id: c2\n"
        "    description: done\n"
        "    disposition: closed\n"
        "    disposition_detail: closed -- superseded by c3\n",
    )
    outcome = carry_gate(repo, gate_paths=["state/handoffs/2026-08-10-x.md"])
    assert outcome == GateOutcome(passed=True, skipped=False, diagnostics=[])


def test_carry_gate_absent_carried_items_key_passes(tmp_path):
    """AC4: a handoff with NO `carried_items` key at all is a legitimate
    green -- absence is not the vacuous-pass hazard named in the plan's
    Anti-scope (that hazard concerned authoring-time validation with no
    staged-path precondition; this gate only ever fires on a staged
    handoff)."""
    repo = _init_repo(tmp_path)
    _seed_handoff(repo, "state/handoffs/2026-08-10-y.md", "")
    outcome = carry_gate(repo, gate_paths=["state/handoffs/2026-08-10-y.md"])
    assert outcome == GateOutcome(passed=True, skipped=False, diagnostics=[])


def test_carry_gate_terminal_disposition_missing_detail_refuses(tmp_path):
    """AC1: a terminal disposition (`blocked`) with no disposition_detail is
    a REFUSAL."""
    repo = _init_repo(tmp_path)
    _seed_handoff(
        repo,
        "state/handoffs/2026-08-10-z.md",
        "carried_items:\n"
        "  - carry_id: c1\n"
        "    description: stuck\n"
        "    disposition: blocked\n",
    )
    outcome = carry_gate(repo, gate_paths=["state/handoffs/2026-08-10-z.md"])
    assert outcome.passed is False
    assert outcome.skipped is False
    assert any("disposition_detail" in line for line in outcome.diagnostics)


def test_carry_gate_missing_carry_id_refuses(tmp_path):
    """AC2: a missing carry_id refuses, delegating to evaluate_gate rather
    than re-implementing the rule."""
    repo = _init_repo(tmp_path)
    _seed_handoff(
        repo,
        "state/handoffs/2026-08-10-a.md",
        "carried_items:\n"
        "  - description: no id\n"
        "    disposition: carried\n",
    )
    outcome = carry_gate(repo, gate_paths=["state/handoffs/2026-08-10-a.md"])
    assert outcome.passed is False
    assert any("carry_id" in line for line in outcome.diagnostics)


def test_carry_gate_unrecognized_disposition_refuses(tmp_path):
    """AC2: a disposition outside the sanctioned set refuses."""
    repo = _init_repo(tmp_path)
    _seed_handoff(
        repo,
        "state/handoffs/2026-08-10-b.md",
        "carried_items:\n"
        "  - carry_id: c1\n"
        "    description: weird\n"
        "    disposition: not_a_real_disposition\n",
    )
    outcome = carry_gate(repo, gate_paths=["state/handoffs/2026-08-10-b.md"])
    assert outcome.passed is False
    assert any("not_a_real_disposition" in line for line in outcome.diagnostics)


def test_carry_gate_preserves_violation_lines_verbatim(tmp_path):
    """AC3: `evaluate_gate`'s own violation text reaches `diagnostics`
    unchanged, prefixed only with the handoff path -- no re-wording."""
    from coordinator_core.ops.handoff_carry_gate import evaluate_gate

    items = [{"description": "no id", "disposition": "carried"}]
    expected_violation = evaluate_gate(items).violations[0]

    repo = _init_repo(tmp_path)
    _seed_handoff(
        repo,
        "state/handoffs/2026-08-10-c.md",
        "carried_items:\n"
        "  - description: no id\n"
        "    disposition: carried\n",
    )
    outcome = carry_gate(repo, gate_paths=["state/handoffs/2026-08-10-c.md"])
    assert f"state/handoffs/2026-08-10-c.md: {expected_violation}" in outcome.diagnostics


def test_carry_gate_refusal_includes_restage_hint(tmp_path):
    """AC6: a refusal's diagnostics tell the operator the path is left
    unstaged and must be re-staged after the fix."""
    repo = _init_repo(tmp_path)
    _seed_handoff(
        repo,
        "state/handoffs/2026-08-10-d.md",
        "carried_items:\n"
        "  - carry_id: c1\n"
        "    disposition: blocked\n",
    )
    outcome = carry_gate(repo, gate_paths=["state/handoffs/2026-08-10-d.md"])
    assert outcome.passed is False
    assert any("unstaged" in line.lower() for line in outcome.diagnostics)


def test_carry_gate_pass_has_no_restage_hint(tmp_path):
    repo = _init_repo(tmp_path)
    _seed_handoff(repo, "state/handoffs/2026-08-10-e.md", "")
    outcome = carry_gate(repo, gate_paths=["state/handoffs/2026-08-10-e.md"])
    assert outcome.diagnostics == []


def test_carry_gate_unreadable_handoff_refuses(tmp_path):
    """AC7: a staged handoff path that EXISTS on disk but cannot be read
    (here: the path is a directory, not a readable file) is a REFUSAL, not
    a silent pass -- fail-loud matches `evaluate_gate`'s own never-fail-open
    contract. Deliberately NOT a missing path -- see
    `test_carry_gate_staged_deletion_path_absent_from_worktree_passes`
    below (AC8) for why an ABSENT path is a different case (a legitimate
    skip, not this refusal) that must not be conflated with this one."""
    repo = _init_repo(tmp_path)
    handoff_as_dir = repo / "state" / "handoffs" / "2026-08-10-unreadable.md"
    handoff_as_dir.mkdir(parents=True)
    outcome = carry_gate(repo, gate_paths=["state/handoffs/2026-08-10-unreadable.md"])
    assert outcome.passed is False
    assert outcome.skipped is False
    assert any("could not read handoff" in line for line in outcome.diagnostics)


def test_carry_gate_staged_deletion_path_absent_from_worktree_passes(tmp_path):
    """AC8: a `gate_paths` entry ABSENT from the worktree is the staged-
    deletion signal, not a generic missing-file case -- `compute_gate_paths`
    (`commit_message.py`) returns `[*commit_paths, *deleted_paths]`, so an
    EM-authored `deleted_paths` entry reaches `gate_paths` with no file
    behind it BY DESIGN (a `git rm state/handoffs/*.md`, e.g. `/distill`
    disposal). Refusing here would make deliberate handoff deletion
    impossible tree-wide -- there is nothing to carry-check about a file
    being removed, so this is a legitimate pass, never a refusal. Must key
    on the path genuinely not existing (checked BEFORE
    `read_carried_items` runs), never on catching the `OSError`
    `read_carried_items` itself would raise -- that would also swallow a
    genuinely unreadable EXISTING file (see the AC7 test above) and
    re-open AC7."""
    repo = _init_repo(tmp_path)
    outcome = carry_gate(repo, gate_paths=["state/handoffs/2026-08-10-deleted.md"])
    assert outcome.passed is True
    assert outcome.diagnostics == []


def test_carry_gate_unparseable_frontmatter_refuses(tmp_path):
    """AC7: a handoff with no parseable YAML frontmatter block is a
    REFUSAL."""
    repo = _init_repo(tmp_path)
    _seed_file(repo, "state/handoffs/2026-08-10-f.md", "no frontmatter here\n")
    outcome = carry_gate(repo, gate_paths=["state/handoffs/2026-08-10-f.md"])
    assert outcome.passed is False
    assert any("unparseable carried_items" in line for line in outcome.diagnostics)


def test_carry_gate_only_filters_handoff_paths(tmp_path):
    """Non-handoff paths in `gate_paths` are ignored -- only
    `state/handoffs/*.md` entries are read."""
    repo = _init_repo(tmp_path)
    _seed_handoff(repo, "state/handoffs/2026-08-10-g.md", "")
    outcome = carry_gate(
        repo,
        gate_paths=["some/other/file.py", "state/handoffs/2026-08-10-g.md"],
    )
    assert outcome == GateOutcome(passed=True, skipped=False, diagnostics=[])


# ---------------------------------------------------------------------------
# op_scope_coverage_gate
# ---------------------------------------------------------------------------

_REGISTRY_RELPATH = "coordinator_core/ops/_registry_map.py"
_OP_SCOPES_RELPATH = "coordinator_core/op_scopes.py"


def _seed_registry_map(repo: Path, op_names, *, valid: bool = True) -> None:
    p = repo / _REGISTRY_RELPATH
    p.parent.mkdir(parents=True, exist_ok=True)
    if valid:
        entries = "\n".join(f'    "{name}": "some.module",' for name in op_names)
        p.write_text(
            "from typing import Dict\n\n"
            f"OP_MODULE_MAP: Dict[str, str] = {{\n{entries}\n}}\n",
            encoding="utf-8",
        )
    else:
        # A dict spelled via a name that isn't OP_MODULE_MAP, so the target
        # binding is genuinely absent -- simulates a rename/restructure.
        p.write_text("SOME_OTHER_NAME = {}\n", encoding="utf-8")


def _seed_op_scopes(repo: Path, op_scope_pairs, *, valid: bool = True) -> None:
    p = repo / _OP_SCOPES_RELPATH
    p.parent.mkdir(parents=True, exist_ok=True)
    if valid:
        entries = "\n".join(f'    "{name}": "{scope}",' for name, scope in op_scope_pairs)
        p.write_text(
            "from typing import Dict\n\n"
            f"_OP_KEY_SCOPE: Dict[str, str] = {{\n{entries}\n}}\n",
            encoding="utf-8",
        )
    else:
        p.write_text("SOME_OTHER_NAME = {}\n", encoding="utf-8")


def test_op_scope_gate_skips_when_registry_map_not_staged(tmp_path):
    """Scope filter: `_registry_map.py` absent from `gate_paths` -> skip, no
    file read at all -- a commit that doesn't touch the registry has nothing
    to say."""
    repo = _init_repo(tmp_path)
    outcome = op_scope_coverage_gate(repo, gate_paths=["some/other/file.py"])
    assert outcome == GateOutcome(passed=True, skipped=True, diagnostics=[])


def test_op_scope_gate_refuses_unclassified_op(tmp_path):
    """Refuse: an op registered in OP_MODULE_MAP with no _OP_KEY_SCOPE entry."""
    repo = _init_repo(tmp_path)
    _seed_registry_map(repo, ["known.op", "unclassified.op"])
    _seed_op_scopes(repo, [("known.op", "none")])
    outcome = op_scope_coverage_gate(repo, gate_paths=[_REGISTRY_RELPATH])
    assert outcome.passed is False
    assert outcome.skipped is False
    assert any("unclassified.op" in line for line in outcome.diagnostics)
    assert not any("known.op" in line for line in outcome.diagnostics)


def test_op_scope_gate_passes_when_all_classified(tmp_path):
    """Pass: every registered op has an _OP_KEY_SCOPE entry -- a scope-table
    entry with no registry counterpart (legacy/test-only) does not trip the
    gate; direction is one-way."""
    repo = _init_repo(tmp_path)
    _seed_registry_map(repo, ["op.a", "op.b"])
    _seed_op_scopes(
        repo,
        [("op.a", "none"), ("op.b", "common_dir"), ("legacy.only.in.scope.table", "none")],
    )
    outcome = op_scope_coverage_gate(repo, gate_paths=[_REGISTRY_RELPATH])
    assert outcome == GateOutcome(passed=True, skipped=False, diagnostics=[])


def test_op_scope_gate_absence_class_registry_map_deleted_skips(tmp_path):
    """Absence class 1: `_registry_map.py` staged in gate_paths but ABSENT from
    the worktree (a staged deletion) -> skip, not refuse."""
    repo = _init_repo(tmp_path)
    # Never write the file at all -- mirrors a staged deletion where the
    # worktree copy is already gone by gate time.
    outcome = op_scope_coverage_gate(repo, gate_paths=[_REGISTRY_RELPATH])
    assert outcome == GateOutcome(passed=True, skipped=True, diagnostics=[])


def test_op_scope_gate_absence_class_op_scopes_missing_refuses(tmp_path):
    """Absence class 2: `op_scopes.py` absent from the worktree -> refuse,
    never a silent pass -- the gate cannot verify coverage without it."""
    repo = _init_repo(tmp_path)
    _seed_registry_map(repo, ["op.a"])
    # op_scopes.py deliberately never written.
    outcome = op_scope_coverage_gate(repo, gate_paths=[_REGISTRY_RELPATH])
    assert outcome.passed is False
    assert outcome.skipped is False
    assert any(_OP_SCOPES_RELPATH in line for line in outcome.diagnostics)


def test_op_scope_gate_absence_class_dict_not_found_in_registry_refuses(tmp_path):
    """Absence class 3a: OP_MODULE_MAP not found by the AST walk (renamed/
    restructured) -> refuse, naming which dict was not found -- a parse
    failure must never read as 'no violations'."""
    repo = _init_repo(tmp_path)
    _seed_registry_map(repo, [], valid=False)
    _seed_op_scopes(repo, [("op.a", "none")])
    outcome = op_scope_coverage_gate(repo, gate_paths=[_REGISTRY_RELPATH])
    assert outcome.passed is False
    assert outcome.skipped is False
    assert any("OP_MODULE_MAP" in line for line in outcome.diagnostics)


def test_op_scope_gate_absence_class_dict_not_found_in_scopes_refuses(tmp_path):
    """Absence class 3b: _OP_KEY_SCOPE not found by the AST walk -> refuse,
    naming which dict was not found."""
    repo = _init_repo(tmp_path)
    _seed_registry_map(repo, ["op.a"])
    _seed_op_scopes(repo, [], valid=False)
    outcome = op_scope_coverage_gate(repo, gate_paths=[_REGISTRY_RELPATH])
    assert outcome.passed is False
    assert outcome.skipped is False
    assert any("_OP_KEY_SCOPE" in line for line in outcome.diagnostics)


def test_op_scope_gate_refuses_on_double_module_level_rebind(tmp_path):
    """P2 hardening: two module-level rebinds of OP_MODULE_MAP (e.g. a
    placeholder `= {}` followed by a later genuine `= {...}` rebind) -> the
    gate cannot know which binding is authoritative and must refuse, naming
    the variable, rather than silently reading the first one `ast.walk`
    reaches."""
    repo = _init_repo(tmp_path)
    p = repo / _REGISTRY_RELPATH
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        "from typing import Dict\n\n"
        "OP_MODULE_MAP: Dict[str, str] = {}\n\n"
        'OP_MODULE_MAP = {"op.a": "some.module"}\n',
        encoding="utf-8",
    )
    _seed_op_scopes(repo, [("op.a", "none")])
    outcome = op_scope_coverage_gate(repo, gate_paths=[_REGISTRY_RELPATH])
    assert outcome.passed is False
    assert outcome.skipped is False
    assert any("OP_MODULE_MAP" in line and "more than once" in line for line in outcome.diagnostics)


def test_op_scope_gate_same_named_local_does_not_count_as_second_binding(tmp_path):
    """A same-named local variable inside a function is NOT a module-level
    binding and must not trip the multiplicity refusal -- only `tree.body`
    (module-level statements) counts."""
    repo = _init_repo(tmp_path)
    p = repo / _REGISTRY_RELPATH
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        "from typing import Dict\n\n"
        'OP_MODULE_MAP: Dict[str, str] = {"op.a": "some.module"}\n\n'
        "def _helper():\n"
        "    OP_MODULE_MAP = {}\n"
        "    return OP_MODULE_MAP\n",
        encoding="utf-8",
    )
    _seed_op_scopes(repo, [("op.a", "none")])
    outcome = op_scope_coverage_gate(repo, gate_paths=[_REGISTRY_RELPATH])
    assert outcome == GateOutcome(passed=True, skipped=False, diagnostics=[])


def test_op_scope_gate_op_scopes_present_but_unreadable_refuses(tmp_path, monkeypatch):
    """Present-but-unreadable, not absent: `op_scopes.py` exists on disk but
    raises `OSError` on read (permissions, encoding I/O failure, transient FS
    error) -> refuse, naming the path or the read failure -- a predicate the
    gate cannot evaluate must never fall through to 'no violations'."""
    repo = _init_repo(tmp_path)
    _seed_registry_map(repo, ["op.a"])
    _seed_op_scopes(repo, [("op.a", "none")])

    real_read_text = Path.read_text

    def _flaky_read_text(self, *args, **kwargs):
        if self == repo / _OP_SCOPES_RELPATH:
            raise OSError("simulated unreadable file")
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", _flaky_read_text)

    outcome = op_scope_coverage_gate(repo, gate_paths=[_REGISTRY_RELPATH])
    assert outcome.passed is False
    assert outcome.skipped is False
    assert any(
        _OP_SCOPES_RELPATH in line and "simulated unreadable file" in line
        for line in outcome.diagnostics
    )


def test_op_scope_gate_registry_map_present_but_unreadable_refuses(tmp_path, monkeypatch):
    """Mirrored present-but-unreadable case on the registry-map side:
    `_registry_map.py` exists but raises `OSError` on read -> refuse, naming
    the path or the read failure, same as the op_scopes.py side."""
    repo = _init_repo(tmp_path)
    _seed_registry_map(repo, ["op.a"])

    real_read_text = Path.read_text

    def _flaky_read_text(self, *args, **kwargs):
        if self == repo / _REGISTRY_RELPATH:
            raise OSError("simulated unreadable file")
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", _flaky_read_text)

    outcome = op_scope_coverage_gate(repo, gate_paths=[_REGISTRY_RELPATH])
    assert outcome.passed is False
    assert outcome.skipped is False
    assert any(
        _REGISTRY_RELPATH in line and "simulated unreadable file" in line
        for line in outcome.diagnostics
    )


# ---------------------------------------------------------------------------
# C3 equivalence fixture -- pre-re-point vs post-re-point parity
#
# C3 re-pointed dirty_tree_gate's staged classification and
# deletion_block_gate's Kept-claim (Assertion-2) HEAD-membership leg onto
# `coordinator_core.git.git_state` (read_index / head_blobs) with no `git`
# process, but landed with NO new tests -- the 48 pre-existing tests above
# predate the re-point and were never written to catch it changing an
# answer. This section is that missing equivalence proof: an oracle,
# reconstructed verbatim from the pre-re-point implementation (recovered via
# `git show 69f92af34^:coordinator_core/ops/ceremony/commit_gates.py`), run
# side-by-side with the live gate over every shape the re-point's own
# module docstring calls out as load-bearing (mode-only divergence, content
# divergence, staged/worktree/add/delete, a clean path, a path with a
# space, a non-ASCII path, a CRLF EOL-phantom, a symlink, and a submodule
# gitlink).
#
# The oracle reuses every helper the re-point did NOT touch
# (`_build_known_scope`, `_diff_name_only_worktree`, `_porcelain_path`,
# `parse_step267_blocks`, `_parse_name_status_deletions`,
# `_parse_name_status_rename_sources`) and only re-implements the two legs
# that changed: `dirty_tree_gate`'s X-column staged check (was `git status
# --porcelain`'s own X char), and `deletion_block_gate`'s Kept-claim
# HEAD-membership check (was an unscoped `git ls-tree -r HEAD --name-only`
# walk, not the blob-type-filtered `head_blobs()`).
# ---------------------------------------------------------------------------


def _old_dirty_tree_gate(worktree_root, gate_paths=None) -> DirtyTreeOutcome:
    """Oracle: `dirty_tree_gate` as it read BEFORE C3 -- staged classified by
    `git status --porcelain`'s own X column, not index-vs-HEAD comparison.
    """
    root = Path(worktree_root)
    known_scope = _cg._build_known_scope(root)
    scoped = gate_paths is not None
    gate_scope = set(gate_paths) if gate_paths else set()

    if scoped and not gate_scope:
        return DirtyTreeOutcome(passed=True, unattributable=[])

    status_result = _status_porcelain(root, sorted(gate_scope) if scoped else None)
    parsed_lines = []
    phantom_candidates = []
    for line in status_result.stdout.splitlines():
        if not line:
            continue
        xy = line[:2]
        path = _cg._porcelain_path(line)
        x_char = xy[0] if xy else " "
        parsed_lines.append((x_char, path))
        if x_char == " ":
            phantom_candidates.append(path)

    real_diff_paths = set()
    if phantom_candidates:
        diff_result = _cg._diff_name_only_worktree(root, phantom_candidates)
        real_diff_paths = {p for p in diff_result.stdout.splitlines() if p}

    unattributable = []
    for x_char, path in parsed_lines:
        if x_char not in (" ", "?"):
            continue
        if x_char == " " and path not in real_diff_paths:
            continue
        if path in known_scope:
            continue
        if scoped and path not in gate_scope:
            continue
        unattributable.append(path)

    return DirtyTreeOutcome(passed=not unattributable, unattributable=unattributable)


def _old_deletion_block_gate(
    msg_text: str, gate_paths, *, cwd, whole_index: bool = False
) -> GateOutcome:
    """Oracle: `deletion_block_gate` as it read BEFORE C3 -- Assertion-2's
    HEAD-membership leg was an unscoped `git ls-tree -r HEAD --name-only`
    walk over the WHOLE tree (every path, any object type), not
    `head_blobs()`'s targeted, blob-type-filtered lookup. Assertion-1 and
    Assertion-3 are UNCHANGED by C3 and are reproduced verbatim here only so
    this oracle is a complete, callable stand-in.
    """
    has_block = has_step267_block(msg_text)
    gate_scope = set(gate_paths)

    if not whole_index and not gate_paths and not has_block:
        return GateOutcome(passed=True, skipped=True, diagnostics=[])

    parsed = parse_step267_blocks(msg_text)

    name_status_result = _cg.diff_cached_name_status(cwd, find_renames=True)
    all_staged_deletions = _cg._parse_name_status_deletions(name_status_result.stdout)
    staged_deletions = (
        [p for p in all_staged_deletions if p in gate_scope]
        if gate_scope
        else all_staged_deletions
    )
    staged_deletions_set = set(staged_deletions)

    all_rename_sources = _cg._parse_name_status_rename_sources(name_status_result.stdout)
    rename_sources = (
        [p for p in all_rename_sources if p in gate_scope] if gate_scope else all_rename_sources
    )
    staged_or_renamed_away_set = staged_deletions_set | set(rename_sources)

    staged_all_set = set()
    tracked_at_head = set()
    if parsed.kept_claimed:
        name_only_result = _cg._git(["diff", "--cached", "--name-only"], cwd=cwd)
        all_staged = [p for p in name_only_result.stdout.splitlines() if p]
        staged_all = [p for p in all_staged if p in gate_scope] if gate_scope else all_staged
        staged_all_set = set(staged_all)

        ls_tree_result = _cg._git(["ls-tree", "-r", "HEAD", "--name-only"], cwd=cwd)
        tracked_at_head = {p for p in ls_tree_result.stdout.splitlines() if p}

    diagnostics = []
    for path in parsed.deleted_claimed:
        if path not in staged_or_renamed_away_set:
            diagnostics.append(f"Deleted-claim NOT staged for deletion: {path}")

    for path in parsed.kept_claimed:
        if path not in tracked_at_head and path not in staged_all_set:
            diagnostics.append(f"Kept-claim does not exist at HEAD or in staged set: {path}")

    for raw in parsed.kept_malformed:
        diagnostics.append(
            "Kept-line has no em-dash separator (unparseable, expected "
            f"'<path> — <reason>'): {raw}"
        )

    if staged_deletions and not has_block:
        diagnostics.append("Staged deletions present but commit body has no Step 2.67 block:")
        for path in staged_deletions:
            diagnostics.append(f"  {path}")

    return GateOutcome(passed=not diagnostics, skipped=False, diagnostics=diagnostics)


def _git_stdout(args, cwd) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True
    ).stdout.strip()


# --- dirty_tree_gate shape builders ----------------------------------------


def _shape_mode_toggle(tmp_path):
    """Pure mode-only divergence: `--chmod=+x` on the INDEX entry, no
    worktree write -- this repo runs core.filemode=false, so a worktree
    `chmod` would never reach git's own dirty classification at all."""
    repo = _init_repo(tmp_path)
    _seed_file(repo, "exec-me.sh", "echo hi\n")
    _git(["add", "--", "exec-me.sh"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)
    _git(["update-index", "--chmod=+x", "exec-me.sh"], repo)
    return repo


def _shape_content_divergence(tmp_path):
    repo = _init_repo(tmp_path)
    _seed_file(repo, "diverge.txt", "original\n")
    _git(["add", "--", "diverge.txt"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)
    (repo / "diverge.txt").write_text("changed unstaged\n", encoding="utf-8")
    return repo


def _shape_staged_only(tmp_path):
    repo = _init_repo(tmp_path)
    _seed_file(repo, "staged.txt", "v1\n")
    _git(["add", "--", "staged.txt"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)
    _seed_file(repo, "staged.txt", "v2\n")
    _git(["add", "--", "staged.txt"], repo)
    return repo


def _shape_worktree_only(tmp_path):
    repo = _init_repo(tmp_path)
    _seed_file(repo, "worktree-only.txt", "base\n")
    _git(["add", "--", "worktree-only.txt"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)
    _seed_file(repo, "worktree-only.txt", "edited unstaged\n")
    return repo


def _shape_add(tmp_path):
    repo = _init_repo(tmp_path)
    _seed_file(repo, "README.md", "x")
    _git(["add", "--", "README.md"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)
    _seed_file(repo, "brand-new.txt", "new\n")
    _git(["add", "--", "brand-new.txt"], repo)
    return repo


def _shape_delete(tmp_path):
    repo = _init_repo(tmp_path)
    _seed_file(repo, "to-delete.txt", "bye\n")
    _git(["add", "--", "to-delete.txt"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)
    _git(["rm", "-q", "to-delete.txt"], repo)
    return repo


def _shape_clean(tmp_path):
    repo = _init_repo(tmp_path)
    _seed_file(repo, "clean.txt", "unchanged\n")
    _git(["add", "--", "clean.txt"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)
    return repo


def _shape_space_path(tmp_path):
    repo = _init_repo(tmp_path)
    _seed_file(repo, "a dir/has space.txt", "base\n")
    _git(["add", "--", "a dir/has space.txt"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)
    _seed_file(repo, "a dir/has space.txt", "edited\n")
    return repo


def _shape_nonascii_path(tmp_path):
    repo = _init_repo(tmp_path)
    _seed_file(repo, "café-résumé.txt", "base\n")
    _git(["add", "--", "café-résumé.txt"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)
    _seed_file(repo, "café-résumé.txt", "edited\n")
    return repo


def _shape_crlf_eol_phantom(tmp_path):
    """core.autocrlf=true here -- a naive on-disk hash disagrees with the
    index OID on a measured 24.5% of clean blobs; this is the shape
    `_diff_name_only_worktree`'s EOL-phantom filter exists to suppress."""
    repo = _init_repo(tmp_path)
    _git(["config", "core.autocrlf", "true"], repo)
    content = b"line1\r\nline2\r\n"
    p = repo / "crlf.txt"
    p.write_bytes(content)
    _git(["add", "--", "crlf.txt"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)
    # Re-write byte-identical content -- a tracked-unstaged phantom.
    p.write_bytes(content)
    return repo


def _shape_symlink(tmp_path):
    """A symlink (mode 120000) tracked at HEAD, SYNTHESISED via a direct
    index write -- this repo runs core.symlinks=false, so a worktree
    symlink would never round-trip through git as one."""
    repo = _init_repo(tmp_path)
    _seed_file(repo, "README.md", "x")
    _git(["add", "--", "README.md"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)
    # cacheinfo needs stdin text, not argv -- git hash-object reads stdin.
    proc = subprocess.run(
        ["git", "hash-object", "-w", "--stdin"],
        cwd=str(repo), input="target.txt", capture_output=True, text=True, check=True,
    )
    blob = proc.stdout.strip()
    _git(["update-index", "--add", "--cacheinfo", f"120000,{blob},link-me"], repo)
    _git(["commit", "-q", "-m", "add symlink"], repo)
    return repo


def _shape_gitlink(tmp_path):
    """A submodule gitlink (mode 160000) tracked at HEAD, SYNTHESISED via a
    direct index write -- this repo's own index contains no 160000 entry at
    all to build a fixture from. The pointed-to sha need not be a real
    submodule commit; git's gitlink object never resolves it locally."""
    repo = _init_repo(tmp_path)
    _seed_file(repo, "README.md", "x")
    _git(["add", "--", "README.md"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)
    head_sha = _git_stdout(["rev-parse", "HEAD"], repo)
    _git(["update-index", "--add", "--cacheinfo", f"160000,{head_sha},vendor/sub"], repo)
    _git(["commit", "-q", "-m", "add gitlink"], repo)
    return repo


_DIRTY_TREE_SHAPES = [
    pytest.param(_shape_mode_toggle, id="mode_toggle_chmod"),
    pytest.param(_shape_content_divergence, id="content_divergence"),
    pytest.param(_shape_staged_only, id="staged_only"),
    pytest.param(_shape_worktree_only, id="worktree_only"),
    pytest.param(_shape_add, id="add"),
    pytest.param(_shape_delete, id="delete"),
    pytest.param(_shape_clean, id="clean_path"),
    pytest.param(_shape_space_path, id="path_with_space"),
    pytest.param(_shape_nonascii_path, id="nonascii_path"),
    pytest.param(_shape_crlf_eol_phantom, id="crlf_eol_phantom"),
    pytest.param(_shape_symlink, id="symlink_120000"),
    pytest.param(_shape_gitlink, id="gitlink_160000"),
]


#: KNOWN, REPORTED DIVERGENCE (executor report, REGRESSION-2 fix): the
#: pre-re-point oracle keys `_diff_name_only_worktree`'s EOL-phantom lookup
#: on `git status --porcelain`'s own C-QUOTED path (`"a dir/has space.txt"`)
#: -- that quoted literal never matches the real (unquoted) worktree path as
#: a `git diff --name-only` pathspec, so the oracle's `real_diff_paths`
#: lookup misses and the genuinely dirty space-containing path is silently
#: swallowed as an EOL phantom (passed). The live gate (now reading `-z`,
#: unquoted) resolves the same lookup correctly and reports the path
#: unattributable -- a CORRECT answer, not bug-for-bug parity with the buggy
#: oracle. Only `path_with_space` hits this in practice: `nonascii_path`'s
#: C-quoted, octal-escaped literal (`"caf\303\251..."`) does NOT reproduce
#: the same miss when fed to `git diff --name-only` as a pathspec -- both
#: live and oracle agree there (verified by a real run, not assumed; the
#: mechanism is presumably git's own pathspec parser recognising and
#: unquoting the C-quoted literal, unlike a plain-text name-list compare) --
#: so `nonascii_path` is deliberately absent from this set.
_DIRTY_TREE_KNOWN_ORACLE_QUOTING_BUG_IDS = {"path_with_space"}


@pytest.mark.parametrize("shape", _DIRTY_TREE_SHAPES)
def test_dirty_tree_gate_matches_pre_c3_oracle(tmp_path, shape, request):
    """AC (undischarged by C3): `dirty_tree_gate`'s re-pointed staged
    classification returns a SET-IDENTICAL answer to the pre-re-point
    (`git status --porcelain` X-column) implementation, across every shape
    named in the C3 dispatch brief.

    EXCEPT `_DIRTY_TREE_KNOWN_ORACLE_QUOTING_BUG_IDS` -- see that constant's
    docstring: the oracle itself misclassifies a quoted dirty path as a
    phantom, and the live gate's more-correct answer is asserted directly
    instead of parity with that bug.
    """
    case_id = request.node.callspec.id
    repo = shape(tmp_path)
    live = dirty_tree_gate(repo)
    oracle = _old_dirty_tree_gate(repo)

    if case_id in _DIRTY_TREE_KNOWN_ORACLE_QUOTING_BUG_IDS:
        assert oracle.unattributable == [], (
            f"oracle={oracle!r} -- expected the oracle's C-quoted lookup to "
            "keep silently swallowing this path as a phantom; if this now "
            "fails, the oracle's own bug may have been fixed elsewhere and "
            "this special-case should be removed"
        )
        assert live.unattributable != [], (
            f"live={live!r} -- expected the live (unquoted, `-z`) gate to "
            "correctly report this genuinely dirty path as unattributable"
        )
        return

    assert live.unattributable == oracle.unattributable
    assert live.passed == oracle.passed


def test_dirty_tree_gate_crlf_phantom_still_suppressed(tmp_path):
    """The load-bearing assertion named in the brief: the EOL-phantom filter
    must still suppress a CRLF phantom under core.autocrlf=true, on the LIVE
    gate (not just parity with the oracle above)."""
    repo = _shape_crlf_eol_phantom(tmp_path)
    outcome = dirty_tree_gate(repo)
    assert outcome.passed is True
    assert outcome.unattributable == []


# --- deletion_block_gate Kept-claim shape builders --------------------------


def _kept_claim_message(path: str) -> str:
    return (
        "subject\n"
        "\n"
        "Kept (Step 2.67):\n"
        f"{path}{_EM_DASH}still needed\n"
        "--- end Step 2.67 blocks ---\n"
    )


@pytest.mark.parametrize(
    "shape,path",
    [
        pytest.param(_shape_clean, "clean.txt", id="clean_path"),
        pytest.param(_shape_space_path, "a dir/has space.txt", id="path_with_space"),
        pytest.param(_shape_nonascii_path, "café-résumé.txt", id="nonascii_path"),
        pytest.param(_shape_symlink, "link-me", id="symlink_120000"),
        pytest.param(_shape_gitlink, "vendor/sub", id="gitlink_160000"),
    ],
)
def test_deletion_gate_kept_claim_matches_pre_c3_oracle(tmp_path, shape, path):
    """AC (undischarged by C3): `deletion_block_gate`'s Assertion-2
    (Kept-claim) re-pointed HEAD-membership leg (`head_blobs`) returns a
    SET-IDENTICAL answer to the pre-re-point (unscoped `git ls-tree -r HEAD
    --name-only`, no type filter) implementation.

    `head_blobs()` was patched (REGRESSION-1 fix) to admit `obj_type ==
    "commit"` (a 160000 gitlink) alongside `"blob"` -- only `"tree"` is
    excluded now -- so `gitlink_160000` matches the oracle here exactly, no
    special-case needed.

    KNOWN, REPORTED DIVERGENCE (REGRESSION-2 fix; not silently dropped --
    see the executor report): the oracle's HEAD-membership leg is an
    unscoped, UNQUOTED-nothing `git ls-tree -r HEAD --name-only` walk --
    default C-quoting applies, so a non-ASCII Kept-claimed path never
    matches the oracle's `tracked_at_head` set and the oracle wrongly
    reports it missing. `nonascii_path` is EXPECTED TO DIVERGE from the
    oracle here in ONE direction only (live.passed=True, oracle.passed=
    False) -- the live gate (`head_blobs`, unquoted `ls-tree -z`) is the
    CORRECT answer, not bug-for-bug parity with the oracle's own quoting bug.
    """
    repo = shape(tmp_path)
    msg = _kept_claim_message(path)
    live = deletion_block_gate(msg, gate_paths=[path], cwd=repo)
    oracle = _old_deletion_block_gate(msg, gate_paths=[path], cwd=repo)

    if path == "café-résumé.txt":
        assert oracle.passed is False, (
            f"oracle={oracle!r} -- expected the oracle's C-quoted ls-tree "
            "lookup to keep missing this non-ASCII path; if this now "
            "passes, the oracle's own bug may have been fixed elsewhere "
            "and this special-case should be removed"
        )
        assert live.passed is True, (
            f"live={live!r} -- expected the live (unquoted `ls-tree -z` via "
            "head_blobs) gate to correctly find this Kept-claim at HEAD"
        )
        return

    assert live.passed == oracle.passed, (
        f"live={live!r} oracle={oracle!r} -- head_blobs() vs unscoped "
        "ls-tree diverged on Kept-claim HEAD membership (see this test's "
        "own docstring)"
    )
    assert live.diagnostics == oracle.diagnostics

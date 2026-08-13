"""
coordinator_core.ops.fleet.tests.test_archive_dest_conflict_heir_stamp_restore

P1 finding (docs/plans/2026-08-13-fleet-archive-dest-collision-vs-idempotent-
replay.md review pass, coordinator:code-reviewer 2026-08-13): `archive_handoffs.
_handle_act_handoffs` stamps `deployment_state: shipped` onto a heir
candidate's own SOURCE file (`_stamp_heir_shipped`) before the dest-collision
check runs. On a genuine (differing-content) dest conflict the candidate is
`continue`d into `skipped[]` without ever building a `Move` — so that stamp
is never committed, reverted, or restaged, leaving the live source in
`state/handoffs/` falsely marked shipped and dirty on disk. The next sweep's
`_is_terminal` then sees the stale stamp and misclassifies the candidate
(the exact Branch-A->Branch-B reclassification trap `_stamp_heir_shipped`'s
own docstring warns against), so the wedge is self-perpetuating.

This module pins the fix: a heir candidate hitting a differing-content dest
conflict must leave its source file byte-identical to its pre-sweep state.

Deliberately does NOT reorder the identity comparison above the stamp — see
the reviewer's adjacent [informational] finding: `_is_identical_duplicate`
must compare POST-stamp src bytes against dst for a genuine idempotent replay
to converge; comparing pre-stamp src would false-positive every heir replay
as a conflict.

Git-free is not reachable here: H4 (a heir candidate's own eligibility gate)
requires a resolvable `shipped_in` commit SHA (`git cat-file -e`), which
needs a real repo to exhibit honestly — a mocked git has no object database
to resolve against. This is the governed one-repo-one-test pattern (per the
spawn-heavy-test-excision-ledger ruling), scoped to a single throwaway repo
and a single test function. `_handle_act_handoffs` is called directly
(bypassing the `@register_op` handler's `check_repo_root`/`main_worktree_root`
plumbing, which this seam does not need) — real production disposition code,
not a reimplementation of it. The differing-bytes case never reaches
`archive_and_commit` at all (the module `continue`s straight to `skipped[]`
after restoring the stamp), so no mover monkeypatch is needed or used.
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from coordinator_core.ops.fleet import archive_handoffs
from coordinator_core.ops.fleet._common import _REASON_DEST_CONFLICT

# Real-git spawn is load-bearing: the heir eligibility gate (H4) requires a
# resolvable `shipped_in` commit SHA via `git cat-file -e`, which needs a
# real object database -- a mocked git has none to resolve against. Single
# test, single repo (also proves idempotence across two consecutive sweeps
# of the same wedge) -- no module-scope hoist needed. The spawn ratchet's
# `_BASELINE` is shrink-only pre-existing residue and is explicitly not the
# route for this file -- coordinator_core/tests/test_no_new_spawning_tests.py
# Rule 2.
pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    # popup-intentional-last-resort — test-only real-git spawn, mirrors the
    # governed real_git.py fixture's own unguarded pattern; no console window
    # risk on the CI/dev platforms this suite runs on.
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=True,
    )


def _run(coro):
    return asyncio.run(coro)


def test_heir_dest_conflict_leaves_source_byte_identical_to_pre_sweep_state(
    tmp_path: Path,
) -> None:
    worktree = tmp_path / "repo"
    worktree.mkdir()
    _git(["init", "-q", "-b", "main"], worktree)
    _git(["config", "user.email", "test@example.invalid"], worktree)
    _git(["config", "user.name", "test"], worktree)

    (worktree / "README.md").write_text("seed\n", encoding="utf-8")
    _git(["add", "-A"], worktree)
    _git(["commit", "-q", "-m", "seed"], worktree)
    shipped_sha = _git(["rev-parse", "HEAD"], worktree).stdout.strip()

    handoffs_dir = worktree / "state" / "handoffs"
    handoffs_dir.mkdir(parents=True)

    candidate_name = "2026-08-01-wedged-heir.md"
    candidate_path = handoffs_dir / candidate_name
    candidate_pre_sweep_bytes = (
        f"---\n"
        f"status: claimed\n"
        f"title: wedged heir\n"
        f"shipped_in: {shipped_sha}\n"
        f"---\n"
        f"body\n"
    ).encode("utf-8")
    candidate_path.write_bytes(candidate_pre_sweep_bytes)

    # Successor: a live, non-terminal handoff naming the candidate as its
    # predecessor -- this is what makes the candidate a heir (_classify_heir_
    # children's succession-edge partition).
    successor_path = handoffs_dir / "2026-08-02-successor.md"
    successor_path.write_text(
        "---\n"
        "status: open\n"
        "deployment_state: in_flight\n"
        "title: successor\n"
        f"predecessor: {candidate_name}\n"
        "---\n"
        "body\n",
        encoding="utf-8",
    )

    # A DIFFERENT file already occupies the archive destination -- the
    # genuine, unresolvable dest-conflict shape (not idempotent replay).
    dest_dir = worktree / "archive" / "handoffs" / "2026-08"
    dest_dir.mkdir(parents=True)
    dest_path = dest_dir / candidate_name
    dest_path.write_text("a DIFFERENT archived copy\n", encoding="utf-8")

    dag_index = [str(candidate_path.resolve()), str(successor_path.resolve())]
    common_dir = worktree / ".git"
    cid = f"state/handoffs/{candidate_name}"

    result = _run(archive_handoffs._handle_act_handoffs(
        mode="already-terminal",
        worktree=worktree,
        dag_index=dag_index,
        candidate_ids=[cid],
        common_dir=common_dir,
    ))

    assert result["skipped"] == [{"id": cid, "reason": _REASON_DEST_CONFLICT}], result
    assert result["acted"] == []
    assert result["failed"] == []

    # The whole point of the fix: the source must be exactly what it was
    # before the sweep touched it -- not stamped "shipped" and left dirty.
    assert candidate_path.read_bytes() == candidate_pre_sweep_bytes

    # And the differing archived copy is untouched too.
    assert dest_path.read_text(encoding="utf-8") == "a DIFFERENT archived copy\n"

    # Idempotence: a second run over the same wedge must see exactly the
    # same disposition and leave the source in the same state again.
    result2 = _run(archive_handoffs._handle_act_handoffs(
        mode="already-terminal",
        worktree=worktree,
        dag_index=dag_index,
        candidate_ids=[cid],
        common_dir=common_dir,
    ))
    assert result2["skipped"] == [{"id": cid, "reason": _REASON_DEST_CONFLICT}], result2
    assert result2["failed"] == []
    assert candidate_path.read_bytes() == candidate_pre_sweep_bytes


async def _fake_archive_and_commit(worktree_root, moves, subject):
    """Records what it was asked to move; never spawns a real git-mv/commit.

    Review: coordinatorcode-reviewer-9021265a [P2, informational] — the
    refusal of the "hoist the dest-collision check above the stamp" remedy
    is justified by the claim that a heir replay must compare POST-stamp src
    bytes against dst to converge. That claim was argued in docstrings but
    never exercised by a test for the heir-specific case; this monkeypatch
    lets the test below reach that convergence without a real git-mv (the
    real mover is exercised elsewhere — see
    test_archive_and_commit_reversal_else_src_reappeared.py).
    """
    _fake_archive_and_commit.captured = list(moves)
    acted = [{"id": m.candidate_id, "archived": True} for m in moves]
    return acted, []


def test_heir_dest_conflict_byte_identical_to_post_stamp_src_converges(
    tmp_path: Path,
) -> None:
    """The reviewer's proof obligation: a heir candidate re-swept over its own
    already-archived copy — where the archived copy is byte-identical to what
    the SOURCE looks like only AFTER `_stamp_heir_shipped` runs — must converge
    (force-overwrite, `acted`), never wedge as `_REASON_DEST_CONFLICT`. This is
    the load-bearing case for refusing to hoist the dest-collision check above
    the stamp: comparing PRE-stamp src bytes against dst would false-positive
    this exact convergence as a conflict.
    """
    worktree = tmp_path / "repo"
    worktree.mkdir()
    _git(["init", "-q", "-b", "main"], worktree)
    _git(["config", "user.email", "test@example.invalid"], worktree)
    _git(["config", "user.name", "test"], worktree)

    (worktree / "README.md").write_text("seed\n", encoding="utf-8")
    _git(["add", "-A"], worktree)
    _git(["commit", "-q", "-m", "seed"], worktree)
    shipped_sha = _git(["rev-parse", "HEAD"], worktree).stdout.strip()

    handoffs_dir = worktree / "state" / "handoffs"
    handoffs_dir.mkdir(parents=True)

    candidate_name = "2026-08-01-converging-heir.md"
    candidate_path = handoffs_dir / candidate_name
    candidate_pre_sweep_bytes = (
        f"---\n"
        f"status: claimed\n"
        f"title: converging heir\n"
        f"shipped_in: {shipped_sha}\n"
        f"---\n"
        f"body\n"
    ).encode("utf-8")
    candidate_path.write_bytes(candidate_pre_sweep_bytes)

    successor_path = handoffs_dir / "2026-08-02-successor.md"
    successor_path.write_text(
        "---\n"
        "status: open\n"
        "deployment_state: in_flight\n"
        "title: successor\n"
        f"predecessor: {candidate_name}\n"
        "---\n"
        "body\n",
        encoding="utf-8",
    )

    # Compute the EXACT post-stamp bytes by stamping a scratch copy — this is
    # what the archive destination looks like from a prior sweep that DID
    # complete (git-mv'd the stamped source), and it is exactly what THIS
    # sweep's own stamp will produce on the live source a moment later.
    scratch_path = tmp_path / "scratch-stamped-copy.md"
    scratch_path.write_bytes(candidate_pre_sweep_bytes)
    archive_handoffs._stamp_heir_shipped(scratch_path)
    post_stamp_bytes = scratch_path.read_bytes()
    assert post_stamp_bytes != candidate_pre_sweep_bytes, (
        "the stamp must actually change bytes for this test to be load-bearing"
    )

    dest_dir = worktree / "archive" / "handoffs" / "2026-08"
    dest_dir.mkdir(parents=True)
    dest_path = dest_dir / candidate_name
    dest_path.write_bytes(post_stamp_bytes)

    dag_index = [str(candidate_path.resolve()), str(successor_path.resolve())]
    common_dir = worktree / ".git"
    cid = f"state/handoffs/{candidate_name}"

    with patch.object(archive_handoffs, "archive_and_commit", _fake_archive_and_commit):
        _fake_archive_and_commit.captured = None
        result = _run(archive_handoffs._handle_act_handoffs(
            mode="already-terminal",
            worktree=worktree,
            dag_index=dag_index,
            candidate_ids=[cid],
            common_dir=common_dir,
        ))

    assert result["skipped"] == [], result
    assert result["failed"] == [], result
    assert result["acted"] == [{"id": cid, "archived": True}], result
    assert _fake_archive_and_commit.captured is not None
    assert len(_fake_archive_and_commit.captured) == 1
    move = _fake_archive_and_commit.captured[0]
    assert move.candidate_id == cid
    assert move.force is True, (
        "a heir replay converging on its own post-stamp archived copy must "
        "force-overwrite, not skip as a conflict"
    )
    assert move.restage_src is True

    # The source was stamped in place (about to be git-mv'd by the real
    # mover, which this test does not exercise) — it now matches the
    # precomputed post-stamp bytes, i.e. it matches dst too.
    assert candidate_path.read_bytes() == post_stamp_bytes

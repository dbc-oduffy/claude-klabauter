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

from coordinator_core.ops.fleet import archive_handoffs
from coordinator_core.ops.fleet._common import _REASON_DEST_CONFLICT


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

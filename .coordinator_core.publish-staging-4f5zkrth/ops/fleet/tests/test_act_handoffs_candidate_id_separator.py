"""
coordinator_core.ops.fleet.tests.test_act_handoffs_candidate_id_separator

Pins the wire-path contract between a caller's ``candidate_ids`` and
``_handle_act_handoffs``'s ``live_handoffs`` map: the map is keyed by
``wire_paths.rel_id``, which is ALWAYS forward-slash, so a relative
candidate_id arriving with ``os.sep`` separators (a Windows caller slicing
repo-relative paths itself) must still resolve to its live handoff.

Why this is worth its own module: when the lookup misses, the candidate takes
the ``handoff_path is None`` branch and is classified ``already-archived`` --
the legitimate idempotent-replay reason -- so the op returns an empty ``acted``
with no failure and no warning, and every caller reports a clean no-op. That
is a silent, total archival stall, and it is invisible in logs.

The backslash id is passed as a literal rather than built with ``os.path.join``
so the assertion bites on POSIX too: a host whose ``os.sep`` is already ``/``
would otherwise exercise nothing and stay green through a regression.

Negative-spec:
  - Does NOT exercise git-mv, archive_and_commit, or the batch commit -- the
    contract under test is the map lookup alone, so ``_is_terminal`` is
    stubbed non-terminal and the run stops at the re-live skip. A ``re-live:``
    reason proves the lookup HIT; ``already-archived`` proves it MISSED. That
    discriminator is the whole assertion.
  - Does NOT re-test absolute candidate_id normalization -- that branch
    predates this contract and is exercised by the archive_plans/prune_bugs
    tolerance it was written for.
  - Spawns no git. Seeds one real file under tmp_path because the miss
    branch is ``handoff_path is None or not handoff_path.exists()`` -- a
    fictional path would report already-archived for the wrong reason and the
    test would pass against the defect.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, patch

from coordinator_core.ops.fleet import archive_handoffs


def _act(candidate_id: str, worktree: Path) -> dict:
    live_path = worktree / "state" / "handoffs" / "x.md"
    live_path.parent.mkdir(parents=True, exist_ok=True)
    live_path.write_text("---\nstatus: claimed\n---\n", encoding="utf-8")
    with patch.object(archive_handoffs, "collect_live_handoff_paths", return_value=[live_path]), \
         patch.object(
             archive_handoffs,
             "_is_terminal",
             return_value=(False, "stub: lookup reached terminality re-check", ""),
         ):
        return asyncio.run(
            archive_handoffs._handle_act_handoffs(
                "already-terminal", worktree, [], [candidate_id], worktree / ".git",
            )
        )


def _reason(result: dict) -> str:
    skipped = result.get("skipped") or []
    assert len(skipped) == 1, f"expected exactly one skip, got {skipped!r}"
    return skipped[0].get("reason", "")


def test_backslash_relative_candidate_id_matches_forward_slash_map(tmp_path):
    worktree = tmp_path
    reason = _reason(_act("state\\handoffs\\x.md", worktree))
    assert not reason.startswith("already-archived"), (
        "backslash-separated relative candidate_id missed the rel_id()-keyed "
        f"live_handoffs map -- silent archival stall; reason was {reason!r}"
    )
    assert reason.startswith("re-live:"), reason


def test_forward_slash_relative_candidate_id_still_matches(tmp_path):
    worktree = tmp_path
    reason = _reason(_act("state/handoffs/x.md", worktree))
    assert reason.startswith("re-live:"), reason


def test_unknown_candidate_id_is_still_already_archived(tmp_path):
    worktree = tmp_path
    reason = _reason(_act("state/handoffs/absent.md", worktree))
    assert reason == "already-archived", reason


def test_mixed_batch_separator_normalization_never_changes_classification(tmp_path):
    """Single dispatch, mixed backslash/forward-slash candidate_ids, mixed
    terminality -- separator normalization must change ONLY the map lookup,
    never which candidates are classified terminal vs held back, and never
    which held-back reason a given candidate carries.

    Mirrors project-rag's real Windows batch (37 candidates: 31 archived, 6
    skipped -- 5x "re-live: has live children: 1", 1x "re-live: live claim
    (claim-dir holder live)", 0 failed) at minimal scale: one candidate per
    outcome, one of the terminal/skip candidates addressed with backslashes
    and the other with forward slashes, so a normalization defect that
    clobbers the guard plane (e.g. archiving a held-back candidate, or
    losing/merging distinct skip reasons) has somewhere to show up.
    """
    worktree = tmp_path
    handoffs_dir = worktree / "state" / "handoffs"
    handoffs_dir.mkdir(parents=True)

    terminal_path = handoffs_dir / "terminal.md"
    terminal_path.write_text("---\nstatus: claimed\n---\n", encoding="utf-8")
    live_children_path = handoffs_dir / "live-children.md"
    live_children_path.write_text("---\nstatus: claimed\n---\n", encoding="utf-8")
    live_claim_path = handoffs_dir / "live-claim.md"
    live_claim_path.write_text("---\nstatus: claimed\n---\n", encoding="utf-8")

    def _is_terminal_side_effect(handoff_path, *_args, **_kwargs):
        # *_args/**_kwargs: unused, kept for signature conformance -- this
        # side_effect stands in for _is_terminal(handoff_path, dag_index,
        # worktree, common_dir, *, allow_in_flight=...) and Mock's side_effect
        # forwards every positional/keyword argument the real call site passes.
        if handoff_path == terminal_path:
            return (True, "consumed; no live children; no live claim", "consumed")
        if handoff_path == live_children_path:
            return (False, "has live children: 1", "")
        if handoff_path == live_claim_path:
            return (False, "live claim (claim-dir holder live)", "")
        raise AssertionError(f"unexpected handoff_path {handoff_path!r}")

    acted_stub = [{"id": "state\\handoffs\\terminal.md", "path": "archive/handoffs/2026-08/terminal.md"}]

    with patch.object(
        archive_handoffs, "collect_live_handoff_paths",
        return_value=[terminal_path, live_children_path, live_claim_path],
    ), patch.object(
        archive_handoffs, "_is_terminal", side_effect=_is_terminal_side_effect,
    ), patch.object(
        archive_handoffs, "archive_and_commit",
        new=AsyncMock(return_value=(acted_stub, [])),
    ):
        result = asyncio.run(
            archive_handoffs._handle_act_handoffs(
                "already-terminal",
                worktree,
                [],
                [
                    "state\\handoffs\\terminal.md",
                    "state/handoffs/live-children.md",
                    "state\\handoffs\\live-claim.md",
                ],
                worktree / ".git",
            )
        )

    acted = result.get("acted") or []
    skipped = result.get("skipped") or []
    assert [a["id"] for a in acted] == ["state\\handoffs\\terminal.md"], acted

    by_id = {s["id"]: s["reason"] for s in skipped}
    assert by_id == {
        "state/handoffs/live-children.md": "re-live: has live children: 1",
        "state\\handoffs\\live-claim.md": "re-live: live claim (claim-dir holder live)",
    }, by_id

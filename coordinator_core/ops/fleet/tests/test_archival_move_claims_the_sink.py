"""The archival movers declare both ends of every landed move.

An archived record left unclaimed at its sink reaches `session.scope.
compute_scope` as an owner-less orphan, and `bash_guards/dispatch_checks`
Check 5 renders `owner:orphan` — the arm gating that check's PM-ratified flip
from warn to deny. `archive_terminal_handoffs` / `archive_actioned_memos` are
exempt from `relocate_touched_path` (no `session_id` in scope inside a bulk
sweep), so the claim has to come from the result instead, through `ipc.py`'s
`_SCOPE_TOUCH_PATHS_KEY` contract — the same seam `memo_reconcile_outbox`
already uses.

Two halves. The first asserts the declaration's SHAPE against
`declare_move_claims` directly — including that a failed move contributes
nothing, which is the half that would silently declare a write that never
happened. The second drives each op's real `_handle_act` and asserts the key
reaches the result, mirroring `memo_reconcile_outbox`'s own dedicated
integration test for the identical contract. Neither op's existing
envelope-contract tests assert on `_scope_touch_paths` at all; they check
subsets of the result dict, so they neither break on the new key nor pin it.
"""
from pathlib import Path

from coordinator_core.ops.fleet._common import Move, declare_move_claims


def _move(name: str) -> Move:
    return Move(
        src=Path(f"/repo/state/handoffs/{name}"),
        dst=Path(f"/repo/archive/handoffs/2026-08/{name}"),
        candidate_id=f"state/handoffs/{name}",
    )


def test_both_ends_of_a_landed_move_are_declared():
    move = _move("a.md")
    result = declare_move_claims(
        {"exit_code": 0}, [move], [{"id": move.candidate_id, "archived": True}]
    )
    declared = result["_scope_touch_paths"]
    assert str(move.dst) in declared, "the sink must be claimed — this is the orphan arm"
    assert str(move.src) in declared, (
        "a move is a deletion at the source too; Check 5 must be able to "
        "attribute that deletion to the same session"
    )
    assert len(declared) == 2


def test_a_failed_move_declares_nothing():
    landed, refused = _move("landed.md"), _move("refused.md")
    result = declare_move_claims(
        {"exit_code": 0},
        [landed, refused],
        [{"id": landed.candidate_id, "archived": True}],
    )
    declared = result["_scope_touch_paths"]
    assert str(refused.src) not in declared and str(refused.dst) not in declared, (
        "_SCOPE_TOUCH_PATHS_KEY is the REAL write set, never the intended "
        "surface — a refused move wrote nothing"
    )
    assert len(declared) == 2


def test_an_empty_batch_writes_no_key():
    assert declare_move_claims({"exit_code": 0}, [], []) == {"exit_code": 0}, (
        "a sweep that moved nothing must not leave an empty declaration behind"
    )


def test_an_acted_id_with_no_matching_move_is_skipped():
    result = declare_move_claims(
        {"exit_code": 0}, [_move("a.md")], [{"id": "state/handoffs/ghost.md"}]
    )
    assert "_scope_touch_paths" not in result


# ---------------------------------------------------------------------------
# The two real call sites
# ---------------------------------------------------------------------------

import pytest

import coordinator_core.ops.fleet.archive_actioned_memos as memos_op
import coordinator_core.ops.fleet.archive_terminal_handoffs as handoffs_op


@pytest.mark.parametrize(
    "module, import_site",
    [
        (handoffs_op, "coordinator_core.ops.fleet._common"),
        (memos_op, "coordinator_core.ops.fleet._common"),
    ],
    ids=["archive_terminal_handoffs", "archive_actioned_memos"],
)
def test_handle_act_populates_the_declaration(module, import_site, monkeypatch):
    """The wiring, not the helper — a call site that drops the call is the
    regression this pins, and it is invisible to the shape tests above.

    `plan_sweep` and `archive_and_commit` are stubbed rather than driven: the
    real pair needs a git repo and a commit, and neither is what this asserts.
    `archive_and_commit` is patched at its DEFINITION site, since one call site
    imports it at module scope and the other inside the function.
    """
    move = _move("wired.md")
    monkeypatch.setattr(module, "plan_sweep", lambda *a, **k: ([move], []))

    async def _fake_archive_and_commit(**kwargs):
        return [{"id": move.candidate_id, "archived": True}], []

    monkeypatch.setattr(
        import_site + ".archive_and_commit", _fake_archive_and_commit, raising=True
    )
    if hasattr(module, "archive_and_commit"):
        monkeypatch.setattr(module, "archive_and_commit", _fake_archive_and_commit)

    result = module._handle_act(
        "act", Path("/repo"), Path("/repo/.git"), [move.candidate_id], 10
    )
    declared = result.get("_scope_touch_paths")
    assert declared is not None, (
        f"{module.__name__}._handle_act dropped the declaration — an archived "
        f"record reaches compute_scope as owner:orphan"
    )
    assert str(move.dst) in declared and str(move.src) in declared

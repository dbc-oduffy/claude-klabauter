"""_close_block reports an already-landed refire's legs not-applicable, a genuine non-landing failed."""

from coordinator_core.ops.dispatch_emit import run_close
from coordinator_core.ops.dispatch_emit.terminal_commit import _close_block


def _ctx(tmp_path):
    return {
        "legs": {},
        "applies": True,
        "presets": {},
        "kind": "plan-run",
        "root": tmp_path,
        "plan_rel": "docs/plans/x.md",
        "run_id": "r1",
        "completion_entry": None,
    }


def test_already_landed_refire_reads_not_applicable(tmp_path):
    block = _close_block(_ctx(tmp_path), {"committed": False, "nothing_to_commit": True})
    for leg in ("review", "plan-reconciliation", "lessons", "post-close"):
        assert block["legs"][leg]["status"] == "not-applicable"
        assert "already landed" in block["legs"][leg]["detail"]
    assert block["outstanding"] == []


def test_refusal_still_fails(tmp_path):
    reply = {"committed": False, "nothing_to_commit": True, "refused": "dirty"}
    block = _close_block(_ctx(tmp_path), reply)
    assert block["legs"]["review"]["status"] == "failed"
    assert block["legs"]["plan-reconciliation"]["status"] == "failed"
    assert "review" in block["outstanding"]


def test_no_commit_without_nothing_to_commit_still_fails(tmp_path):
    block = _close_block(_ctx(tmp_path), {"committed": False})
    assert block["legs"]["review"]["status"] == "failed"
    assert block["legs"]["plan-reconciliation"]["status"] == "failed"
    assert set(block["outstanding"]) >= {"review", "plan-reconciliation"}
    assert set(block["legs"]) >= set(run_close.RUN_BOUND_LEGS)

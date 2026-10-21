"""plan.seam_check hands one plan-edge cache to every plan of a call, and none outlives the call.

Git is faked at the module's ``run_git`` seam; no process is spawned.
"""

from __future__ import annotations

import pytest

from coordinator_core.ops import plan_seam_check as op
from coordinator_core.ops.dispatch_emit import spine_read
from coordinator_core.ops.tests.test_plan_seam_check import _plan, _root, _row, git  # noqa: F401

TARGET_ROW = _row("T1", "[t.py]")


def _dependent(root, name):
    edge = "  depends_on_plan:\n    - {plan: docs/plans/target.md, chunk: T1, gate_kind: output-consumption-runtime}\n"
    return _plan(root, name, _row("R1", f"[{name}.py]", extra=edge))


@pytest.fixture
def target_reads(monkeypatch):
    """Texts `resolve_plan_edge` parses, as a list: one entry per edge-target load."""
    reads: list = []
    real = spine_read.load_rows_memo

    def counting(text):
        if "id: T1" in text:
            reads.append(text)
        return real(text)

    monkeypatch.setattr(spine_read, "load_rows_memo", counting)
    return reads


def test_three_plans_onto_one_target_read_it_once_per_evaluate(tmp_path, git, target_reads):
    root = _root(tmp_path)
    target = _plan(root, "target", TARGET_ROW)
    plans = [target] + [_dependent(root, n) for n in ("a", "b", "c")]
    op._evaluate({"plans": plans, "phase": "prep", "named_set": True}, root)
    # One parse by the target's own read_spine, one edge load shared by all three dependents.
    assert len(target_reads) == 2


def test_two_evaluates_do_not_share_a_cache(tmp_path, git, target_reads):
    root = _root(tmp_path)
    target = _plan(root, "target", TARGET_ROW)
    plans = [target] + [_dependent(root, n) for n in ("a", "b", "c")]
    params = {"plans": plans, "phase": "prep", "named_set": True}
    op._evaluate(params, root)
    first = len(target_reads)
    _plan(root, "target", TARGET_ROW + _row("T2", "[t2.py]"))
    op._evaluate(params, root)
    assert len(target_reads) == 2 * first
    assert "id: T2" in target_reads[-1]

"""`closed` joins every residual plan-axis status set exactly where `abandoned` is."""
from __future__ import annotations

import functools
import importlib.util
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]


def _load_bin(name: str):
    path = REPO / "coordinator" / "bin" / name
    spec = importlib.util.spec_from_file_location(name.replace("-", "_").removesuffix(".py"), path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@functools.lru_cache(maxsize=1)
def _sets():
    from coordinator_core.distill import delete_guard
    from coordinator_core.ops.fleet import prune_emitted
    from coordinator_core.ops.session import guard_concrete_path_citations as gcpc
    from coordinator_core.roadmap import audit
    from coordinator_core import workstream_complete as wc

    return {
        "gcpc": gcpc._TERMINAL_PLAN_STATUSES,
        "provisional_expiry": _load_bin("check-provisional-expiry.py")._NOT_ACTIVELY_EXECUTING_STATUSES,
        "wave_fire": _load_bin("emit-wave-fire.py")._SHELVED_PLAN_STATUSES,
        "delete_guard": delete_guard.PLAN_NEVER_DELIVERED_STATUSES,
        "prune_emitted": prune_emitted._STATE_CLOSED_STATUSES,
        "audit": audit._PLAN_TERMINAL_STATUSES,
        "leg_a": wc._LEG_A_TERMINAL_PLAN_STATUS,
    }


@pytest.mark.parametrize("key", list(_sets()))
def test_closed_membership_equals_abandoned(key):
    s = _sets()[key]
    assert "abandoned" in s
    assert ("closed" in s) == ("abandoned" in s)


def test_review_receipt_exemption_matches_abandoned():
    from coordinator_core import workstream_complete as wc

    assert wc._status_requires_review_receipt("closed") == wc._status_requires_review_receipt("abandoned")
    assert wc._status_requires_review_receipt("closed") is False
    for gated in ("implemented", "shipped", "complete", "deferred"):
        assert wc._status_requires_review_receipt(gated) is True


def test_spine_read_status_edge_hold_treats_closed_like_abandoned():
    from coordinator_core.ops.dispatch_emit import spine_read
    from coordinator_core.ops.dispatch_emit.spine_read import DanglingPlanDependencyError

    for status in ("abandoned", "closed"):
        with pytest.raises(DanglingPlanDependencyError):
            spine_read._status_edge_hold("o", "p", "implemented", status)


@pytest.mark.parametrize("status,expected_blocked", [("abandoned", False), ("closed", False), ("approved", True)])
def test_update_docs_scan_lineage_successor(tmp_path, status, expected_blocked):
    from coordinator_core.ops.ceremony import update_docs_scan

    plans = tmp_path / "docs" / "plans"
    plans.mkdir(parents=True)
    (plans / "2026-01-01-successor.md").write_text(
        f"---\nstatus: {status}\nsupersedes: docs/plans/old.md\n---\nbody\n", encoding="utf-8"
    )
    edges = update_docs_scan._phase8_lineage_backstop(tmp_path)
    assert len(edges) == 1
    assert edges[0]["blocked"] is expected_blocked

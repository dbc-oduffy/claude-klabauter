"""`--no-push` reaches every pushing workday-complete directive; the default argv is unchanged."""

from __future__ import annotations

import io
from contextlib import redirect_stdout
from datetime import datetime

import pytest

from coordinator_core.ops.requirement_register import StallReport
from coordinator_core.ops.workday_complete_backfill_scan import GapRow, GapScan
from coordinator_core.workday_complete import apply as wc_apply
from coordinator_core.workday_complete import brief as wc_brief
from coordinator_core.workday_complete import run_spec

PUSHING = ("d_step3_consolidate", "d_step3_5_backfill_phase_b", "d_step9_changelog")


@pytest.fixture
def stub(monkeypatch):
    scan = GapScan(rows=[GapRow("2026-10-17", 2, "a" * 40, "b" * 40)])
    monkeypatch.setattr(wc_brief, "resolve_operator_config", lambda env=None: {})
    monkeypatch.setattr(wc_brief, "head_branch", lambda _p: "work/x")
    monkeypatch.setattr(wc_brief, "_compute_open_day_goals", lambda: {"today": [], "stale": [], "unreadable_error": None})
    monkeypatch.setattr(wc_brief, "_compute_register_stall", lambda: StallReport())
    monkeypatch.setattr(wc_brief, "_compute_dirty_tree_verdict", lambda: {"ambiguous": False, "evidence": "clean"})
    monkeypatch.setattr(wc_brief, "_compute_backfill_scan", lambda: (scan, ""))
    monkeypatch.setattr(wc_brief, "_main_worktree_root_for_directive", lambda: "/repo")
    monkeypatch.setattr(wc_brief, "compute_cockpit_contract_freshness", lambda: {})
    monkeypatch.setattr(wc_brief, "_compute_completion_receipts_gate", lambda _d: {})


def _args(env, directive_id):
    return next(d["args"] for d in env["directives"] if d["id"] == directive_id)


def _brief(**kw):
    with redirect_stdout(io.StringIO()):
        return wc_brief.brief(decisions={}, now_local=datetime(2026, 10, 20, 18, 0), **kw)[1]


def test_conclude_no_push_marks_all_three_pushing_directives(stub):
    env = _brief(no_push=True, scope_summary="-- wrapped")
    for did in PUSHING:
        assert "--no-push" in _args(env, did), did
    step9 = _args(env, "d_step9_changelog")
    assert step9.index("--no-push") < step9.index("--")


def test_default_carries_no_no_push(stub):
    env = _brief()
    assert all("--no-push" not in d["args"] for d in env["directives"])
    assert _args(env, "d_step3_consolidate") == []


def test_catch_up_no_push_marks_step3_and_backfill(stub):
    env = _brief(no_push=True, catch_up=True)
    assert "--no-push" in _args(env, "d_step3_consolidate")
    assert "--no-push" in _args(env, "d_step3_5_backfill_phase_b")


def test_apply_main_threads_no_push(monkeypatch):
    seen = {}
    monkeypatch.setattr(wc_apply, "apply", lambda **k: (seen.update(k), (0, {}))[1])
    with redirect_stdout(io.StringIO()):
        assert wc_apply.main(["--catch-up", "--no-push"]) == 0
        assert seen["no_push"] is True
        seen.clear()
        assert wc_apply.main(["--catch-up"]) == 0
    assert "no_push" not in seen


def test_run_spec_forwards_no_push_to_the_brief_step():
    assert "--no-push" in run_spec.SPEC.launch_flags
    assert "--no-push" in list(run_spec.front_door({"--no-push": True})[-1].args)
    assert "--no-push" not in list(run_spec.front_door({})[-1].args)

"""workday-complete catch-up mode: period_mode on the brief, concluding directives absent."""

from __future__ import annotations

import io
import json
from contextlib import redirect_stdout
from datetime import datetime

import pytest

from coordinator_core.cadence import period_mode as pm
from coordinator_core.ops.requirement_register import StallReport
from coordinator_core.ops.workday_complete_backfill_scan import GapRow, GapScan
from coordinator_core.workday_complete import apply as wc_apply
from coordinator_core.workday_complete import brief as wc_brief

# The directive and judgment-point ids a no-flag brief emitted at 18:00 before catch-up existed.
GOLDEN_DIRECTIVE_IDS = [
    "d_step1_validate",
    "d_step1_5_cruft_sweep",
    "d_step2_5_dirty_tree_scan",
    "d_step2_67_handoff_housekeeping",
    "d_step3_consolidate",
    "d_step3_5_backfill_scan",
    "d_step3_5_backfill_anchor_a0",
    "d_step3_5_backfill_phase_b",
    "d_step4a_inventory",
    "d_step6_archive_audit_completions",
    "d_step7_5_prune_closed_bugs",
    "d_step8_improvement_queue_nudge",
    "d_step9_changelog",
    "d_step10_weekly_staleness",
    "d_step10_5_post_command_hook",
]
GOLDEN_JUDGMENT_IDS = [
    "jp_step4b_analyst_dispatch",
    "jp_step4_5_clustering_dispatch",
    "jp_step4e_health_ledger_new_rows",
]

_OPEN_GOALS = {"today": [{"goal_id": "g1", "text": "ship"}], "stale": [], "unreadable_error": None}
_NO_GOALS = {"today": [], "stale": [], "unreadable_error": None}


def _rows(*days: str) -> GapScan:
    return GapScan(rows=[GapRow(d, 2, "a" * 40, "b" * 40) for d in days])


@pytest.fixture
def stub(monkeypatch):
    state = {"scan": _rows("2026-10-17", "2026-10-18"), "goals": _NO_GOALS}
    monkeypatch.setattr(wc_brief, "resolve_operator_config", lambda env=None: {})
    monkeypatch.setattr(wc_brief, "head_branch", lambda _p: "work/x")
    monkeypatch.setattr(wc_brief, "_compute_open_day_goals", lambda: state["goals"])
    monkeypatch.setattr(wc_brief, "_compute_register_stall", lambda: StallReport())
    monkeypatch.setattr(
        wc_brief, "_compute_dirty_tree_verdict", lambda: {"ambiguous": False, "evidence": "clean"}
    )
    monkeypatch.setattr(wc_brief, "_compute_backfill_scan", lambda: (state["scan"], ""))
    monkeypatch.setattr(wc_brief, "_main_worktree_root_for_directive", lambda: "/repo")
    monkeypatch.setattr(wc_brief, "compute_cockpit_contract_freshness", lambda: {})
    monkeypatch.setattr(wc_brief, "_compute_completion_receipts_gate", lambda _d: {})
    return state


def _brief(**kwargs):
    with redirect_stdout(io.StringIO()):
        return wc_brief.brief(decisions={}, **kwargs)


def _ids(envelope):
    return (
        [d["id"] for d in envelope["directives"]],
        [p["id"] for p in envelope["judgment_points"]],
    )


def test_golden_conclude_set_at_18_00(stub):
    code, env = _brief(now_local=datetime(2026, 10, 20, 18, 0))
    assert code == 0
    assert _ids(env) == (GOLDEN_DIRECTIVE_IDS, GOLDEN_JUDGMENT_IDS)
    assert env["period_mode"]["mode"] == "conclude"
    assert env["period_mode"]["source"] == "auto-clock"


def test_catch_up_flag_drops_all_concluding_ids_and_keeps_phase_b_over_every_row(stub):
    code, env = _brief(now_local=datetime(2026, 10, 20, 10, 48), catch_up=True)
    assert code == 0
    directive_ids, jp_ids = _ids(env)
    for cid in pm.CONCLUDING_DIRECTIVES["workday-complete"]:
        assert cid not in directive_ids and cid not in jp_ids
    phase_b = next(d for d in env["directives"] if d["id"] == "d_step3_5_backfill_phase_b")
    assert phase_b["args"] == ["backfill-dispatch-rows", "--allow-empty"]
    assert phase_b["stdin_from"] == "d_step3_5_backfill_scan"
    assert env["period_mode"] == {
        "mode": "catch_up",
        "source": "explicit",
        "in_progress": "2026-10-20",
        "missed": ["2026-10-17", "2026-10-18"],
        "reason": "explicit catch_up",
    }
    assert "2026-10-20" not in json.dumps(env["directives"])
    assert env["narration"].startswith(
        "Catch-up run: 2026-10-17, 2026-10-18 backfilled; 2026-10-20 left open"
    )


def test_no_flag_at_10_48_emits_the_catch_up_set(stub):
    _, auto = _brief(now_local=datetime(2026, 10, 20, 10, 48))
    _, flagged = _brief(now_local=datetime(2026, 10, 20, 10, 48), catch_up=True)
    assert _ids(auto) == _ids(flagged)
    assert auto["period_mode"]["source"] == "auto-clock"
    assert auto["period_mode"]["mode"] == "catch_up"


def test_conclude_flag_at_10_48_emits_the_golden_set(stub):
    _, env = _brief(now_local=datetime(2026, 10, 20, 10, 48), conclude=True)
    assert _ids(env) == (GOLDEN_DIRECTIVE_IDS, GOLDEN_JUDGMENT_IDS)
    assert env["period_mode"]["source"] == "explicit"


def test_open_day_goals_add_no_closeout_under_catch_up(stub):
    stub["goals"] = _OPEN_GOALS
    _, env = _brief(now_local=datetime(2026, 10, 20, 10, 48))
    directive_ids, jp_ids = _ids(env)
    assert "d_goal_close_day" not in directive_ids
    assert "jp_day_goal_closeout" not in jp_ids
    _, concluded = _brief(now_local=datetime(2026, 10, 20, 18, 0))
    assert "d_goal_close_day" in _ids(concluded)[0]
    assert "jp_day_goal_closeout" in _ids(concluded)[1]


def test_after_midnight_prior_day_is_a_gap_row_and_no_directive_names_the_new_date(stub):
    stub["scan"] = _rows("2026-10-19")
    _, env = _brief(now_local=datetime(2026, 10, 20, 0, 30))
    assert env["period_mode"]["missed"] == ["2026-10-19"]
    assert env["period_mode"]["in_progress"] == "2026-10-20"
    assert "2026-10-20" not in json.dumps(env["directives"])


def test_no_gap_rows_narration_says_nothing_was_missed(stub):
    stub["scan"] = GapScan()
    _, env = _brief(now_local=datetime(2026, 10, 20, 9, 0))
    assert env["period_mode"]["missed"] == []
    assert env["narration"].startswith("Catch-up run: no missed days backfilled; 2026-10-20 left open")


@pytest.mark.parametrize(
    "argv",
    [
        ["--catch-up", "--only"],
        ["--catch-up", "--conclude"],
        ["--catch-up", "--for-date", "2026-10-19"],
        ["--conclude", "--only"],
        ["--conclude", "--for-date", "2026-10-19"],
    ],
)
def test_brief_main_refuses_flag_conflicts(stub, argv):
    with redirect_stdout(io.StringIO()):
        assert wc_brief.main(argv) != 0


@pytest.mark.parametrize(
    "argv",
    [
        ["--catch-up", "--only"],
        ["--catch-up", "--conclude"],
        ["--catch-up", "--for-date", "2026-10-19"],
        ["--conclude", "--only"],
        ["--conclude", "--for-date", "2026-10-19"],
    ],
)
def test_apply_main_refuses_flag_conflicts_before_running_anything(monkeypatch, argv):
    def _boom(**_k):
        raise AssertionError("apply() must not run")

    monkeypatch.setattr(wc_apply, "apply", _boom)
    with redirect_stdout(io.StringIO()):
        assert wc_apply.main(argv) != 0


def test_apply_main_threads_catch_up_into_apply(monkeypatch):
    seen = {}

    def _fake(**kwargs):
        seen.update(kwargs)
        return 0, {}

    monkeypatch.setattr(wc_apply, "apply", _fake)
    with redirect_stdout(io.StringIO()):
        assert wc_apply.main(["--catch-up"]) == 0
    assert seen["catch_up"] is True and "conclude" not in seen


def test_run_spec_threads_the_flag_into_the_brief_step_and_skips_the_open_day_audit():
    from coordinator_core.workday_complete import run_spec

    assert {"--catch-up", "--conclude"} <= run_spec.SPEC.launch_flags
    brief_step = run_spec.front_door({"--catch-up": True})[-1]
    assert list(brief_step.args) == ["brief", "--catch-up"]
    assert "--catch-up" not in list(run_spec.front_door({})[-1].args)

    envelope = {"directives": [], "period_mode": {"mode": "catch_up"}}
    groups = run_spec.agent_groups(envelope, [], None)
    assert "archive-audit" not in {g["id"] for g in groups}
    concluded = run_spec.agent_groups({"directives": [], "period_mode": {"mode": "conclude"}}, [], None)
    assert "archive-audit" in {g["id"] for g in concluded}


def test_for_date_only_keeps_single_date_semantics(stub):
    _, env = _brief(now_local=datetime(2026, 10, 20, 10, 48), for_date="2026-10-19", only_mode=True)
    directive_ids, _ = _ids(env)
    assert "d_step9_changelog" in directive_ids
    phase_b = next(d for d in env["directives"] if d["id"] == "d_step3_5_backfill_phase_b")
    assert "--for-date" in phase_b["args"]

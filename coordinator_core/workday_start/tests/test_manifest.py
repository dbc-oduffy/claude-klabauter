"""Manifest pins: dispatch closure, cost rows, stage order, record shape, cadence gates."""
from __future__ import annotations

import datetime as dt
import json
import os
import time
from pathlib import Path

import pytest

from coordinator_core.authz.dispatchable import ASSEMBLER_DISPATCHABLE
from coordinator_core.contract.decision_object.reader_result import build_directive
from coordinator_core.workday_start import briefing, contract, manifest

BIN_DIR = Path(__file__).resolve().parents[3] / "coordinator" / "bin"
TODAY = dt.date(2026, 10, 12)
ALL_STAGES = contract.LAUNCH_STAGES + contract.FINISH_STAGES
_KIND_KEYS = ("op", "inproc")


def _kind(record: dict) -> str:
    return next((k for k in _KIND_KEYS if k in record), "cli")


def _costs() -> dict:
    return json.loads(manifest.STEP_COSTS_PATH.read_text(encoding="utf-8"))["steps"]


def _all(tmp_path: Path, **kw):
    return manifest.steps_for(ALL_STAGES, repo_root=tmp_path, today=TODAY, **kw)


def test_dispatch_row_equals_consumes_manifest() -> None:
    assert ASSEMBLER_DISPATCHABLE["workday_start"] == manifest.CONSUMES_MANIFEST


def test_every_cli_step_is_dispatchable_and_has_a_bin_file() -> None:
    clis = {r["cli"] for r in manifest.STEP_TABLE if "cli" in r}
    assert clis <= ASSEMBLER_DISPATCHABLE["workday_start"]
    missing = sorted(
        c for c in manifest.CONSUMES_MANIFEST
        if not any((BIN_DIR / f"{c}{ext}").exists() for ext in (".py", ".cmd", ".ps1"))
    )
    assert missing == []


def test_orient_directive_clis_and_queue_append_are_admitted() -> None:
    assert manifest.ORIENT_DIRECTIVE_CLIS <= manifest.CONSUMES_MANIFEST
    assert "coordinator-queue-append" in manifest.CONSUMES_MANIFEST


def test_engine_steps_cost_at_or_under_the_bar() -> None:
    costs = _costs()
    over = []
    for spec in _all(Path(".")):
        if spec.cost_class != "engine":
            continue
        row = costs[spec.id]
        if row["process_ms_median"] is None or row["process_ms_median"] > 500:
            over.append(spec.id)
    assert over == []


def test_class_comes_from_the_costs_file_and_engine_gaps_are_not_started() -> None:
    costs = _costs()
    for spec in _all(Path(".")):
        if spec.id.startswith("gap-"):
            assert spec.cost_class == "over-bar"
            assert spec.detail.startswith(manifest.ENGINE_GAP_REASON)
        elif spec.id in costs:
            assert spec.cost_class == costs[spec.id]["cost_class"]
        else:
            assert spec.kind == "inproc" and spec.cost_class == "unmeasured"


def test_every_step_stage_is_in_the_contract_tuples_and_in_order() -> None:
    order = {s: i for i, s in enumerate(ALL_STAGES)}
    stages = [manifest.RUN_FIELDS[r["id"]]["stage"] for r in manifest.STEP_TABLE]
    assert set(stages) <= set(order)
    assert stages == sorted(stages, key=order.__getitem__)
    assert set(manifest.RUN_FIELDS) == {r["id"] for r in manifest.STEP_TABLE}
    assert len({r["id"] for r in manifest.STEP_TABLE}) == len(manifest.STEP_TABLE)


def test_decide_has_one_entry_and_the_judgment_finish_stages_have_none() -> None:
    by_stage: dict[str, list[str]] = {}
    for r in manifest.STEP_TABLE:
        by_stage.setdefault(manifest.RUN_FIELDS[r["id"]]["stage"], []).append(_kind(r))
    assert by_stage["decide"] == ["inproc"]
    assert "decisions" not in by_stage and "failure-backlog" not in by_stage


def test_records_have_exactly_the_directive_keys() -> None:
    live = set(build_directive("x", "y", [], "z"))
    for r in manifest.STEP_TABLE:
        kind = _kind(r)
        assert (set(r) - {kind}) | {"cli"} == live, r["id"]
        assert r["depends_on"] is None and r["already_satisfied"] is False, r["id"]


def test_on_exit_values_are_contract_rule_ids_and_only_step0_gates() -> None:
    gating = [
        (sid, f) for sid, f in manifest.RUN_FIELDS.items() if f.get("gates_next")
    ]
    assert [sid for sid, _ in gating] == ["step0-branch-setup"]
    on_exit = gating[0][1]["on_exit"]
    assert on_exit == {2: "step0-stale-branch", 3: "step0-reconcile-outcome"}
    assert set(on_exit.values()) <= set(contract.RULE_IDS)


def test_headings_are_briefing_sections() -> None:
    for spec in _all(Path(".")):
        assert spec.heading in briefing.SECTION_ORDER, spec.id


def test_doe_manifest_gap_legs_are_present() -> None:
    by_id = {r["id"]: r for r in manifest.STEP_TABLE}
    assert by_id["hyg-check-bin-forwarders"]["cli"] == "check-bin-forwarders"
    assert by_id["hyg-fleet-claude-md-handoff-convention"]["cli"] == "check-fleet-claude-md-handoff-convention"
    assert by_id["read-mcp-registration"]["inproc"].endswith(":mcp_registration")
    assert by_id["sentinel-check-fixture-sync"]["inproc"].endswith(":fixture_sync_presence")
    costs = _costs()
    for sid in ("hyg-check-bin-forwarders", "hyg-fleet-claude-md-handoff-convention"):
        assert costs[sid]["cost_class"] == "engine" and costs[sid]["process_ms_median"] <= 500


def test_killed_steps_are_not_entries() -> None:
    ids = " ".join(r["id"] for r in manifest.STEP_TABLE)
    for killed in ("reconcile-sweep", "cruft-sweep", "roadmap", "severity"):
        assert killed not in ids


def _touch_marker(root: Path, age_days: int) -> None:
    marker = root / manifest.WEEKLY_MARKER
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.touch()
    t = time.time() - age_days * 86400
    os.utime(marker, (t, t))


def _real_today() -> dt.date:
    return dt.date.today()


def _weekly_ids(root: Path, today: dt.date, **kw) -> tuple[set[str], set[str]]:
    owed = manifest.steps_for(("weekly", "backfill-scan"), repo_root=root, today=today, **kw)
    skipped = manifest.cadence_skipped(("weekly", "backfill-scan"), repo_root=root, today=today, **kw)
    return {s.id for s in owed}, {s.id for s, _ in skipped}


def test_weekly_group_skips_on_a_fresh_marker(tmp_path: Path) -> None:
    _touch_marker(tmp_path, 1)
    (tmp_path / "docs" / "plans").mkdir(parents=True)
    owed, skipped = _weekly_ids(tmp_path, _real_today(), freshness={"stdout": '{"doc_commit_delta": 3}'})
    assert owed == set()
    assert "weekly-orphan-branch-sweep" in skipped and "weekly-backfill-scan" in skipped
    assert "weekly-check-harvest-debt" in skipped


def test_weekly_group_runs_on_a_stale_marker(tmp_path: Path) -> None:
    _touch_marker(tmp_path, 30)
    (tmp_path / "docs" / "plans").mkdir(parents=True)
    owed, skipped = _weekly_ids(tmp_path, _real_today())
    assert "weekly-orphan-branch-sweep" in owed and "weekly-backfill-scan" in owed
    assert skipped == {"weekly-draft-plan-aging"}


def test_weekly_group_runs_with_no_marker(tmp_path: Path) -> None:
    owed, _ = _weekly_ids(tmp_path, _real_today())
    assert "weekly-orphan-branch-sweep" in owed


def test_draft_plan_aging_needs_five_plans(tmp_path: Path) -> None:
    plans = tmp_path / "docs" / "plans"
    plans.mkdir(parents=True)
    for i in range(4):
        (plans / f"p{i}.md").write_text("x")
    assert "weekly-draft-plan-aging" in _weekly_ids(tmp_path, TODAY)[1]
    (plans / "p4.md").write_text("x")
    assert "weekly-draft-plan-aging" in _weekly_ids(tmp_path, TODAY)[0]


def test_harvest_debt_is_daily_only_at_the_sentinel(tmp_path: Path) -> None:
    _touch_marker(tmp_path, 1)
    today = _real_today()
    sentinel = {"stdout": json.dumps({"result": {"doc_commit_delta": 99}})}
    assert "weekly-check-harvest-debt" in _weekly_ids(tmp_path, today, freshness=sentinel)[0]
    assert "weekly-check-harvest-debt" in _weekly_ids(tmp_path, today, freshness=None)[0]
    recent = {"stdout": json.dumps({"doc_commit_delta": 4})}
    assert "weekly-check-harvest-debt" in _weekly_ids(tmp_path, today, freshness=recent)[1]


def test_snippet_sync_runs_early_when_step0_changed_snippets(tmp_path: Path) -> None:
    _touch_marker(tmp_path, 1)
    today = _real_today()
    assert "weekly-verify-snippet-sync" in _weekly_ids(tmp_path, today)[1]
    changed = ["coordinator\\snippets\\example-retrieval-repo-preamble.md"]
    assert "weekly-verify-snippet-sync" in _weekly_ids(tmp_path, today, step0_changed_paths=changed)[0]


def test_outbox_surface_needs_a_file_in_the_outbox(tmp_path: Path) -> None:
    def ids() -> tuple[set[str], set[str]]:
        owed = manifest.steps_for(("memos",), repo_root=tmp_path, today=TODAY)
        skipped = manifest.cadence_skipped(("memos",), repo_root=tmp_path, today=TODAY)
        return {s.id for s in owed}, {s.id for s, _ in skipped}

    assert ids() == (set(), {"memo-outbox-surface"})
    outbox = tmp_path / manifest.OUTBOX_DIR
    outbox.mkdir(parents=True)
    assert ids() == (set(), {"memo-outbox-surface"})
    (outbox / "memo.md").write_text("x")
    assert ids() == ({"memo-outbox-surface"}, set())


def test_weekly_touch_runs_only_when_the_weekly_group_did(tmp_path: Path) -> None:
    _touch_marker(tmp_path, 1)
    fresh = manifest.steps_for(("marker",), repo_root=tmp_path, today=_real_today())
    assert "marker-weekly-touch" not in {s.id for s in fresh}
    stale_root = tmp_path / "stale"
    _touch_marker(stale_root, 30)
    stale = manifest.steps_for(("marker",), repo_root=stale_root, today=_real_today())
    assert "marker-weekly-touch" in {s.id for s in stale}


def test_touch_weekly_marker_creates_the_marker(tmp_path: Path) -> None:
    assert manifest.touch_weekly_marker(tmp_path) == (0, "")
    assert (tmp_path / manifest.WEEKLY_MARKER).is_file()


def test_placeholders_are_expanded(tmp_path: Path) -> None:
    specs = {s.id: s for s in _all(tmp_path)}
    assert "created>=2026-09-12" in specs["triage-query-completions"].args
    assert specs["rollup-stall-report"].args == ('{"today":"2026-10-12"}',)
    assert json.loads(specs["hyg-git-maintenance-daily"].args[0])["repo"] == tmp_path.as_posix()
    assert not [
        a for s in specs.values() for a in s.args if "{" in a and not a.startswith(("{\"", "{}"))
    ]


def test_goal_event_and_orientation_cache_gates(tmp_path: Path) -> None:
    def skipped(stage: str) -> set[str]:
        return {s.id for s, _ in manifest.cadence_skipped((stage,), repo_root=tmp_path, today=TODAY)}

    assert skipped("goal-event") == {"goal-append-goal-event"}
    assert skipped("orientation-cache") == {"cache-orientation-cache"}
    (tmp_path / "tasks").mkdir()
    target = tmp_path / manifest.GOAL_EVENT_TEXT
    target.parent.mkdir(parents=True)
    target.write_text("x")
    assert skipped("goal-event") == set() and skipped("orientation-cache") == set()


def test_step0_is_first_of_its_stage_and_gates() -> None:
    spec = next(s for s in _all(Path(".")) if s.stage == "step0")
    assert spec.id == "step0-branch-setup" and spec.gates_next
    assert dict(spec.on_exit) == {2: "step0-stale-branch", 3: "step0-reconcile-outcome"}


def test_unknown_gate_raises(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        manifest._gate_reason(
            "nope", repo_root=tmp_path, today=TODAY, freshness=None, step0_changed_paths=()
        )

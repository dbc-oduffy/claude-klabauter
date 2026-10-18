"""close_out_and_stamp._determine_shipped counts an eligible, em-discharged performer-em row delivered."""

from __future__ import annotations

from pathlib import Path

import yaml

import coordinator_core.execute_plan_assemble.close_out_and_stamp as coas
from coordinator_core.ops.dispatch_emit import sizing_fire, spine_read
from coordinator_core.ops.plan_tasks_mutate import EM_DISCHARGE_KEY, evidence_sidecar_path

_PLAN_REL = "docs/plans/plan.md"


def _plan(rows: list[dict]) -> str:
    spine = yaml.safe_dump(rows, sort_keys=False)
    return f"---\ntitle: t\n---\n\n# t\n\n## Tasks\n\n```yaml plan-tasks\n{spine}```\n"


def _row(row_id: str, **extra) -> dict:
    return {"id": row_id, "title": row_id, "disposition": "open", **extra}


def _write(tmp_path: Path, rows: list[dict], discharged: list[str]) -> str:
    plan = tmp_path / _PLAN_REL
    plan.parent.mkdir(parents=True)
    text = _plan(rows)
    plan.write_text(text, encoding="utf-8")
    if discharged:
        entries = {i: {"recorded_at": "2026-10-18T00:00:00Z", "text": "performed"} for i in discharged}
        evidence_sidecar_path(plan).write_text(yaml.safe_dump({EM_DISCHARGE_KEY: entries}), encoding="utf-8")
    return text


def _em_rows(*ids: str) -> list[dict]:
    return [_row(i, performer="em") for i in ids]


def test_em_discharged_rows_without_sha_are_delivered(tmp_path: Path):
    text = _write(tmp_path, _em_rows("V1", "V2", "V3"), ["V1", "V2", "V3"])
    shipped, missing, backed, error = coas._determine_shipped(text, _PLAN_REL, tmp_path)
    assert (shipped, missing, backed, error) == (True, [], True, None)


def test_em_row_without_discharge_entry_is_missing(tmp_path: Path):
    text = _write(tmp_path, _em_rows("V1", "V2", "V3"), ["V1", "V2"])
    shipped, missing, _backed, _error = coas._determine_shipped(text, _PLAN_REL, tmp_path)
    assert (shipped, missing) == (False, ["V3"])


def test_non_em_row_with_discharge_entry_is_missing(tmp_path: Path):
    rows = _em_rows("V1") + [_row("C1")]
    text = _write(tmp_path, rows, ["V1", "C1"])
    shipped, missing, _backed, _error = coas._determine_shipped(text, _PLAN_REL, tmp_path)
    assert (shipped, missing) == (False, ["C1"])


def test_gated_em_row_with_discharge_entry_is_missing(tmp_path: Path):
    rows = [_row("V1", performer="em", external_gate=[{"id": "g", "cleared": False}])]
    text = _write(tmp_path, rows, ["V1"])
    shipped, missing, _backed, _error = coas._determine_shipped(text, _PLAN_REL, tmp_path)
    assert (shipped, missing) == (False, ["V1"])


def test_one_predicate_shared_by_stamp_and_sizing_fire():
    import inspect

    assert "read_em_discharges" not in inspect.getsource(sizing_fire)
    assert "read_em_discharges" not in inspect.getsource(coas)
    assert callable(spine_read.drop_em_discharged)

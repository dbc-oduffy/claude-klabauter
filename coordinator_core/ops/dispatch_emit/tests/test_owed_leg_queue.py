"""owed_leg_queue: owed legs persist per halt and drop once the plan discharges them."""

from __future__ import annotations

import yaml

from coordinator_core.ops.dispatch_emit.owed_leg_queue import (
    open_owed_legs,
    render_owed_legs,
    write_owed_legs,
)

PLAN = "docs/plans/p.md"


def _plan(root, dispositions):
    rows = "".join(
        f"- id: {rid}\n  title: t\n  change_kind: code-edit\n  surface: a.md\n"
        f"  writes: [a.md]\n  disposition: {disp}\n  body: b\n"
        for rid, disp in dispositions.items()
    )
    path = root / PLAN
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "---\ntitle: p\n---\n\n# p\n\n## Tasks\n\n```yaml plan-tasks\n" + rows + "```\n",
        encoding="utf-8",
    )
    return path


OWED = [
    {"id": "E1", "reason": "em-performed", "detail": "wire it"},
    {"id": "O1", "reason": "operator", "detail": "slot leg"},
]


def test_write_persists_one_yaml_per_owed_row(tmp_path):
    paths = write_owed_legs(tmp_path, "run-1", PLAN, OWED)
    assert [p.name for p in paths] == ["run-1--E1.yaml", "run-1--O1.yaml"]
    record = yaml.safe_load(paths[0].read_text(encoding="utf-8"))
    assert {k: record[k] for k in ("id", "run_id", "plan", "reason", "detail")} == {
        "id": "E1", "run_id": "run-1", "plan": PLAN, "reason": "em-performed", "detail": "wire it",
    }
    assert record["created"]
    assert b"\r\n" not in paths[0].read_bytes()


def test_write_is_idempotent_and_keeps_created(tmp_path):
    first = write_owed_legs(tmp_path, "run-1", PLAN, OWED)[0]
    stamped = yaml.safe_load(first.read_text(encoding="utf-8"))
    stamped["created"] = "2020-01-01T00:00:00Z"
    first.write_text(yaml.safe_dump(stamped), encoding="utf-8")
    write_owed_legs(tmp_path, "run-1", PLAN, [{"id": "E1", "reason": "em-performed", "detail": "new"}])
    record = yaml.safe_load(first.read_text(encoding="utf-8"))
    assert record["created"] == "2020-01-01T00:00:00Z"
    assert record["detail"] == "new"
    assert len(list((tmp_path / "state" / "owed-legs").glob("*.yaml"))) == 2


def test_write_skips_rows_without_an_id(tmp_path):
    assert write_owed_legs(tmp_path, "r", PLAN, [{"reason": "operator"}, {"id": "", "reason": "x"}]) == []


def test_open_lists_every_leg_while_the_plan_rows_are_open(tmp_path):
    _plan(tmp_path, {"E1": "open", "O1": "open"})
    write_owed_legs(tmp_path, "run-1", PLAN, OWED)
    assert [r["id"] for r in open_owed_legs(tmp_path)] == ["E1", "O1"]


def test_disposition_off_open_discharges_the_leg(tmp_path):
    _plan(tmp_path, {"E1": "open", "O1": "coded"})
    write_owed_legs(tmp_path, "run-1", PLAN, OWED)
    assert [r["id"] for r in open_owed_legs(tmp_path)] == ["E1"]


def test_em_discharge_in_the_evidence_sidecar_discharges_an_open_row(tmp_path):
    plan = _plan(tmp_path, {"E1": "open", "O1": "open"})
    write_owed_legs(tmp_path, "run-1", PLAN, OWED)
    plan.with_name("p.evidence.yaml").write_text(
        yaml.safe_dump({"em_discharged": {"E1": {"recorded_at": "2026-11-04T00:00:00Z", "text": "done"}}}),
        encoding="utf-8",
    )
    assert [r["id"] for r in open_owed_legs(tmp_path)] == ["O1"]


def test_an_unreadable_plan_leaves_the_leg_open(tmp_path):
    write_owed_legs(tmp_path, "run-1", PLAN, OWED)
    assert [r["id"] for r in open_owed_legs(tmp_path)] == ["E1", "O1"]


def test_no_queue_dir_and_malformed_files_read_as_empty(tmp_path):
    assert open_owed_legs(tmp_path) == []
    directory = tmp_path / "state" / "owed-legs"
    directory.mkdir(parents=True)
    (directory / "bad.yaml").write_text(": : [", encoding="utf-8")
    (directory / "list.yaml").write_text("- a\n", encoding="utf-8")
    assert open_owed_legs(tmp_path) == []


def test_render_one_line_per_row():
    rows = [{"run_id": "r1", "id": "E1", "reason": "em-performed", "plan": PLAN}]
    assert render_owed_legs(rows) == f"OWED r1 E1 (em-performed) {PLAN}"
    assert render_owed_legs([]) == ""

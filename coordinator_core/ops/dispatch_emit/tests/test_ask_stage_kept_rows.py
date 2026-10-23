"""A kept row stages with its DONE report in place and kept_from stamped; anything else stages as today."""

from __future__ import annotations

from pathlib import Path

from coordinator_core.ops.dispatch_emit import kept_work, requirements_ledger, resume_snapshot, row_returns
from coordinator_core.ops.dispatch_emit.ask_stage import _handler
from coordinator_core.ops.dispatch_emit.emit import _dispatch_report_path
from coordinator_core.ops.dispatch_emit.tests.test_ask_stage import read_stage_reply

PLAN_REL = "docs/plans/p.md"
RUN = "r2"
KEPT_RUN = "r1"
WRITES = {"C1": "docs/x/C1.md", "C2": "docs/x/C2.md"}


def _plan(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir(exist_ok=True)
    rows = "".join(
        f"- id: {rid}\n  title: t{rid}\n  change_kind: doc-edit\n  surface: {w}\n  writes:\n    - {w}\n"
        for rid, w in WRITES.items()
    )
    plan = tmp_path / PLAN_REL
    plan.parent.mkdir(parents=True, exist_ok=True)
    plan.write_text(
        f"---\ntitle: p\n---\n\n# p\n\n## Tasks\n\n```yaml plan-tasks\n{rows}```\n", encoding="utf-8", newline="\n"
    )


def _product(tmp_path: Path, rid: str, report: str) -> None:
    for rel, text in ((WRITES[rid], "bytes"), (_dispatch_report_path(PLAN_REL, rid), report)):
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")
    kept_work.write(
        tmp_path / "state/warp" / KEPT_RUN,
        {rid: {"paths": {WRITES[rid]: kept_work.fingerprint(tmp_path, WRITES[rid])}}},
        None,
    )


def _stage(tmp_path: Path, kept: tuple = ("C1",)) -> dict:
    resume_snapshot.write(tmp_path, RUN, PLAN_REL, [], ["C1", "C2"], kept=kept, kept_run=KEPT_RUN if kept else None)
    return read_stage_reply(
        tmp_path, _handler({"run_id": RUN, "plan_path": PLAN_REL, "only_rows": ["C1", "C2"]}, repo_root=tmp_path)
    )


def test_a_kept_row_keeps_its_report_and_is_stamped(tmp_path):
    _plan(tmp_path)
    _product(tmp_path, "C1", "DONE: C1")
    reply = _stage(tmp_path)
    rows = {r["id"]: r for r in reply["rows"]}
    report = tmp_path / _dispatch_report_path(PLAN_REL, "C1")
    assert report.read_text(encoding="utf-8") == "DONE: C1"
    assert not report.with_name("C1.prior.md").exists()
    assert rows["C1"]["kept_from"] == KEPT_RUN
    assert rows["C1"]["writes"] == [WRITES["C1"]]
    assert "kept_from" not in rows["C2"]
    assert KEPT_RUN in (tmp_path / rows["C1"]["brief_path"]).read_text(encoding="utf-8")


def test_a_kept_row_whose_report_reads_blocked_is_staged_normally(tmp_path):
    _plan(tmp_path)
    _product(tmp_path, "C1", "BLOCKED: C1")
    rows = {r["id"]: r for r in _stage(tmp_path)["rows"]}
    report = tmp_path / _dispatch_report_path(PLAN_REL, "C1")
    assert "kept_from" not in rows["C1"]
    assert not report.exists()
    assert report.with_name("C1.prior.md").read_text(encoding="utf-8") == "BLOCKED: C1"


def test_a_kept_row_whose_bytes_moved_is_staged_normally(tmp_path):
    _plan(tmp_path)
    _product(tmp_path, "C1", "DONE: C1")
    (tmp_path / WRITES["C1"]).write_text("moved", encoding="utf-8")
    rows = {r["id"]: r for r in _stage(tmp_path)["rows"]}
    assert "kept_from" not in rows["C1"]
    assert (tmp_path / _dispatch_report_path(PLAN_REL, "C1")).with_name("C1.prior.md").is_file()


def test_a_snapshot_without_kept_stages_as_today(tmp_path):
    _plan(tmp_path)
    _product(tmp_path, "C1", "DONE: C1")
    rows = {r["id"]: r for r in _stage(tmp_path, kept=())["rows"]}
    assert all("kept_from" not in r for r in rows.values())
    assert (tmp_path / _dispatch_report_path(PLAN_REL, "C1")).with_name("C1.prior.md").is_file()


def _empty_plan(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir(exist_ok=True)
    rows = (
        "- id: C2\n  title: tC2\n  change_kind: doc-edit\n  surface: docs/x/C2.md\n  writes: []\n"
        "- id: C3\n  title: tC3\n  change_kind: doc-edit\n  surface: docs/x/C3.md\n  writes:\n    - docs/x/C3.md\n"
        "  depends_on:\n    - chunk: C2\n      gate_kind: output-consumption-runtime\n      note: reads figures\n"
    )
    plan = tmp_path / PLAN_REL
    plan.parent.mkdir(parents=True, exist_ok=True)
    plan.write_text(
        f"---\ntitle: p\n---\n\n# p\n\n## Tasks\n\n```yaml plan-tasks\n{rows}```\n", encoding="utf-8", newline="\n"
    )


def _persist_empty(tmp_path: Path, text: str = "DONE: figures 960449 B") -> None:
    record = tmp_path / "state/warp" / KEPT_RUN
    row_returns.write(record, {"C2": text})
    kept_work.write(record, {"C2": {"paths": {}, "return": row_returns.digest(text)}}, None)


def _stage_empty(tmp_path: Path, kept: tuple) -> dict:
    resume_snapshot.write(tmp_path, RUN, PLAN_REL, [], ["C2", "C3"], kept=kept, kept_run=KEPT_RUN if kept else None)
    return read_stage_reply(
        tmp_path, _handler({"run_id": RUN, "plan_path": PLAN_REL, "only_rows": ["C2", "C3"]}, repo_root=tmp_path)
    )


def test_a_writes_empty_row_with_a_persisted_done_return_stages_kept_and_briefs_its_dependent(tmp_path):
    _empty_plan(tmp_path)
    _persist_empty(tmp_path)
    rows = {r["id"]: r for r in _stage_empty(tmp_path, ("C2",))["rows"]}
    assert rows["C2"]["kept_from"] == KEPT_RUN
    assert "kept_from" not in rows["C3"]
    brief = (tmp_path / rows["C3"]["brief_path"]).read_text(encoding="utf-8")
    assert f"state/warp/{KEPT_RUN}/returns.json" in brief
    assert 'under key "C2"' in brief


def test_a_writes_empty_row_named_by_a_note_stages_fresh(tmp_path):
    _empty_plan(tmp_path)
    _persist_empty(tmp_path)
    notes = tmp_path / "state/warp" / KEPT_RUN / requirements_ledger.NOTES_FILE
    notes.write_text('{"note": "redo C2 by hand"}\n', encoding="utf-8", newline="\n")
    rows = {r["id"]: r for r in _stage_empty(tmp_path, ("C2",))["rows"]}
    assert "kept_from" not in rows["C2"]
    assert "returns.json" not in (tmp_path / rows["C3"]["brief_path"]).read_text(encoding="utf-8")


def test_a_writes_empty_row_whose_persisted_return_is_not_done_stages_fresh(tmp_path):
    _empty_plan(tmp_path)
    _persist_empty(tmp_path, "BLOCKED: no figures")
    rows = {r["id"]: r for r in _stage_empty(tmp_path, ("C2",))["rows"]}
    assert "kept_from" not in rows["C2"]
    assert "returns.json" not in (tmp_path / rows["C3"]["brief_path"]).read_text(encoding="utf-8")

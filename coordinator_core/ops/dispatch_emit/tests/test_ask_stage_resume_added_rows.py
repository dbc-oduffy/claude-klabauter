"""With the gate's resume snapshot, the resume stage keeps every row outside not_to_run."""

from __future__ import annotations

from pathlib import Path

from coordinator_core.ops.dispatch_emit import resume_snapshot
from coordinator_core.ops.dispatch_emit.ask_stage import _handler
from coordinator_core.ops.dispatch_emit.tests.test_ask_stage import read_stage_reply

PLAN_REL = "docs/plans/p.md"
RUN = "r1"


def _dep(*ids: str) -> str:
    edges = "".join(f"    - chunk: {i}\n      gate_kind: output-consumption-runtime\n" for i in ids)
    return f"  depends_on:\n{edges}" if ids else ""


def _row(rid: str, extra: str = "") -> str:
    return (
        f"- id: {rid}\n  title: t{rid}\n  change_kind: doc-edit\n  surface: docs/x/{rid}.md\n"
        f"  writes:\n    - docs/x/{rid}.md\n{extra}"
    )


def _plan(tmp_path: Path, rows: str) -> None:
    (tmp_path / ".git").mkdir(exist_ok=True)
    plan = tmp_path / PLAN_REL
    plan.parent.mkdir(parents=True, exist_ok=True)
    plan.write_text(
        f"---\ntitle: p\n---\n\n# p\n\n## Tasks\n\n```yaml plan-tasks\n{rows}```\n",
        encoding="utf-8",
        newline="\n",
    )


def _stage(tmp_path: Path, only_rows: list[str]) -> dict:
    return read_stage_reply(
        tmp_path, _handler({"run_id": RUN, "plan_path": PLAN_REL, "only_rows": only_rows}, repo_root=tmp_path)
    )


def _replanned(extra_row: str = "") -> str:
    return (
        _row("C20")
        + _row("C21", _dep("C20", "C22", "C23"))
        + _row("C22", _dep("C20"))
        + _row("C23")
        + extra_row
    )


def test_added_rows_stage_and_dependant_waits(tmp_path):
    _plan(tmp_path, _replanned())
    resume_snapshot.write(tmp_path, RUN, PLAN_REL, ["C20"], ["C21"])
    reply = _stage(tmp_path, ["C21"])
    assert "error" not in reply
    rows = {r["id"]: r for r in reply["rows"]}
    assert set(rows) == {"C21", "C22", "C23"}
    assert rows["C21"]["wave"] > rows["C22"]["wave"]
    assert rows["C21"]["wave"] > rows["C23"]["wave"]


def test_not_to_run_row_is_never_staged_and_its_edge_is_stripped(tmp_path):
    _plan(tmp_path, _replanned())
    resume_snapshot.write(tmp_path, RUN, PLAN_REL, ["C20"], ["C21"])
    reply = _stage(tmp_path, ["C21"])
    assert "C20" not in {r["id"] for r in reply["rows"]}
    rows = {r["id"]: r for r in reply["rows"]}
    assert rows["C22"]["wave"] == 0


def test_added_em_row_appears_owed(tmp_path):
    _plan(tmp_path, _replanned(_row("E9", "  performer: em\n")))
    resume_snapshot.write(tmp_path, RUN, PLAN_REL, ["C20"], ["C21"])
    reply = _stage(tmp_path, ["C21"])
    assert [o["id"] for o in reply["owed"]] == ["E9"]


def test_without_snapshot_only_rows_is_an_exact_set(tmp_path):
    _plan(tmp_path, _replanned())
    reply = _stage(tmp_path, ["C21"])
    assert [r["id"] for r in reply["rows"]] == ["C21"]
    assert reply["rows"][0]["wave"] == 0


def test_snapshot_for_another_plan_is_ignored(tmp_path):
    _plan(tmp_path, _replanned())
    resume_snapshot.write(tmp_path, RUN, "docs/plans/other.md", ["C20"], ["C21"])
    reply = _stage(tmp_path, ["C21"])
    assert [r["id"] for r in reply["rows"]] == ["C21"]


def test_only_rows_id_missing_from_plan_still_refuses(tmp_path):
    _plan(tmp_path, _replanned())
    resume_snapshot.write(tmp_path, RUN, PLAN_REL, ["C20"], ["C21"])
    reply = _stage(tmp_path, ["C99"])
    assert "C99" in str(reply.get("error", reply))

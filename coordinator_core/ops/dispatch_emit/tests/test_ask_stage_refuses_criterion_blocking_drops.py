"""`dispatch.ask_stage` refuses at staging when an unstaged row sits behind an uncoded predecessor plan."""

from __future__ import annotations

from pathlib import Path

from coordinator_core.ops.dispatch_emit.ask_stage import _handler
from coordinator_core.ops.dispatch_emit.tests.test_ask_stage import read_stage_reply
from coordinator_core.ops.dispatch_emit.tests.test_ask_stage_resume_added_rows import _row

PLAN_REL = "docs/plans/p.md"
PRED_REL = "docs/plans/pred.md"
SIZING = "state/sizings/pred-sizing.yaml"
RUN = "r1"

_HELD = (
    "- id: B\n  title: tB\n  change_kind: doc-edit\n  surface: docs/x/B.md\n  writes:\n    - docs/x/B.md\n"
    f"  depends_on_plan:\n    - plan: {PRED_REL}\n      chunk: P1\n      gate_kind: output-consumption-runtime\n"
)
_TAIL = (
    "- id: T\n  title: tT\n  change_kind: doc-edit\n  surface: docs/x/T.md\n  writes:\n    - docs/x/T.md\n"
    "  depends_on:\n    - chunk: B\n      gate_kind: output-consumption-runtime\n"
)


def _plan(path: Path, rows: str, fm: str = "title: p\n") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\n{fm}---\n\n# p\n\n## Tasks\n\n```yaml plan-tasks\n{rows}```\n", encoding="utf-8", newline="\n"
    )


def _setup(tmp_path: Path, pred_disposition: str, rows: str) -> None:
    (tmp_path / ".git").mkdir(exist_ok=True)
    extra = "" if pred_disposition == "open" else f"  disposition: {pred_disposition}\n"
    _plan(tmp_path / PRED_REL, _row("P1", extra), fm=f"title: pred\nsizing_object: {SIZING}\n")
    _plan(tmp_path / PLAN_REL, rows)


def _stage(tmp_path: Path, **extra) -> dict:
    params = {"run_id": RUN, "plan_path": PLAN_REL, **extra}
    return read_stage_reply(tmp_path, _handler(params, repo_root=tmp_path))


def test_uncoded_predecessor_refuses_with_blocking_drops(tmp_path):
    _setup(tmp_path, "open", _row("A") + _HELD)
    reply = _stage(tmp_path)
    assert reply["error"].startswith("CRITERION-UNREACHABLE-AT-STAGING:")
    assert "B" in reply["error"] and SIZING in reply["error"]
    [drop] = reply["criterion_blocking_drops"]
    assert drop["id"] == "B" and "predecessor not yet coded" in drop["reason"]
    assert drop["predecessor_plan"] == PRED_REL and drop["predecessor_sizing"] == SIZING
    assert not list(tmp_path.rglob("manifest.json"))
    notes = list(tmp_path.rglob("notes.jsonl"))
    assert notes and '"staging_drop"' in notes[0].read_text(encoding="utf-8")


def test_coded_predecessor_stages_both(tmp_path):
    _setup(tmp_path, "coded", _row("A") + _HELD)
    reply = _stage(tmp_path)
    assert "error" not in reply
    assert {r["id"] for r in reply["rows"]} == {"A", "B"}


def test_transitive_tail_is_named_too(tmp_path):
    _setup(tmp_path, "open", _row("A") + _HELD + _TAIL)
    reply = _stage(tmp_path)
    assert {d["id"] for d in reply["criterion_blocking_drops"]} == {"B", "T"}
    assert {d["predecessor_sizing"] for d in reply["criterion_blocking_drops"]} == {SIZING}


def test_predecessor_without_sizing_object_names_none(tmp_path):
    _setup(tmp_path, "open", _row("A") + _HELD)
    _plan(tmp_path / PRED_REL, _row("P1"))
    [drop] = _stage(tmp_path)["criterion_blocking_drops"]
    assert drop["predecessor_sizing"] is None and drop["predecessor_plan"] == PRED_REL


def test_zero_dispatchable_refusal_carries_the_key(tmp_path):
    _setup(tmp_path, "open", _HELD)
    reply = _stage(tmp_path)
    assert "zero dispatchable" in reply["error"]
    assert [d["id"] for d in reply["criterion_blocking_drops"]] == ["B"]


def test_only_rows_narrowing_past_the_held_row_does_not_refuse(tmp_path):
    _setup(tmp_path, "open", _row("A") + _HELD)
    reply = _stage(tmp_path, only_rows=["A"])
    assert "error" not in reply
    assert {r["id"] for r in reply["rows"]} == {"A"}

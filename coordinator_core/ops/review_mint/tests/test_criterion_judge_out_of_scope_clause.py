"""A terminal judge prompt names an out-of-scope-ruled row's clause as struck, never not_met."""
from __future__ import annotations

from pathlib import Path

from coordinator_core.ops.dispatch_emit.emit import _struck_judge_rows
from coordinator_core.ops.review_mint.execute_review import (
    LEDGER_STRUCK_RULE,
    STRUCK_RULE,
    compose_criterion_judge,
    struck_rows_clause,
)
from coordinator_core.ops.review_mint.tests.test_execute_review import _STAGE_SCHEMAS, _judge_review

_SIZING_REL = "state/sizings/2026-10-27-x.yaml"
_PLAN = f"---\nsizing_object: {_SIZING_REL}\n---\n# plan\n"
_SIZING = """\
intent: x
requirement_register:
  rows:
  - id: R1
    source_text: the far-side jaw contour is labelled
    status: deferred
    ruling:
      source: pm
      quote: out of scope
      ref: workflow.revise:run-1:out_of_scope
  - id: R2
    source_text: the near side is labelled
    status: open
"""


def _call(**kw):
    return compose_criterion_judge(
        _judge_review(),
        stage_schemas={**_STAGE_SCHEMAS, "judge-result": {"type": "object"}},
        plan_path="docs/plans/example.md",
        run_base_sha="a" * 40,
        falsifier=None,
        **kw,
    )


def _repo(tmp_path: Path) -> Path:
    (tmp_path / "state" / "sizings").mkdir(parents=True)
    (tmp_path / _SIZING_REL).write_text(_SIZING, encoding="utf-8")
    return tmp_path


def test_only_the_ruled_out_row_is_struck(tmp_path):
    rows = _struck_judge_rows(_PLAN, _repo(tmp_path))
    assert rows == [{"id": "R1", "text": "the far-side jaw contour is labelled"}]


def test_struck_row_reaches_the_judge_prompt_with_the_rule(tmp_path):
    call = _call(struck_rows=_struck_judge_rows(_PLAN, _repo(tmp_path)))
    assert "R1: the far-side jaw contour is labelled" in call
    assert "`out_of_scope`" in call and "never grade it `not_met`" in call
    assert "R2: the near side" not in call


def test_no_ruling_leaves_the_prompt_without_the_clause(tmp_path):
    (tmp_path / "state" / "sizings").mkdir(parents=True)
    (tmp_path / _SIZING_REL).write_text(_SIZING.replace("status: deferred", "status: open"), encoding="utf-8")
    assert _struck_judge_rows(_PLAN, tmp_path) == []
    assert _call(struck_rows=[]) == _call()
    assert struck_rows_clause([]) == "" and STRUCK_RULE not in _call()


def test_ledger_folded_judge_carries_the_standing_rule():
    assert LEDGER_STRUCK_RULE in _call(ledger_charter="LEDGER CHARTER TEXT")
    assert LEDGER_STRUCK_RULE not in _call()

"""P-G6 replay: the acceptance oracle ran non-default args while the falsifier called defaults."""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import falsifier_integrity_phase as fip

_PLAN = """---
title: x
prime_exit_criterion:
  statement: the gate can fail
  falsifier:
    how: {how}
    baseline_output: green today
    baseline_ref: abc1234
    expected_when_true: red
---
# Plan

## Acceptance Criteria
| AC-1 | `{oracle}` is green |

## Tasks
"""


@pytest.fixture(autouse=True)
def _rubric(tmp_path_factory, monkeypatch):
    path = tmp_path_factory.mktemp("rubric") / "r.md"
    path.write_text("rubric\n", encoding="utf-8")
    monkeypatch.setattr(fip, "_reviewer_rubric_path", lambda: path)


def _plan(how: str, oracle: str) -> str:
    return _PLAN.format(how=how, oracle=oracle)


def test_differing_argument_sets_are_refused():
    text = _plan("python tools/inst.py", "python tools/inst.py --strict --rows 5")
    msg = fip.oracle_falsifier_arg_mismatch(text)
    assert msg is not None and "tools/inst.py" in msg


def test_identical_argument_sets_pass_in_any_order():
    text = _plan("python tools/inst.py --a --b", "python tools/inst.py --b --a")
    assert fip.oracle_falsifier_arg_mismatch(text) is None


def test_oracle_naming_another_instrument_is_not_compared():
    text = _plan("python tools/inst.py", "python tools/other.py --x")
    assert fip.oracle_falsifier_arg_mismatch(text) is None


def test_review_inputs_refuses_a_mismatched_plan(tmp_path: Path):
    (tmp_path / "tools").mkdir()
    (tmp_path / "tools" / "inst.py").write_text("print('ok')\n", encoding="utf-8")
    rel = "docs/plans/p.md"
    (tmp_path / rel).parent.mkdir(parents=True)
    (tmp_path / rel).write_text(
        _plan("python tools/inst.py", "python tools/inst.py --strict"), encoding="utf-8"
    )
    with pytest.raises(fip.OracleFalsifierArgMismatch):
        fip.review_inputs([rel], repo_root=tmp_path, report_dir=Path("reports"))


def test_review_inputs_accepts_a_matching_plan(tmp_path: Path):
    (tmp_path / "tools").mkdir()
    (tmp_path / "tools" / "inst.py").write_text("print('ok')\n", encoding="utf-8")
    rel = "docs/plans/p.md"
    (tmp_path / rel).parent.mkdir(parents=True)
    (tmp_path / rel).write_text(
        _plan("python tools/inst.py --strict", "python tools/inst.py --strict"), encoding="utf-8"
    )
    [inp] = fip.review_inputs([rel], repo_root=tmp_path, report_dir=Path("reports"))
    assert inp.plan_path == rel

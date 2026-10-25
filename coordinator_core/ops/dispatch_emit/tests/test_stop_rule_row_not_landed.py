"""A row whose executor fired a stop rule is carried by its checkpoint, never landed."""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit.checkpoint_stamp import STOP_RULE_TRAILER
from coordinator_core.ops.dispatch_emit.emit import landed_rows_from_text
from coordinator_core.ops.dispatch_emit.tests.test_ask_compose import _compose

PLAN = "docs/plans/a.md"
_GOLDEN = (
    Path(__file__).resolve().parents[3]
    / "benchmarks" / "fixtures" / "emit_full_path_plan.expected.script.mjs"
)
_SUBJECT = "checkpoint(wave 0): 2 rows — L6, L7"
_LINE = f"{STOP_RULE_TRAILER}: L7"


def test_plan_route_committer_writes_the_stop_rule_line():
    script = _GOLDEN.read_text(encoding="utf-8")
    assert "const stopped = done.filter((i) => _stoppedBy.includes(i));" in script
    assert f"'\\n{STOP_RULE_TRAILER}: ' + stopped.join(', ')" in script


def test_ask_route_committer_writes_the_stop_rule_line():
    script = _compose()
    assert "const stopped = done.filter((i) => _stoppedBy.includes(i));" in script
    assert f"'\\n{STOP_RULE_TRAILER}: ' + stopped.join(', ')" in script


@pytest.mark.parametrize("sep", ["\n", "\\n"])
def test_text_reader_omits_the_stopped_row_in_both_newline_shapes(sep):
    text = f"abc {_SUBJECT}{sep}{sep}Checkpoint-Plan: {PLAN}{sep}{_LINE}"
    assert landed_rows_from_text(text) == {"L6"}


def test_text_reader_keeps_a_row_a_later_subject_names_bare():
    text = f"{_SUBJECT}\n\n{_LINE}\ndef checkpoint(wave 1): 1 rows — L7\n"
    assert landed_rows_from_text(text) == {"L6", "L7"}


def test_text_reader_keeps_the_stopped_row_for_review():
    text = f"{_SUBJECT}\n\n{_LINE}"
    assert landed_rows_from_text(text, exclude_stop_rule=False) == {"L6", "L7"}

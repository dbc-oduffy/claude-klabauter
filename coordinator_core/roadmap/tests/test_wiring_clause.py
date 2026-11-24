"""wiring_clause_refusal accepts a statement naming an entry point and a check, refuses the rest."""

from __future__ import annotations

import pytest

from coordinator_core.roadmap.post_stamp_clause import (
    EXIT_CRITERION_REFUSAL_HEAD,
    REMEDIATION_MARKER,
)
from coordinator_core.roadmap.wiring_clause import wiring_clause_refusal

PM_EXIT = (
    "`coordinator-invoke sizing.accept_exit_criterion` refuses a criterion that names no wiring "
    "path, and tests covering touched files pass"
)


@pytest.mark.parametrize(
    "text",
    [
        PM_EXIT,
        "`coordinator-invoke sizing.accept_exit_criterion` refuses it; `python -m pytest a/test_x.py` passes",
        "sizing.accept_exit_criterion refuses it, verified by python -m pytest coordinator_core/ops/tests",
        "`python bin/claude-klabauter-doctor-probe.py --json` exits 0 and `grep -c wired x.py` prints 1",
        "coordinator/bin/publish.py runs and coordinator_core/tests/test_publish.py passes",
    ],
)
def test_entry_point_and_check_pass(text):
    assert wiring_clause_refusal(text) is None


@pytest.mark.parametrize(
    "text,missing",
    [
        ("the feature works end to end", "wiring path"),
        ("we did our best and it is done", "runnable check"),
        ("", "wiring path"),
        (None, "wiring path"),
        ("`coordinator-invoke plan.validate` is reachable", None),
        ("see plan.md and notes.txt; `pytest -q` passes", "wiring path"),
        ("tests covering touched files pass", "wiring path"),
    ],
)
def test_refused_statements(text, missing):
    msg = wiring_clause_refusal(text)
    if missing is None:
        # a backticked entry point with arguments is itself a runnable command
        assert msg is None
        return
    assert msg is not None
    assert msg.startswith(EXIT_CRITERION_REFUSAL_HEAD)
    assert REMEDIATION_MARKER in msg
    assert missing in msg


def test_no_check_names_only_the_check_as_missing():
    msg = wiring_clause_refusal("the op sizing.accept_exit_criterion is wired")
    assert "runnable check" in msg and "wiring path" not in msg


def test_refusal_is_one_line_with_template():
    msg = wiring_clause_refusal("it works")
    assert "\n" not in msg
    assert msg.split(REMEDIATION_MARKER, 1)[1].startswith("name the entry point and the check")

"""A refire reports a row held behind an unfinished epistemic-premise predecessor with a reason."""

from __future__ import annotations

import os
import time

from coordinator_core.ops.dispatch_emit import resume_snapshot
from coordinator_core.ops.dispatch_emit.tests.conftest import write_run_scripts
from coordinator_core.ops.dispatch_emit.tests.test_ask_stage_resume_added_rows import (
    PLAN_REL,
    RUN,
    _plan,
    _row,
)
from coordinator_core.ops.dispatch_emit.tests.test_ask_stage_resume_added_rows import _stage as _stage_reply


def _stage_with_run_script(tmp_path, only_rows):
    """The run script the manifest reply's twin step needs, then the stage."""
    write_run_scripts(tmp_path, RUN)
    return _stage_reply(tmp_path, only_rows)

_HELD = (
    "- id: C4\n  title: tC4\n  change_kind: doc-edit\n"
    "  depends_on:\n    - chunk: C3\n      gate_kind: epistemic-premise\n"
)


def test_epistemic_held_row_is_reported_not_refused(tmp_path):
    _plan(tmp_path, _row("C3") + _HELD)
    resume_snapshot.write(tmp_path, RUN, PLAN_REL, [], ["C3", "C4"])
    reply = _stage_with_run_script(tmp_path, ["C3", "C4"])
    assert "error" not in reply
    assert {r["id"] for r in reply["rows"]} == {"C3"}
    assert reply["dropped_rows"] == [{"id": "C4", "reason": "held behind C3 (epistemic-premise)"}]


_VERIFY_C3 = (
    "- id: C3\n  title: tC3\n  change_kind: verification\n  surface: docs/x/C3.md\n  writes: []\n"
)
_DECLARED_C4 = _row("C4", "  depends_on:\n    - chunk: C3\n      gate_kind: epistemic-premise\n")
_REPORT_DIR = ".coordinator-local/subagent-share/dispatch-reports/p"


def _c3_report(tmp_path, text, name="C3.md", age=-60):
    """A premise report written `age` seconds from now: the run record is made at refire, so a negative
    age is older than the run and a positive one was written during it."""
    path = tmp_path / _REPORT_DIR / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    stamp = time.time() + age
    os.utime(path, (stamp, stamp))


def _refire(tmp_path, c4):
    _plan(tmp_path, _VERIFY_C3 + c4)
    resume_snapshot.write(tmp_path, RUN, PLAN_REL, [], ["C3", "C4"])
    return _stage_with_run_script(tmp_path, ["C3", "C4"])


def test_premise_that_ran_in_this_run_is_not_rerun_and_its_dependent_stages(tmp_path):
    _c3_report(tmp_path, "DONE: C3\n<exit-status>DONE</exit-status>\n", age=60)
    reply = _refire(tmp_path, _HELD)
    assert {r["id"] for r in reply["rows"]} == {"C4"}
    reasons = {d["id"]: d["reason"] for d in reply["dropped_rows"]}
    assert set(reasons) == {"C3"} and "already ran" in reasons["C3"]


def test_premise_report_older_than_the_run_still_holds(tmp_path):
    _c3_report(tmp_path, "DONE: C3\n<exit-status>DONE</exit-status>\n", age=-3600)
    reply = _refire(tmp_path, _HELD)
    assert reply["dropped_rows"] == [{"id": "C4", "reason": "held behind C3 (epistemic-premise)"}]
    assert {r["id"] for r in reply["rows"]} == {"C3"}


def test_prior_leg_report_set_aside_as_prior_still_counts(tmp_path):
    _c3_report(tmp_path, "DONE: C3\n", name="C3.prior.md", age=60)
    reply = _refire(tmp_path, _HELD)
    assert "C4" in {r["id"] for r in reply["rows"]}


def test_first_leg_premise_with_no_report_still_holds(tmp_path):
    reply = _refire(tmp_path, _HELD)
    assert reply["dropped_rows"] == [{"id": "C4", "reason": "held behind C3 (epistemic-premise)"}]


def test_premise_whose_prior_report_is_blocked_still_holds(tmp_path):
    _c3_report(tmp_path, "BLOCKED: C3\n<exit-status>BLOCKED</exit-status>\n", age=60)
    reply = _refire(tmp_path, _HELD)
    assert reply["dropped_rows"] == [{"id": "C4", "reason": "held behind C3 (epistemic-premise)"}]


def test_dependent_with_declared_writes_stages_after_the_premise(tmp_path):
    reply = _refire(tmp_path, _DECLARED_C4)
    rows = {r["id"]: r["wave"] for r in reply["rows"]}
    assert rows["C4"] > rows["C3"]
    assert not reply.get("dropped_rows")


def test_landed_predecessor_gives_no_held_behind_reason(tmp_path):
    _plan(tmp_path, _row("C3") + _row("C5") + _HELD)
    resume_snapshot.write(tmp_path, RUN, PLAN_REL, ["C3"], ["C4"], reasons={"C3": "landed"})
    reply = _stage_with_run_script(tmp_path, ["C4"])
    assert "error" not in reply
    assert "C4" in {r["id"] for r in reply["rows"]}
    assert all("held behind" not in d["reason"] for d in reply.get("dropped_rows", []))

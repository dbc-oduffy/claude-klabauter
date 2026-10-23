"""An XS-staged brief carries the sizing's em_analysis.em_brief, below the PM's words and outside them."""

from __future__ import annotations

import yaml

from coordinator_core.ops.dispatch_emit.ask_contract import PM_INTENT_FILE, RECORD_DIR_ROOT
from coordinator_core.ops.dispatch_emit.ask_stage import PM_INTENT_HEADING, stage
from coordinator_core.ops.dispatch_emit.tests.test_ask_stage import SIZING_REL, repo  # noqa: F401

_DIAG = "DIAG-MARK the bug is in pkg/a.py::f; change the guard to <="


def _sizing(repo, em_analysis):  # noqa: F811
    doc = {
        "intent": "EM restatement",
        "intent_source": "em-elaborated",
        "pm_verbatims": [{"turn": 0, "text": "fix f"}],
        "deliverable_id": "dlv-xs-abc123",
        "em_analysis": em_analysis,
    }
    (repo / SIZING_REL).write_text(yaml.safe_dump(doc), encoding="utf-8", newline="\n")


def test_xs_brief_carries_em_brief_below_pm_intent_and_not_in_it(repo):  # noqa: F811
    _sizing(repo, {"em_brief": _DIAG})
    m = stage(repo, run_id="eb1", sizing_rel=SIZING_REL, writes=["pkg/a.py"])
    brief = (repo / m.rows[0].brief_path).read_text(encoding="utf-8")
    assert _DIAG in brief
    assert brief.index(PM_INTENT_HEADING) < brief.index("DIAG-MARK")
    pm_intent = (repo / RECORD_DIR_ROOT / "eb1" / PM_INTENT_FILE).read_text(encoding="utf-8")
    assert "DIAG-MARK" not in pm_intent


def test_no_em_brief_leaves_the_brief_without_the_section(repo):  # noqa: F811
    _sizing(repo, {"em_brief": "   "})
    m = stage(repo, run_id="eb2", sizing_rel=SIZING_REL, writes=["pkg/a.py"])
    brief = (repo / m.rows[0].brief_path).read_text(encoding="utf-8")
    assert "EM diagnosis" not in brief

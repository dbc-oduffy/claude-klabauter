"""A register row `workflow.revise` added to a live run reaches every executor brief ask_stage stages."""

from __future__ import annotations

import subprocess

import pytest
import yaml

from coordinator_core.ops.dispatch_emit import requirements_ledger as rl
from coordinator_core.ops.dispatch_emit.ask_stage import REVISIONS_HEADING, stage
from coordinator_core.ops.dispatch_emit.tests.test_ask_stage import SIZING_REL, repo  # noqa: F401
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_REVISION = "R2-MARK also make the conform job refuse undeclared writes"


def _live_run(repo, run_id):  # noqa: F811
    doc = {
        "schema": "sizing-object",
        "name": "revise fixture",
        "intent": "Fix the thing",
        "estimate": {"tshirt": "XS", "provisional": True},
        "route": "spec-dispatch",
        "detents": [],
        "fork": None,
        "xl_exit": None,
        "status": "sized",
        "premise": {"provenance": "not-applicable", "evidence": "fixture"},
        "pm_verbatims": [{"turn": 0, "text": "fix the thing"}],
        "exit_criterion": {"statement": "it works", "accepted": None},
        "deliverable_id": "dlv-xs-abc123",
    }
    (repo / SIZING_REL).write_text(yaml.safe_dump(doc), encoding="utf-8", newline="\n")
    subprocess.run(["git", "init", "-q"], cwd=str(repo), check=True, capture_output=True, **no_console_creationflags())
    rl.start(repo, run_id)
    rl.bind(repo, run_id, SIZING_REL)


def test_a_revise_added_row_is_in_the_executor_brief(repo):  # noqa: F811
    _live_run(repo, "rv1")
    rl.revise(repo, "rv1", verbatim=_REVISION, sender="peer-session-1")
    m = stage(repo, run_id="rv1", sizing_rel=SIZING_REL, writes=["pkg/a.py"])
    brief = (repo / m.rows[0].brief_path).read_text(encoding="utf-8")
    assert REVISIONS_HEADING in brief
    assert f"> {_REVISION}" in brief
    assert "from peer-session-1" in brief


def test_a_run_with_no_revision_has_no_revisions_section(repo):  # noqa: F811
    _live_run(repo, "rv2")
    m = stage(repo, run_id="rv2", sizing_rel=SIZING_REL, writes=["pkg/a.py"])
    assert REVISIONS_HEADING not in (repo / m.rows[0].brief_path).read_text(encoding="utf-8")

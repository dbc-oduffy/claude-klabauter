"""Exit criterion end to end: a halted run's own withheld edit never contradicts its premise, and its criterion_not_met refire reaches the fix leg."""

from __future__ import annotations

import json

import pytest

from coordinator_core.ops import plan_readiness_verdict
from coordinator_core.ops.dispatch_emit import ask_gate, halted_run, kept_work
from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT
from coordinator_core.ops.dispatch_emit.tests.test_ask_gate import REL, _put, repo  # noqa: F401
from coordinator_core.ops.dispatch_emit.tests.test_ask_gate_resume import PLAN_REL, _plan

RUN = "ask-20261001T000000-aaaaaa"
NEW_RUN = "ask-20261002T000000-bbbbbb"
GATE = {"plan": PLAN_REL, "plan_sha256": "0" * 64, "reviews": [], "coverage": None}
EDITED = "src/x.py"
FAILING_NODE = "tests/test_x.py::test_edit"
READINESS_PLAN = "docs/plans/2026-10-01-readiness.md"

_SPINE = """- id: C1
  title: Ship it
  body: Do the named work and pin it with a test.
  change_kind: code-edit
  surface: coordinator_core/ops/plan_prep_gate.py
  writes: [coordinator_core/ops/plan_prep_gate.py]
  queue_scope: project
  disposition: open
"""


@pytest.fixture(autouse=True)
def _no_bind(monkeypatch):
    monkeypatch.setattr(ask_gate, "_bind_run", lambda *a, **k: None)


def _readiness_plan(repo):
    (repo / "coordinator_core").mkdir(exist_ok=True)
    (repo / READINESS_PLAN).write_text(
        "---\ntitle: fixture\nauthor: fixture\nstatus: draft\ncreated: 2026-10-01\n"
        "census:\n  - question: q\n    command: echo a\n    result: '1'\n"
        "capabilities: []\nanchor_premises: []\n"
        f"sizing_object: {REL}\n"
        f"prime_exit_criterion:\n  statement: the op reports per class\n  derived_from: {REL}\n"
        "---\n\n# Fixture\n\n## Tasks\n\n```yaml plan-tasks\n" + _SPINE + "```\n",
        encoding="utf-8",
    )


def _halt_criterion_not_met(repo):
    _put(repo, tshirt="S", route="spec-dispatch")
    _plan(repo, rows=[("C1", "coded"), ("C2", "open")])
    record = repo / RECORD_DIR_ROOT / RUN
    halted_run.write_halt_marker(
        record,
        halted="criterion_not_met",
        sizing_rel=REL,
        plan_rel=PLAN_REL,
        blocking=("test_blocker",),
        blocking_items=[FAILING_NODE],
    )
    edited = repo / EDITED
    edited.parent.mkdir(parents=True, exist_ok=True)
    edited.write_text("edited = 'by the halted run'\n", encoding="utf-8")
    kept_work.write(record, kept_work.kept_chunks(repo, {EDITED: "C2"}, {EDITED: "halt-record-only"}), GATE)
    index = repo / ".coordinator-local" / "halt-withheld"
    index.mkdir(parents=True, exist_ok=True)
    (index / f"{RUN}.json").write_text(json.dumps({"paths": [EDITED]}), encoding="utf-8")
    (index / ".built").write_bytes(b"")
    _readiness_plan(repo)
    (repo / ".git").mkdir(exist_ok=True)


def _readiness(repo, citation_path):
    row = {"verdict": "CONTRADICTED", "citation": f"{citation_path}:3 the plan cites base content"}
    return plan_readiness_verdict._handler({"plan": READINESS_PLAN, "premise": {"rows": [row]}}, repo / ".git")


def test_refire_regrades_the_run_own_edit_row_and_resumes_at_the_fix_leg(repo):  # noqa: F811
    _halt_criterion_not_met(repo)

    verdict = _readiness(repo, EDITED)
    assert verdict["verdict"] == "ready" and verdict["blocking_findings"] == 0

    phases = ask_gate._handler({"sizing_path": REL, "run_id": NEW_RUN}, repo_root=repo / ".git")["resume_phases"]
    assert {"plan", "plan_review"} <= set(phases["reuse"])
    assert phases["rerun"][phases["rerun"].index("execute"):] == ["execute", "review", "test", "criterion", "commit"]


def test_a_row_citing_a_path_the_run_did_not_withhold_still_pulls(repo):  # noqa: F811
    _halt_criterion_not_met(repo)
    verdict = _readiness(repo, "src/untouched.py")
    assert verdict["verdict"] == "judgment" and verdict["blocking_findings"] == 1

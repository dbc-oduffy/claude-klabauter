"""A refire of a pulled-plan halt resumes from that plan: the sizing's `plan:` edge, every row open, no checkpoint commit."""

from __future__ import annotations

import importlib.machinery
import importlib.util
import subprocess
from pathlib import Path

import pytest
import yaml

from coordinator_core.ops.dispatch_emit.ask_gate import gate
from coordinator_core.ops.dispatch_emit.sizing_fire import resumable_plan
from coordinator_core.ops.dispatch_emit.tests.test_ask_gate import ACCEPTED, REL, _put, repo  # noqa: F401
from coordinator_core.win_portability import no_console_creationflags

PLAN_REL = "docs/plans/2026-10-22-pulled.md"
ROW_IDS = ["C1", "C2", "C3"]


pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


def _git_init(repo):  # noqa: F811
    subprocess.run(["git", "init", "-q", str(repo)], capture_output=True, check=True, **no_console_creationflags())


def _pulled_plan(repo):  # noqa: F811
    """A plan every row of which is open and which no checkpoint commit names: a never-staged pulled plan."""
    path = repo / PLAN_REL
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "".join(
        f"- id: {i}\n  title: T{i}\n  disposition: open\n  writes:\n    - some/{i}.py\n" for i in ROW_IDS
    )
    path.write_text(
        f"---\nrun_id: fixture\n---\n\n## Tasks\n\n```yaml plan-tasks\n{body}```\n", encoding="utf-8", newline="\n"
    )


def test_refire_of_a_pulled_plan_halt_resumes_at_m_plus_from_the_plan_edge(repo):  # noqa: F811
    _git_init(repo)
    _put(repo, tshirt="L", route="plan", accepted=ACCEPTED, plan=PLAN_REL)
    _pulled_plan(repo)
    v = gate(repo, REL)
    assert v.halt is None and v.arm == "m_plus"
    assert v.resume_plan == PLAN_REL
    assert v.resume_rows == ROW_IDS
    assert v.spine_plan is None
    assert v.to_json()["resume_plan"] == PLAN_REL


def test_resumable_plan_keeps_a_never_staged_plan_at_m_plus(repo):  # noqa: F811
    _git_init(repo)
    _pulled_plan(repo)
    plan = resumable_plan({"plan": PLAN_REL}, REL, repo, "m_plus")
    assert plan is not None and plan.plan_rel == PLAN_REL and list(plan.incomplete) == ROW_IDS


def test_a_sizing_with_no_plan_edge_does_not_resume_at_m_plus(repo):  # noqa: F811
    _git_init(repo)
    _put(repo, tshirt="L", route="plan", accepted=ACCEPTED)
    _pulled_plan(repo)
    v = gate(repo, REL)
    assert v.resume_plan is None


def test_plan_scaffolded_with_a_sizing_object_writes_the_plan_edge():
    cli = Path(__file__).resolve().parents[4] / "coordinator" / "bin" / "coordinator-doc-new.py"
    loader = importlib.machinery.SourceFileLoader("coordinator_doc_new_pulled_edge_test", str(cli))
    mod = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
    loader.exec_module(mod)
    old = (
        "schema: sizing-object\nintent: example\nname: example\nestimate:\n  tshirt: L\n  provisional: true\n"
        "route: plan\ndetents: []\nfork: null\nxl_exit: null\npremise:\n  provenance: not-applicable\n"
        "  evidence: example\nstatus: sized\nplan: null\n"
    )
    new = mod._mutate_sizing_reverse_edge(old, PLAN_REL)
    assert yaml.safe_load(new)["plan"] == PLAN_REL

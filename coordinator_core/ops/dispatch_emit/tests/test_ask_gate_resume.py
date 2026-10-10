"""ask_gate resume: an unfired plan on disk rides the verdict as resume_plan; a coded row refuses naming --plan."""

from __future__ import annotations

import subprocess
import textwrap

import pytest

from coordinator_core.ops.dispatch_emit.ask_gate import gate
from coordinator_core.ops.dispatch_emit.sizing_fire import SizingFireRefused, resumable_plan
from coordinator_core.ops.dispatch_emit.tests.test_ask_gate import ACCEPTED, REL, _put, repo  # noqa: F401
from coordinator_core.win_portability import no_console_creationflags

PLAN_REL = "docs/plans/2026-10-01-gate.md"


def _plan(repo, disposition="open", rel=PLAN_REL):
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        textwrap.dedent(
            f"""\
            ---
            run_id: fixture
            ---

            ## Tasks

            ```yaml plan-tasks
            - id: A1
              title: One
              disposition: {disposition}
              writes:
                - some/one.py
            ```
            """
        ),
        encoding="utf-8",
        newline="\n",
    )


def test_s_with_unfired_plan_gates_with_resume_plan_and_no_refusal(repo):  # noqa: F811
    _put(repo, tshirt="S", route="spec-dispatch")
    _plan(repo)
    v = gate(repo, REL)
    assert v.halt is None and v.arm == "s" and v.resume_plan == PLAN_REL
    assert v.to_json()["resume_plan"] == PLAN_REL


def test_s_with_coded_row_refuses_naming_plan_flag(repo):  # noqa: F811
    _put(repo, tshirt="S", route="spec-dispatch")
    _plan(repo, disposition="coded")
    v = gate(repo, REL)
    assert v.arm is None and v.halt["kind"] == "refusal"
    assert f"--plan {PLAN_REL}" in v.halt["reason"]


def test_s_without_plan_has_no_resume_plan_key(repo):  # noqa: F811
    _put(repo, tshirt="S", route="spec-dispatch")
    v = gate(repo, REL)
    assert v.halt is None and "resume_plan" not in v.to_json()


def test_resumable_plan_none_without_a_plan(repo):  # noqa: F811
    assert resumable_plan({}, REL, repo, "s") is None
    assert resumable_plan({}, REL, repo, "m_plus") is None


def test_resumable_plan_follows_the_plan_edge_at_m_plus(repo):  # noqa: F811
    _plan(repo, rel="docs/plans/other.md")
    assert resumable_plan({"plan": "docs/plans/other.md"}, REL, repo, "m_plus") == "docs/plans/other.md"


def test_resumable_plan_refuses_an_edge_escaping_plans(repo):  # noqa: F811
    (repo / "elsewhere.md").write_text("x", encoding="utf-8")
    assert resumable_plan({"plan": "elsewhere.md"}, REL, repo, "m_plus") is None


def test_resumable_plan_coded_raises(repo):  # noqa: F811
    _plan(repo, disposition="coded")
    with pytest.raises(SizingFireRefused, match="--plan"):
        resumable_plan({}, REL, repo, "s")


@pytest.mark.cadence
@pytest.mark.spawns_process
def test_m_with_plan_edge_returns_resume_plan_and_baton(repo):  # noqa: F811
    subprocess.run(["git", "init", "-q", str(repo)], capture_output=True, **no_console_creationflags())
    _put(repo, tshirt="M", route="plan", accepted=ACCEPTED, plan=PLAN_REL)
    _plan(repo)
    v = gate(repo, REL)
    assert v.halt is None and v.arm == "m_plus"
    assert v.resume_plan == PLAN_REL and v.baton["path"].startswith("state/handoffs/")

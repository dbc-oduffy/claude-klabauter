"""The efdb75 incident shape: an M/plan sizing whose `ask-*` run halted at the ledger check re-fires
through `dispatch.ask_gate` with a new run id and resumes at the ledger check, not at planning."""

from __future__ import annotations

import re
import subprocess

import pytest

from coordinator_core.git.git_dir import resolve_git_common_dir
from coordinator_core.ops.dispatch_emit import ask_compose, ask_gate
from coordinator_core.ops.dispatch_emit.ask_compose import compose_ask_script
from coordinator_core.ops.dispatch_emit.ask_contract import LedgerHalt, ledger_halt_path, write_ledger_halt
from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW
from coordinator_core.ops.dispatch_emit.tests.test_ask_gate import ACCEPTED, REL, _put, repo  # noqa: F401
from coordinator_core.ops.dispatch_emit.tests.test_ask_gate_resume import PLAN_REL, _plan
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

HALTED_RUN = "ask-20261016T200022-dc28d4"
REFIRE_RUN = "warp-2026-10-01-gate-20261016T202611-efdb75"
RULING = {"source": "pm", "quote": "out of scope", "on": "2026-10-16", "ref": f"workflow.revise:{HALTED_RUN}:out_of_scope"}
REGISTER = {
    "sources": [{"doc": "ask", "ref": "ask"}],
    "rows": [{"id": "R1", "source_text": "the fix", "surface": "internal", "status": "deferred", "ruling": RULING}],
}
_BLITZ_FN = "  async function planBlitz(args) {\n    return { ready: [] };\n  }"


def _git(repo, *a):
    subprocess.run(["git", *a], cwd=str(repo), check=True, capture_output=True, **no_console_creationflags())


def _incident_repo(repo):
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t.example")
    _git(repo, "config", "user.name", "t")
    _plan(repo, rows=[("C0", "open"), ("C1", "open")])
    _put(repo, tshirt="M", route="plan", accepted=ACCEPTED, plan=PLAN_REL, requirement_register=REGISTER)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    for suffix in ("", " (process files)"):
        (repo / f"h{len(suffix)}.txt").write_text("x\n", encoding="utf-8")
        _git(repo, "add", "-A")
        trailers = "Halted-By: ledger-check\nLands-Chunks: C0, C1\n" if suffix else "Halted-By: ledger-check\n"
        _git(repo, "commit", "-q", "-m", f"warp: record halted {HALTED_RUN} run artifacts{suffix}\n\n{trailers}")
    write_ledger_halt(
        ledger_halt_path(resolve_git_common_dir(repo), PLAN_REL),
        LedgerHalt(HALTED_RUN, PLAN_REL, REL, ("R1",)),
    )


def test_ask_gate_with_a_new_run_id_resumes_at_the_ledger(repo):  # noqa: F811
    _incident_repo(repo)
    reply = ask_gate._handler({"sizing_path": REL, "run_id": REFIRE_RUN}, repo)
    assert reply.get("halt") is None, reply
    assert reply.get("resume_ledger") is not None, reply
    assert reply.get("resume_plan") is None, reply


def test_emit_time_gate_agrees_with_ask_gate_on_the_incident_shape(repo):  # noqa: F811
    _incident_repo(repo)
    verdict = ask_gate.gate(repo, REL, writes=["<footprint authored at run time>"], baton=None)
    assert verdict.halt is None, verdict
    assert verdict.resume_ledger is not None, verdict
    assert verdict.resume_plan is None, verdict


def test_regate_after_an_in_run_ruling_keeps_resume_ledger(monkeypatch):
    monkeypatch.setattr(ask_compose, "_known_arm", lambda *_: "s")
    script = compose_ask_script(
        repo_root="REPO", prompt=None, sizing_rel="state/sizings/x.yaml", run_id="run-1", session_id=None,
        wrap_stage=lambda _t: (_BLITZ_FN, ["Size", "Plan"]), plan_blitz_text="stub", script_path="x.mjs",
        in_run_commit=True, accept_pending=True, **REVIEW_KW,
    )
    # Commit 3e9a83f96c added `_gate = { ..._gate, arm: _gate.arm.trim().toLowerCase() }`, which
    # normalises the arm and takes no reply; a take reads the arm from a reply other than `_gate`.
    every_arm_spread = re.findall(r"_gate = \{ \.\.\._gate, arm: _\w+\.arm[^;]*;", script)
    takes = re.findall(r"_gate = \{ \.\.\._gate, arm: _(?!gate\.)\w+\.arm[^;]*;", script)
    normalisations = [m for m in every_arm_spread if "_gate.arm.trim().toLowerCase()" in m]
    assert normalisations
    assert takes == [m for m in every_arm_spread if m not in normalisations]
    assert takes
    for take in takes:
        assert "resume_ledger" in take, take

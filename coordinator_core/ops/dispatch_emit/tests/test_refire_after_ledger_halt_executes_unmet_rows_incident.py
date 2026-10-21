"""The 21fa0afd76 incident shape: a refire of a ledger-check halt carries the owed work and the revise
note into the resume, composes a remaining-work leg, and land_run refuses `met` on a tree
no declared product path changed in since the halt."""

from __future__ import annotations

import json
import subprocess

import pytest

from coordinator_core.ops.dispatch_emit import ask_compose, ask_gate
from coordinator_core.ops.dispatch_emit import requirements_ledger as rl
from coordinator_core.ops.dispatch_emit import land_run
from coordinator_core.ops.dispatch_emit.ask_compose import compose_ask_script
from coordinator_core.ops.dispatch_emit.ask_contract import (
    RECORD_DIR_ROOT,
    RESUME_LEDGER_HALT_SHA_FIELD,
    RESUME_LEDGER_NOTES_FIELD,
    RESUME_LEDGER_REMAINING_FIELD,
    RESUME_LEDGER_UNLANDED_FIELD,
    TERMINAL_RESUME_LEDGER_PARAM,
    UNCHANGED_SINCE_HALT_REASON,
    halted_record_message,
)
from coordinator_core.ops.dispatch_emit.halted_run import write_halt_marker
from coordinator_core.ops.dispatch_emit.reply_sidecar import read_reply
from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW
from coordinator_core.ops.dispatch_emit.tests.test_ask_gate import ACCEPTED, REL, _put, repo  # noqa: F401
from coordinator_core.ops.dispatch_emit.tests.test_ask_gate_resume import PLAN_REL, _plan
from coordinator_core.ops.dispatch_emit.tests.test_refire_after_ledger_halt_resumes_incident import (
    _BLITZ_FN,
    _git,
)
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

HALTED_RUN = "ask-20261017T014048-0c9cac"
REFIRE_RUN = "warp-2026-10-17-refire-20261017T020238-a32fd8"
NOTE = "R1 still owes the unchanged-since-halt refusal in land_run"
REMAINING = {"R1": {"remaining": ["add the unchanged-since-halt refusal"], "evidence": "falsifier still fails"}}
REGISTER = {
    "sources": [{"doc": "ask", "ref": "ask"}],
    "rows": [{"id": "R1", "source_text": "refuse met on an unchanged diff", "surface": "internal", "status": "open"}],
}
MET = {"items": [{"id": "R1", "status": "met", "wired": True}]}


def _gate(repo, run_dir, verdict, manifest, resume_ledger):
    return land_run._ledger_gate(repo, run_dir, verdict, manifest, (), {}, None, {}, resume_ledger, None)


def _head(repo):
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(repo), check=True, capture_output=True, text=True,
        **no_console_creationflags(),
    ).stdout.strip()


def _halted_repo(repo):
    """A landed plan whose ledger-check halt commit recorded R1's remaining work, plus one revise note."""
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t.example")
    _git(repo, "config", "user.name", "t")
    _plan(repo, rows=[("C0", "open"), ("C1", "open")])
    _put(repo, tshirt="M", route="plan", accepted=ACCEPTED, plan=PLAN_REL, requirement_register=REGISTER)
    for rel in ("some/C0.py", "some/C1.py"):
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text("x = 1\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    rl.start(repo, HALTED_RUN)
    rl.revise(repo, HALTED_RUN, note=NOTE)
    record = repo / RECORD_DIR_ROOT / HALTED_RUN
    write_halt_marker(
        record, halted="ledger-check", sizing_rel=REL, plan_rel=PLAN_REL, blocking=("R1",),
        blocking_items=[{"id": "R1", **REMAINING["R1"]}],
    )
    (record / "manifest.json").write_text(
        json.dumps({"rows": [{"id": "C0", "writes": ["some/C0.py"]}, {"id": "C1", "writes": ["some/C1.py"]}]}),
        encoding="utf-8",
    )
    _git(repo, "add", "-A")
    msg = halted_record_message(HALTED_RUN, {"halted": "ledger-check", "plan": PLAN_REL})
    _git(repo, "commit", "-q", "-m", msg + "\nLands-Chunks: C0, C1\n")
    return _head(repo)


def _script(monkeypatch) -> str:
    monkeypatch.setattr(ask_compose, "_known_arm", lambda *_: "s")
    return compose_ask_script(
        repo_root="REPO", prompt=None, sizing_rel="state/sizings/x.yaml", run_id="run-1", session_id=None,
        wrap_stage=lambda _t: (_BLITZ_FN, ["Size", "Plan"]), plan_blitz_text="stub", script_path="x.mjs",
        in_run_commit=True, **REVIEW_KW,
    )


def test_the_refire_gate_carries_the_halt_sha_the_owed_work_and_the_revise_note(repo):  # noqa: F811
    halt_sha = _halted_repo(repo)
    reply = ask_gate._handler({"sizing_path": REL, "run_id": REFIRE_RUN}, repo)
    assert reply.get("halt") is None, reply
    resume = reply["resume_ledger"]
    assert resume["run_id"] == HALTED_RUN and resume["blocking"] == ["R1"]
    assert resume[RESUME_LEDGER_HALT_SHA_FIELD] == halt_sha
    assert resume[RESUME_LEDGER_REMAINING_FIELD] == REMAINING
    assert resume[RESUME_LEDGER_NOTES_FIELD] == [NOTE]
    assert reply.get("resume_plan") is None and reply.get("resume_rows") is None


def test_the_composed_resume_script_runs_a_remaining_work_leg_before_the_verify(monkeypatch):
    script = _script(monkeypatch)
    leg = script.index("label: 'ledger-remaining'")
    assert script.index("if (_resumeLedger && !_ledgerExec && ((_gate.resume_ledger.blocking ?? []).length || _reviseNoted)) {") < leg
    assert leg < script.index("label: 'verify'")
    prompt = script[leg - 2200 : leg]
    assert "_gate.resume_ledger.remaining ?? {}" in prompt and "_gate.resume_ledger.revise_notes ?? []" in prompt
    assert "agentType: 'coordinator:executor'" in script[leg:][:200]
    assert f"_nextAction.params.{TERMINAL_RESUME_LEDGER_PARAM} = " in script


def test_land_run_refuses_met_on_an_unchanged_tree_and_passes_after_a_declared_edit(repo):  # noqa: F811
    halt_sha = _halted_repo(repo)
    resume = ask_gate._handler({"sizing_path": REL, "run_id": REFIRE_RUN}, repo)["resume_ledger"]
    rl.start(repo, REFIRE_RUN)
    rl.bind(repo, REFIRE_RUN, REL)
    run_dir = repo / RECORD_DIR_ROOT / REFIRE_RUN
    manifest = repo / RECORD_DIR_ROOT / HALTED_RUN / "manifest.json"
    param = {"run_id": REFIRE_RUN, "halt_sha": resume[RESUME_LEDGER_HALT_SHA_FIELD], "blocking": resume["blocking"]}
    assert param["halt_sha"] == halt_sha

    refusal = _gate(repo, run_dir, MET, manifest, param)[0]
    assert refusal["refused"] == "ledger-unmet"
    assert refusal["ledger_failures"] == [{"id": "R1", "reason": UNCHANGED_SINCE_HALT_REASON}]

    (repo / "state" / "extra.yaml").write_text("a: 1\n", encoding="utf-8")
    assert _gate(repo, run_dir, MET, manifest, param)[0]["refused"] == "ledger-unmet"

    (repo / "some" / "C0.py").write_text("x = 2\n", encoding="utf-8")
    after = _gate(repo, run_dir, MET, manifest, param)[0]
    assert after is None or UNCHANGED_SINCE_HALT_REASON not in json.dumps(after)
    assert after is None or after["refused"] == "ledger-unmet"


def _unlanded_halted_repo(repo):
    """The 6ffef8 shape: S / spec-dispatch, plan row C1 open, a ledger-check halt commit with no
    `Lands-Chunks:` trailer, so the staged C1 never landed."""
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t.example")
    _git(repo, "config", "user.name", "t")
    _plan(repo, rows=[("C1", "open")])
    _put(repo, tshirt="S", route="spec-dispatch", accepted=ACCEPTED, plan=PLAN_REL, requirement_register=REGISTER)
    (repo / "some").mkdir(parents=True, exist_ok=True)
    (repo / "some" / "C1.py").write_text("x = 1\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    rl.start(repo, HALTED_RUN)
    record = repo / RECORD_DIR_ROOT / HALTED_RUN
    write_halt_marker(
        record, halted="ledger-check", sizing_rel=REL, plan_rel=PLAN_REL, blocking=("R1",),
        blocking_items=[{"id": "R1", **REMAINING["R1"]}],
    )
    (record / "manifest.json").write_text(
        json.dumps({"rows": [{"id": "C1", "writes": ["some/C1.py"]}]}), encoding="utf-8"
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", halted_record_message(HALTED_RUN, {"halted": "ledger-check", "plan": PLAN_REL}))
    return _head(repo)


def test_unlanded_gate_names_the_never_landed_row_and_stages_only_it(repo):  # noqa: F811
    _unlanded_halted_repo(repo)
    reply = ask_gate._handler({"sizing_path": REL, "run_id": REFIRE_RUN}, repo)
    assert reply.get("halt") is None, reply
    resume = reply["resume_ledger"]
    assert resume["run_id"] == HALTED_RUN and resume["blocking"] == ["R1"]
    assert resume[RESUME_LEDGER_UNLANDED_FIELD] == ["C1"]
    assert [r["id"] for r in read_reply(repo, reply["manifest_handle"])["rows"]] == ["C1"]


def test_unlanded_composed_script_sets_ledger_exec_ahead_of_the_execute_phase(repo, monkeypatch):  # noqa: F811
    _unlanded_halted_repo(repo)
    reply = ask_gate._handler({"sizing_path": REL, "run_id": REFIRE_RUN}, repo)
    assert reply["arm"] == "s", reply
    script = _script(monkeypatch)
    guard = script.index("if (!_halted && (!_resumeLedger || _ledgerExec)) {")
    field = script.index(f"resume_ledger.{RESUME_LEDGER_UNLANDED_FIELD}")
    assert field < guard
    assert "_ledgerExec = true" in script[field:guard]
    assert "_ledgerUnlanded.length" in script[:guard]
    # The remaining-work leg is the fallback for a resume with nothing unlanded, never the unlanded path.
    leg = script.index("label: 'ledger-remaining'")
    gate_if = script.index("if (_resumeLedger && !_ledgerExec && ((_gate.resume_ledger.blocking ?? []).length || _reviseNoted)) {")
    assert gate_if < leg and script.index("_ledgerExec = true", field) < gate_if


def test_unlanded_land_run_still_refuses_met_on_an_unchanged_tree(repo):  # noqa: F811
    halt_sha = _unlanded_halted_repo(repo)
    resume = ask_gate._handler({"sizing_path": REL, "run_id": REFIRE_RUN}, repo)["resume_ledger"]
    rl.start(repo, REFIRE_RUN)
    rl.bind(repo, REFIRE_RUN, REL)
    run_dir = repo / RECORD_DIR_ROOT / REFIRE_RUN
    manifest = repo / RECORD_DIR_ROOT / HALTED_RUN / "manifest.json"
    param = {"run_id": REFIRE_RUN, "halt_sha": resume[RESUME_LEDGER_HALT_SHA_FIELD], "blocking": resume["blocking"]}
    assert param["halt_sha"] == halt_sha
    refusal = _gate(repo, run_dir, MET, manifest, param)[0]
    assert refusal["refused"] == "ledger-unmet"
    assert refusal["ledger_failures"] == [{"id": "R1", "reason": UNCHANGED_SINCE_HALT_REASON}]

"""Both 2026-10-22 incident refires, rebuilt under tmp_path from the committed run records
(50e55e at 6f1db95dce, b6d61d at 836f8c8a79): a refire of a ledger-check-halted XS or S run keeps the
halted run's arm, stages one dispatchable row serving R1 whose writes cover the owed file, and the
composed resume script holds no `_gate.arm` reassignment."""

from __future__ import annotations

import json
import re

import pytest

from coordinator_core.ops.dispatch_emit import ask_compose, ask_gate
from coordinator_core.ops.dispatch_emit import requirements_ledger as rl
from coordinator_core.ops.dispatch_emit.ask_compose import compose_ask_script
from coordinator_core.ops.dispatch_emit.ask_contract import (
    LEG_DISPATCH,
    RECORD_DIR_ROOT,
    RESUME_LEDGER_ARM_FIELD,
    RESUME_LEDGER_DERIVED_FIELD,
    RESUME_LEDGER_LEG_FIELD,
    halted_record_message,
)
from coordinator_core.ops.dispatch_emit.halted_run import write_halt_marker
from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW
from coordinator_core.ops.dispatch_emit.tests.test_ask_gate import ACCEPTED, REL, _put, repo  # noqa: F401
from coordinator_core.ops.dispatch_emit.tests.test_ask_gate_resume import PLAN_REL
from coordinator_core.ops.dispatch_emit.tests.test_refire_after_ledger_halt_resumes_incident import _git

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

REFIRE_RUN = "warp-2026-10-22-refire-20261022T172513-841c99"
REGISTER = {
    "sources": [{"doc": "ask", "ref": "ask"}],
    "rows": [{"id": "R1", "source_text": "the owed fix", "surface": "internal", "status": "open"}],
}
_BLITZ_FN = "  async function planBlitz(args) {\n    return { ready: [] };\n  }"

XS_RUN = "ask-20261022T165956-50e55e"
XS_SPINE = f"{RECORD_DIR_ROOT}/{XS_RUN}/gate.spine.md"
XS_OWED = "coordinator_core/machine_profile.py"
XS_REMAINING = [
    "apply_guard_level must return a Stop-valid warn shape for Stop/SubagentStop "
    "(coordinator_core/machine_profile.py::apply_guard_level)"
]
XS_NOTE = "R1 still owes the Stop-valid warn shape from apply_guard_level"

S_RUN = "ask-20261022T165956-b6d61d"
S_OWED = "coordinator_core/bash_guards/_dc_rewrites.py"
S_SERVING = "coordinator_core/bash_guards/guard_grep_via_bash.py"
S_REMAINING = ["compile gate on _dc_rewrites.check_grep_via_bash_rewrite is still owed"]
S_NOTE = "R1 still owes the compile gate on _dc_rewrites.check_grep_via_bash_rewrite"


def _spine(rel_path, rows):
    """`rows` is `[(id, disposition, writes, register_rows)]`; written as a plan-tasks fence."""
    body = "".join(
        f"- id: {i}\n  title: T{i}\n  disposition: {d}\n  writes:\n"
        + "".join(f"    - {w}\n" for w in ws)
        + (f"  register_rows: [{', '.join(rr)}]\n" if rr else "")
        for i, d, ws, rr in rows
    )
    rel_path.parent.mkdir(parents=True, exist_ok=True)
    rel_path.write_text(
        f"---\nrun_id: fixture\n---\n\n## Tasks\n\n```yaml plan-tasks\n{body}```\n", encoding="utf-8", newline="\n"
    )


def _halt(repo, run, plan_rel, remaining, note, landed):
    """A committed ledger-check halt on R1 whose remaining list and revise note are recorded."""
    rl.start(repo, run)
    record = repo / RECORD_DIR_ROOT / run
    write_halt_marker(
        record, halted="ledger-check", sizing_rel=REL, plan_rel=plan_rel, blocking=("R1",),
        blocking_items=[{"id": "R1", "remaining": remaining, "evidence": "ledger verdict"}],
    )
    (record / "manifest.json").write_text(json.dumps({"rows": [{"id": i} for i in landed]}), encoding="utf-8")
    (record / rl.NOTES_FILE).write_text(json.dumps({"note": note, "at": "2026-10-22T17:00:00Z"}) + "\n", encoding="utf-8")
    _git(repo, "add", "-A")
    msg = halted_record_message(run, {"halted": "ledger-check", "plan": plan_rel})
    _git(repo, "commit", "-q", "-m", msg + (f"\nLands-Chunks: {', '.join(landed)}\n" if landed else ""))


def _git_init(repo):
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t.example")
    _git(repo, "config", "user.name", "t")


def _xs_replay_repo(repo):
    """X1 consumed, X2 withheld, ledger-halted on R1 with no plan edge on the sizing."""
    _put(repo, tshirt="XS", route="dispatch", requirement_register=REGISTER)
    _git_init(repo)
    (repo / "coordinator_core").mkdir(parents=True, exist_ok=True)
    (repo / XS_OWED).write_text("def apply_guard_level():\n    return None\n", encoding="utf-8")
    _spine(repo / XS_SPINE, [("X1", "open", [XS_OWED], ["R1"]), ("X2", "voided", ["some/X2.py"], [])])
    (repo / "x.txt").write_text("x\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "checkpoint(wave 1): 1 rows — X1", "-m", f"Checkpoint-Plan: {XS_SPINE}\nCheckpoint-Base: abc1234")
    _halt(repo, XS_RUN, XS_SPINE, XS_REMAINING, XS_NOTE, [])


def _s_replay_repo(repo):
    """C1 and C2 landed, ledger-halted on R1 owing a compile gate in a module beside C2's write."""
    _put(repo, tshirt="S", route="spec-dispatch", accepted=ACCEPTED, plan=PLAN_REL, requirement_register=REGISTER)
    _git_init(repo)
    (repo / "coordinator_core" / "bash_guards").mkdir(parents=True, exist_ok=True)
    (repo / S_OWED).write_text("def check_grep_via_bash_rewrite():\n    return None\n", encoding="utf-8")
    (repo / S_SERVING).write_text("x = 1\n", encoding="utf-8")
    _spine(repo / PLAN_REL, [("C1", "open", ["some/C1.py"], []), ("C2", "open", [S_SERVING], ["R1"])])
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    _halt(repo, S_RUN, PLAN_REL, S_REMAINING, S_NOTE, ["C1", "C2"])


def _compose(monkeypatch, arm):
    monkeypatch.setattr(ask_compose, "_known_arm", lambda *_: arm)
    return compose_ask_script(
        repo_root="REPO", prompt=None, sizing_rel=REL, run_id="run-1", session_id=None,
        wrap_stage=lambda _t: (_BLITZ_FN, ["Size", "Plan"]), plan_blitz_text="stub",
        script_path="x.mjs", in_run_commit=True, **REVIEW_KW,
    )


def _staged_rows(repo):
    return json.loads((repo / RECORD_DIR_ROOT / REFIRE_RUN / "manifest.json").read_text(encoding="utf-8"))["rows"]


def _assert_script_keeps_arm(script):
    block = script[script.index("const _resumeLedger = !_halted"):]
    resume = block[: block.index("label: 'verify'")]
    assert not re.search(r"_gate\s*=\s*\{\s*\.\.\._gate,\s*arm\s*:", resume), "ledger resume reassigns _gate.arm"
    assert "_ledgerDerived.length) { _ledgerExec = true; }" in block


def _assert_row(repo, out, arm, owed):
    resume = out["resume_ledger"]
    assert out["arm"] == arm and resume[RESUME_LEDGER_ARM_FIELD] == arm, out
    derived = resume[RESUME_LEDGER_DERIVED_FIELD]
    assert len(derived) == 1, resume
    staged = [r for r in _staged_rows(repo) if r["id"] == derived[0]]
    assert len(staged) == 1, _staged_rows(repo)
    assert staged[0]["register_rows"] == ["R1"] and owed in staged[0]["writes"], staged
    leg = resume[RESUME_LEDGER_LEG_FIELD]
    assert leg["action"] == LEG_DISPATCH and owed in leg["writes"], leg


def test_50e55e_xs_refire_stages_a_row_for_r1_and_keeps_arm_xs(repo, monkeypatch):  # noqa: F811
    _xs_replay_repo(repo)

    out = ask_gate._handler({"sizing_path": REL, "run_id": REFIRE_RUN, "writes": [XS_OWED]}, repo)

    assert out.get("halt") is None and "error" not in (out.get("manifest") or {}), out
    _assert_row(repo, out, "xs", XS_OWED)
    assert XS_NOTE in out["resume_ledger"]["revise_notes"]
    _assert_script_keeps_arm(_compose(monkeypatch, "xs"))


def test_b6d61d_s_refire_stages_a_row_for_r1_and_keeps_arm_s(repo, monkeypatch):  # noqa: F811
    _s_replay_repo(repo)

    out = ask_gate._handler({"sizing_path": REL, "run_id": REFIRE_RUN}, repo)

    assert out.get("halt") is None and "error" not in (out.get("manifest") or {}), out
    _assert_row(repo, out, "s", S_OWED)
    assert S_NOTE in out["resume_ledger"]["revise_notes"]
    assert S_SERVING in _staged_rows(repo)[-1]["writes"] or any(S_SERVING in r["writes"] for r in _staged_rows(repo))
    _assert_script_keeps_arm(_compose(monkeypatch, "s"))

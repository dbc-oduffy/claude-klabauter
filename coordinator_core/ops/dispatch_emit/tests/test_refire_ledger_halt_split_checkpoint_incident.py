"""Incident shape of a ledger-check halt refire across a split checkpoint pair: coded rows stamped by
either half count as checkpoint-landed, and a later refusal halt rewriting `halted.json` does not hide
the ledger-check record, so the gate resumes at the ledger instead of re-running plan-blitz."""

from __future__ import annotations

import json
import subprocess

import pytest

from coordinator_core.ops.dispatch_emit import ask_gate, sizing_fire
from coordinator_core.ops.dispatch_emit.ask_contract import (
    RECORD_DIR_ROOT,
    halted_record_message,
    ledger_halt_path,
)
from coordinator_core.ops.dispatch_emit.halted_run import write_halt_marker
from coordinator_core.ops.dispatch_emit.tests.test_ask_gate import ACCEPTED, REL, _put, repo  # noqa: F401
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


def _git(repo, *a):
    return subprocess.run(
        ["git", *a], cwd=str(repo), check=True, capture_output=True, text=True, **no_console_creationflags()
    ).stdout.strip()


PLAN_REL = "docs/plans/2026-10-21-i102.md"
RUN = "ask-20261021T133759-cd90b8"
REMAINING = ["stale assertion one", "stale assertion two", "typecheck error", "operator and EM rows"]
R1_ITEM = {"id": "R1", "remaining": REMAINING, "evidence": "gaps left"}
REGISTER = {
    "sources": [{"doc": "ask", "ref": "ask"}],
    "rows": [{"id": "R1", "source_text": "row R1", "surface": "internal", "status": "open"}],
}
CODED = [f"C{n}" for n in range(1, 9)]
OWED = ["C9", "C10", "C11"]


def _commit(repo, name, subject, *trailers):
    (repo / name).write_text(subject + "\n", encoding="utf-8")
    _git(repo, "add", name)
    _git(repo, "commit", "-q", "-m", subject + ("\n\n" + "\n".join(trailers) if trailers else ""))
    return _git(repo, "rev-parse", "HEAD")


def _write_plan(repo, stamps):
    body = ""
    for rid in CODED:
        body += f"- id: {rid}\n  title: T{rid}\n  disposition: coded\n  disposition_ref: {stamps[rid]}\n"
        body += f"  writes:\n    - some/{rid}.py\n"
    for rid in ("C9", "C10"):
        body += f"- id: {rid}\n  title: T{rid}\n  disposition: open\n  execution_mode: operator\n"
        body += f"  writes:\n    - some/{rid}.py\n"
    body += "- id: C11\n  title: TC11\n  disposition: open\n  performer: em\n  writes:\n    - some/C11.py\n"
    path = repo / PLAN_REL
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\nrun_id: fixture\n---\n\n## Tasks\n\n```yaml plan-tasks\n{body}```\n", encoding="utf-8", newline="\n"
    )


def _incident(repo, *, split=True, refusal=True):
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t.example")
    _git(repo, "config", "user.name", "t")
    _put(repo, tshirt="M", route="plan", accepted=ACCEPTED, plan=PLAN_REL, requirement_register=REGISTER)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    trailer = f"Checkpoint-Plan: {PLAN_REL}"
    wave0 = "checkpoint(wave 0): 4 rows — C1, C2, C3, C8"
    content = _commit(repo, "w0.txt", wave0, trailer)
    process = _commit(repo, "w0p.txt", wave0 + " (process files)", trailer) if split else content
    stamps = {"C1": content, "C3": content, "C8": content, "C2": process}
    for rid, wave in (("C4", 1), ("C5", 1), ("C6", 2), ("C7", 2)):
        stamps[rid] = _commit(repo, f"w{wave}{rid}.txt", f"checkpoint(wave {wave}): 1 rows — {rid}", trailer)
    record = repo / RECORD_DIR_ROOT / RUN
    write_halt_marker(
        record, halted="ledger-check", sizing_rel=REL, plan_rel=PLAN_REL, blocking=("R1",),
        blocking_items=[R1_ITEM],
    )
    (record / "manifest.json").write_text(
        json.dumps({"rows": [{"id": i} for i in CODED], "owed": [{"id": i} for i in OWED], "gated": []}),
        encoding="utf-8",
    )
    msg = halted_record_message(RUN, {"halted": "ledger-check", "plan": PLAN_REL})
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", msg)
    pointer = ledger_halt_path(repo, PLAN_REL)
    pointer.parent.mkdir(parents=True, exist_ok=True)
    pointer.write_text(
        json.dumps(sizing_fire.LedgerHalt(RUN, PLAN_REL, REL, ("R1",)).to_json()), encoding="utf-8"
    )
    if refusal:
        write_halt_marker(record, halted="refusal", sizing_rel=REL, reason="plan-blitz pulled twice")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", f"warp: record halted {RUN} run artifacts\n\nHalted-By: refusal")
    _write_plan(repo, stamps)
    return stamps


def _assert_ledger(plan):
    assert plan is not None and plan.incomplete == ()
    assert plan.ledger.run_id == RUN
    assert plan.ledger.blocking == ("R1",)
    assert plan.ledger.remaining["R1"]["remaining"] == REMAINING


@pytest.mark.parametrize("refusal", [True, False])
def test_split_checkpoint_stamps_resume_at_the_ledger(repo, refusal):  # noqa: F811
    stamps = _incident(repo, refusal=refusal)
    assert stamps["C1"] != stamps["C2"]
    _assert_ledger(sizing_fire.resumable_plan({"plan": PLAN_REL}, REL, repo))


def test_single_commit_checkpoint_still_resumes_at_the_ledger(repo):  # noqa: F811
    _incident(repo, split=False)
    _assert_ledger(sizing_fire.resumable_plan({"plan": PLAN_REL}, REL, repo))


def test_the_gate_sets_resume_ledger_and_no_resume_rows(repo):  # noqa: F811
    _incident(repo)
    verdict = ask_gate._handler({"sizing_path": REL, "run_id": "warp-2026-10-21-refire-20261021T150000-aaaaaa"}, repo)
    assert verdict["resume_ledger"]["blocking"] == ["R1"]
    assert verdict["resume_ledger"]["remaining"]["R1"]["remaining"] == REMAINING
    assert not verdict.get("resume_rows")


def test_a_coded_row_stamped_by_no_landing_commit_is_terminal_coded(repo):  # noqa: F811
    stamps = _incident(repo)
    from coordinator_core.ops.dispatch_emit.emit import resume_landed_state

    state = resume_landed_state(repo, PLAN_REL)
    rows = [{"id": "C1", "disposition": "coded", "disposition_ref": stamps["C1"]}]
    assert sizing_fire._terminal_coded(rows, state) is False
    rows[0]["disposition_ref"] = "f" * 40
    assert sizing_fire._terminal_coded(rows, state) is True

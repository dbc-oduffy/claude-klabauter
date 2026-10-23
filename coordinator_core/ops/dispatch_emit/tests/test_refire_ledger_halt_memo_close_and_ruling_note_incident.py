"""A refire of a ledger-check halt whose only unstaged row is an inbound-memo close, with R1 blocking and a
revise note ruling new work, takes the ledger resume and stages a derived row carrying that work.

Provenance (never read): runs ask-20261023T031504-dd822d and its refire warp-...-20261023T032427-3210de took
the row resume ['C5'] and staged zero agents; baseline commit 681955bc62.
"""

from __future__ import annotations

import json
import subprocess

import pytest

from coordinator_core.ops.dispatch_emit import ask_gate
from coordinator_core.ops.dispatch_emit import requirements_ledger as rl
from coordinator_core.ops.dispatch_emit.ask_contract import (
    RECORD_DIR_ROOT,
    RESUME_LEDGER_DERIVED_FIELD,
    RESUME_LEDGER_UNLANDED_FIELD,
    halted_record_message,
)
from coordinator_core.ops.dispatch_emit.halted_run import write_halt_marker
from coordinator_core.ops.dispatch_emit.reply_sidecar import read_reply
from coordinator_core.ops.dispatch_emit.tests.test_ask_gate import ACCEPTED, REL, _put, repo  # noqa: F401
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

HALTED_RUN = "ask-20261023T031504-dd822d"
REFIRE_RUN = "warp-2026-10-23-governor-engine-root-mirror-resolve-dd822d-20261023T032427-3210de"
PLAN_REL = "docs/plans/2026-10-23-governor-engine-root-mirror-resolve.md"
MEMO = "state/cross-repo/inbox/2026-10-23-some-memo.md"
SCRIPT = "coordinator/bin/gem-triage-turn-intent.py"
GEM_HOME = "coordinator_core/group_em/gem_home.py"
J1 = (
    "Add gem_holder() to coordinator_core/group_em/gem_home.py and switch gem-triage-turn-intent.py "
    "to it. Work landed in 71ebad6c6a meets the rows it names."
)
REMAINING = ["gem-triage-turn-intent.py still depends on hooks _gem_home", "memo not closed"]
REGISTER = {
    "sources": [{"doc": "ask", "ref": "ask"}],
    "rows": [{"id": "R1", "source_text": "resolve the gem home through the engine", "surface": "internal", "status": "open"}],
}
SPINE = (
    "- id: C{n}\n  title: T{n}\n  disposition: {d}\n  writes:\n    - some/c{n}.py\n"
)


def _git(repo, *a):
    subprocess.run(["git", *a], cwd=str(repo), check=True, capture_output=True, **no_console_creationflags())


def _plan_text() -> str:
    rows = "".join(SPINE.format(n=n, d="open") for n in (1, 2, 3))
    rows += f"- id: C4\n  title: T4\n  disposition: open\n  register_rows: [R1]\n  writes:\n    - {SCRIPT}\n"
    rows += f"- id: C5\n  title: close memo\n  disposition: open\n  surface: {MEMO}\n  writes:\n    - {MEMO}\n"
    return f"---\nrun_id: fixture\n---\n\n## Tasks\n\n```yaml plan-tasks\n{rows}```\n"


def _incident_repo(repo):
    """C1/C4 landed by a checkpoint commit, C2/C3 by the halt commit's Lands-Chunks, C5 open and never staged."""
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t.example")
    _git(repo, "config", "user.name", "t")
    _put(repo, tshirt="S", route="spec-dispatch", accepted=ACCEPTED, plan=PLAN_REL, requirement_register=REGISTER)
    (repo / PLAN_REL).parent.mkdir(parents=True, exist_ok=True)
    (repo / PLAN_REL).write_text(_plan_text(), encoding="utf-8", newline="\n")
    for rel in (SCRIPT, GEM_HOME, MEMO, "some/c1.py", "some/c2.py", "some/c3.py"):
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text("x = 1\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(
        repo, "commit", "-q", "-m", "checkpoint(wave 0): 2 rows — C1, C4",
        "-m", f"Checkpoint-Plan: {PLAN_REL}\nCheckpoint-Base: abc1234",
    )
    rl.start(repo, HALTED_RUN)
    rl.revise(repo, HALTED_RUN, note=J1)
    record = repo / RECORD_DIR_ROOT / HALTED_RUN
    write_halt_marker(
        record, halted="ledger-check", sizing_rel=REL, plan_rel=PLAN_REL, blocking=("R1",),
        blocking_items=[{"id": "R1", "remaining": REMAINING, "evidence": "gem_holder not added"}],
    )
    (record / "manifest.json").write_text(
        json.dumps({"rows": [{"id": f"C{n}", "writes": [f"some/c{n}.py"]} for n in (1, 2, 3, 4)], "owed": [], "gated": []}),
        encoding="utf-8",
    )
    _git(repo, "add", "-A")
    msg = halted_record_message(HALTED_RUN, {"halted": "ledger-check", "plan": PLAN_REL})
    _git(repo, "commit", "-q", "-m", msg + "\nLands-Chunks: C2, C3\n")


def test_refire_takes_the_ledger_resume_not_the_memo_close_row_resume(repo):  # noqa: F811
    _incident_repo(repo)
    reply = ask_gate._handler({"sizing_path": REL, "run_id": REFIRE_RUN}, repo)
    assert reply.get("halt") is None, reply
    assert reply.get("resume_rows") is None and reply.get("resume_plan") is None
    resume = reply["resume_ledger"]
    assert resume["run_id"] == HALTED_RUN and resume["blocking"] == ["R1"]
    assert "C5" in resume[RESUME_LEDGER_UNLANDED_FIELD]
    assert resume[RESUME_LEDGER_DERIVED_FIELD] == ["LR1"]


def test_refire_stages_a_derived_row_for_r1_carrying_j1(repo):  # noqa: F811
    _incident_repo(repo)
    reply = ask_gate._handler({"sizing_path": REL, "run_id": REFIRE_RUN}, repo)
    assert reply.get("halt") is None, reply
    assert "- id: LR1" in (repo / PLAN_REL).read_text(encoding="utf-8")
    rows = read_reply(repo, reply["manifest_handle"])["rows"]
    derived = [r for r in rows if r["id"] == "LR1"]
    assert len(derived) == 1
    spine = (repo / PLAN_REL).read_text(encoding="utf-8")
    assert "gem_holder" in spine[spine.index("- id: LR1"):]

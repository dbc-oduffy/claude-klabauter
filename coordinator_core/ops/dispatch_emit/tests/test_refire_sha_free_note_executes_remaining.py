"""The 029d27 shape: a refire of a ledger-check halt whose only plan row is `coded`, declares no
`register_rows`, and whose remaining work names no path, dispatches the plan's serving-row writes."""

from __future__ import annotations

import json

import pytest

from coordinator_core.ops.dispatch_emit import ask_gate
from coordinator_core.ops.dispatch_emit import requirements_ledger as rl
from coordinator_core.ops.dispatch_emit.ask_contract import (
    LEG_DISPATCH,
    LEG_FOOTPRINT_FILE,
    LEG_SKIP,
    RECORD_DIR_ROOT,
    RESUME_LEDGER_LEG_FIELD,
    halted_record_message,
)
from coordinator_core.ops.dispatch_emit.halted_run import write_halt_marker
from coordinator_core.ops.dispatch_emit.tests.test_ask_gate import ACCEPTED, REL, _put, repo  # noqa: F401
from coordinator_core.ops.dispatch_emit.tests.test_ask_gate_resume import PLAN_REL
from coordinator_core.ops.dispatch_emit.tests.test_refire_after_ledger_halt_resumes_incident import _git

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

HALTED = "ask-halted"
FRESH = "warp-fresh-refire"
REGISTER = {"rows": [{"id": "R1", "source_text": "do the thing", "surface": "internal", "status": "open"}]}
WRITES = ["pkg/mod.py", "pkg/other.py"]
DISPATCHED = [*WRITES, "pkg/tests/test_mod.py", "pkg/tests/test_other.py"]


def _setup(repo, note, claims="register_claims:\n  rows: [R1]\n"):  # noqa: F811
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t.example")
    _git(repo, "config", "user.name", "t")
    (repo / PLAN_REL).parent.mkdir(parents=True, exist_ok=True)
    (repo / PLAN_REL).write_text(
        f"---\nrun_id: fixture\n{claims}---\n\n## Tasks\n\n```yaml plan-tasks\n"
        "- id: C1\n  title: TC1\n  disposition: coded\n  writes:\n    - pkg/mod.py\n    - pkg/other.py\n```\n",
        encoding="utf-8",
        newline="\n",
    )
    _put(repo, tshirt="S", route="spec-dispatch", accepted=ACCEPTED, plan=PLAN_REL, requirement_register=REGISTER)
    for rel in WRITES:
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text("x = 1\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    rl.start(repo, HALTED)
    rl.revise(repo, HALTED, note=note)
    record = repo / RECORD_DIR_ROOT / HALTED
    write_halt_marker(
        record, halted="ledger-check", sizing_rel=REL, plan_rel=PLAN_REL, blocking=("R1",),
        blocking_items=[{"id": "R1", "remaining": ["do the thing"], "evidence": "e"}],
    )
    (record / "manifest.json").write_text(
        json.dumps({"rows": [{"id": "C1", "writes": WRITES}]}), encoding="utf-8"
    )
    _git(repo, "add", "-A")
    msg = halted_record_message(HALTED, {"halted": "ledger-check", "plan": PLAN_REL})
    _git(repo, "commit", "-q", "-m", msg + "\nLands-Chunks: C1\n")
    rl.start(repo, FRESH, resumed_from=HALTED)


def test_a_sha_free_note_dispatches_the_plan_rows_writes(repo):  # noqa: F811
    note = "also cover the second module"
    _setup(repo, note)

    out = ask_gate._handler({"sizing_path": REL, "run_id": FRESH}, repo)

    resume = out["resume_ledger"]
    assert note in resume["revise_notes"], resume
    leg = resume[RESUME_LEDGER_LEG_FIELD]
    assert leg["action"] == LEG_DISPATCH and leg["writes"] == DISPATCHED, leg
    on_disk = json.loads((repo / RECORD_DIR_ROOT / FRESH / LEG_FOOTPRINT_FILE).read_text(encoding="utf-8"))
    assert on_disk["action"] == LEG_DISPATCH and on_disk["writes"] == DISPATCHED, on_disk


def test_a_ten_hex_sha_note_still_skips_when_no_row_serves(repo):  # noqa: F811
    _setup(repo, "already applied in 4257b125f8", claims="")

    out = ask_gate._handler({"sizing_path": REL, "run_id": FRESH}, repo)

    assert out["resume_ledger"][RESUME_LEDGER_LEG_FIELD]["action"] == LEG_SKIP, out["resume_ledger"]

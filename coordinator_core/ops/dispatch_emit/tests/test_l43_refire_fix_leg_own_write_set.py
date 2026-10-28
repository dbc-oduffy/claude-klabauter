"""L43 replay: a refire's fix leg may write a named path of the halted run's own write set (a
completed row's file), through the real ask_gate ledger-resume entry, and the emitted row prompt
names that file as inside the footprint. Halt shapes copy 7cb45c (`ledger-check`, failing test in a
completed-row file) and eddcc7 (halted.json `run_artifacts.withheld` naming completed-row files).

Trap: the prompt assertions inject `leg_writes` by `dataclasses.replace`; no production caller
(`op.py` -> `emit_script`) passes it, so they pin the clause text, not the refire's emitted prompt."""

from __future__ import annotations

import dataclasses
import json

import pytest

from coordinator_core.ops.dispatch_emit import ask_gate
from coordinator_core.ops.dispatch_emit import requirements_ledger as rl
from coordinator_core.ops.dispatch_emit.ask_contract import (
    LEG_DISPATCH,
    LEG_FOOTPRINT_FILE,
    LEG_REFUSE,
    RECORD_DIR_ROOT,
    RESUME_LEDGER_LEG_FIELD,
    halted_record_message,
    record_dir,
)
from coordinator_core.ops.dispatch_emit.emit import _row_prompt
from coordinator_core.ops.dispatch_emit.halted_run import write_halt_marker
from coordinator_core.ops.dispatch_emit.refire_inherited_scope import inherited_scope
from coordinator_core.ops.dispatch_emit.tests.test_ask_gate import ACCEPTED, REL, _put, repo  # noqa: F401
from coordinator_core.ops.dispatch_emit.tests.test_ask_gate_resume import PLAN_REL, _plan
from coordinator_core.ops.dispatch_emit.tests.test_footprint_miss_clause import _PLAN, _ctx, _row
from coordinator_core.ops.dispatch_emit.tests.test_refire_after_ledger_halt_executes_unmet_rows_incident import (
    HALTED_RUN,
    REFIRE_RUN,
    REGISTER,
)
from coordinator_core.ops.dispatch_emit.tests.test_refire_after_ledger_halt_resumes_incident import _git

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

DONE_FILE = "some/C1.py"
OTHER_FILE = "tools/not_in_the_run.py"
RED = f"{DONE_FILE}::test_x: boom"


def _halted(repo, remaining, *, withheld=None, tshirt="L", declared=(DONE_FILE,)):  # noqa: F811
    """A ledger-check halt whose only manifest row C1 wrote and landed DONE_FILE (Lands-Chunks: C1)."""
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t.example")
    _git(repo, "config", "user.name", "t")
    _plan(repo, rows=[("C1", "open")])
    _put(repo, tshirt=tshirt, route="spec-dispatch", accepted=ACCEPTED, plan=PLAN_REL, requirement_register=REGISTER)
    (repo / "some").mkdir(parents=True, exist_ok=True)
    (repo / "tools").mkdir(parents=True, exist_ok=True)
    (repo / DONE_FILE).write_text("x = 1\n", encoding="utf-8")
    (repo / OTHER_FILE).write_text("y = 1\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    rl.start(repo, HALTED_RUN)
    record = repo / RECORD_DIR_ROOT / HALTED_RUN
    write_halt_marker(
        record, halted="ledger-check", sizing_rel=REL, plan_rel=PLAN_REL, blocking=("R1",),
        blocking_items=[{"id": "R1", "remaining": remaining, "evidence": "e"}],
    )
    if withheld:
        marker = record / "halted.json"
        doc = json.loads(marker.read_text(encoding="utf-8"))
        doc["run_artifacts"] = {"withheld": {p: "halt-record-only" for p in withheld}}
        marker.write_text(json.dumps(doc), encoding="utf-8")
    (record / "manifest.json").write_text(
        json.dumps({"rows": [{"id": "C1", "writes": [DONE_FILE]}], "review_declared_paths": list(declared)}),
        encoding="utf-8",
    )
    _git(repo, "add", "-A")
    msg = halted_record_message(HALTED_RUN, {"halted": "ledger-check", "plan": PLAN_REL})
    _git(repo, "commit", "-q", "-m", msg + "\nLands-Chunks: C1\n")


def _leg(repo):  # noqa: F811
    out = ask_gate._handler({"sizing_path": REL, "run_id": REFIRE_RUN}, repo)
    return out, out["resume_ledger"][RESUME_LEDGER_LEG_FIELD]


def _prompt(leg) -> str:
    ctx = dataclasses.replace(_ctx(), leg_writes=tuple(leg["writes"]))
    return _row_prompt(_row("C1", "tools/a.py"), _PLAN, ctx)


def test_7cb45c_red_in_a_completed_row_file_dispatches_the_leg_on_that_file(repo):  # noqa: F811
    _halted(repo, [RED])

    out, leg = _leg(repo)

    assert out.get("halt") is None, out.get("halt")
    assert leg["action"] == LEG_DISPATCH and DONE_FILE in leg["writes"], leg
    assert not leg["unwritable"], leg
    assert DONE_FILE in inherited_scope(repo, out["resume_ledger"])
    footprint = json.loads((record_dir(repo, REFIRE_RUN) / LEG_FOOTPRINT_FILE).read_text(encoding="utf-8"))
    assert DONE_FILE in footprint["writes"]
    prompt = _prompt(leg)
    assert f"`{DONE_FILE}`" in prompt and "never outside-footprint" in prompt


def test_red_in_a_file_outside_the_run_write_set_still_refuses(repo):  # noqa: F811
    # The S arm derives a row for any named file, so the refusal side is pinned on the leg plan the
    # gate calls, with the inherited set the gate computes for this halt.
    from coordinator_core.ops.dispatch_emit.ledger_remaining_paths import remaining_leg_plan

    _halted(repo, [RED, f"{OTHER_FILE}::test_y: boom"])
    out, _leg_plan = _leg(repo)
    inherited = list(inherited_scope(repo, out["resume_ledger"]))
    assert DONE_FILE in inherited and OTHER_FILE not in inherited

    remaining = {"R1": {"remaining": [RED, f"{OTHER_FILE}::test_y: boom"]}}
    leg = remaining_leg_plan(remaining, [], [], ["R1"], [], None, repo, inherited=inherited)

    assert leg["action"] == LEG_REFUSE and leg["unwritable"] == [OTHER_FILE], leg
    assert DONE_FILE in leg["writes"] and OTHER_FILE not in leg["writes"]


def test_eddcc7_withheld_completed_row_file_is_the_legs_write_and_the_refire_does_not_rehalt(repo):  # noqa: F811
    _halted(repo, [RED], withheld=[DONE_FILE], declared=())

    out, leg = _leg(repo)

    assert (out.get("halt") or {}).get("reason") is None, out.get("halt")
    assert leg["action"] == LEG_DISPATCH and DONE_FILE in leg["writes"], leg
    assert f"`{DONE_FILE}`" in _prompt(leg)

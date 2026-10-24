"""dispatch.ask_gate at arm s derives one row per open ledger item, appends it to the resumed spine and stages it."""

from __future__ import annotations

import json

import pytest

from coordinator_core.ops.dispatch_emit import ask_gate
from coordinator_core.ops.dispatch_emit import requirements_ledger as rl
from coordinator_core.ops.dispatch_emit.ask_contract import (
    LEG_DISPATCH,
    RECORD_DIR_ROOT,
    RESUME_LEDGER_ARM_FIELD,
    RESUME_LEDGER_DERIVED_FIELD,
    RESUME_LEDGER_LEG_FIELD,
    RESUME_LEDGER_UNSERVED_FIELD,
    halted_record_message,
)
from coordinator_core.ops.dispatch_emit.halted_run import write_halt_marker
from coordinator_core.ops.dispatch_emit.tests.test_ask_gate import ACCEPTED, REL, _put, repo  # noqa: F401
from coordinator_core.ops.dispatch_emit.tests.test_ask_gate_resume import PLAN_REL, _plan
from coordinator_core.ops.dispatch_emit.tests.test_refire_after_ledger_halt_executes_unmet_rows_incident import (
    HALTED_RUN,
    REFIRE_RUN,
    REGISTER,
)
from coordinator_core.ops.dispatch_emit.tests.test_refire_after_ledger_halt_resumes_incident import _git

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

OWED = "coordinator_core/owed_module.py"


def _halted(repo, remaining, tshirt="S", route="spec-dispatch"):
    """A ledger-check halt whose only plan row landed, leaving R1 open."""
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t.example")
    _git(repo, "config", "user.name", "t")
    _plan(repo, rows=[("C1", "open")])
    _put(repo, tshirt=tshirt, route=route, accepted=ACCEPTED, plan=PLAN_REL, requirement_register=REGISTER)
    (repo / "some").mkdir(parents=True, exist_ok=True)
    (repo / "some" / "C1.py").write_text("x = 1\n", encoding="utf-8")
    (repo / "coordinator_core").mkdir(parents=True, exist_ok=True)
    (repo / OWED).write_text("y = 1\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    rl.start(repo, HALTED_RUN)
    record = repo / RECORD_DIR_ROOT / HALTED_RUN
    write_halt_marker(
        record, halted="ledger-check", sizing_rel=REL, plan_rel=PLAN_REL, blocking=("R1",),
        blocking_items=[{"id": "R1", "remaining": remaining, "evidence": "e"}],
    )
    (record / "manifest.json").write_text(
        json.dumps({"rows": [{"id": "C1", "writes": ["some/C1.py"]}]}), encoding="utf-8"
    )
    _git(repo, "add", "-A")
    msg = halted_record_message(HALTED_RUN, {"halted": "ledger-check", "plan": PLAN_REL})
    _git(repo, "commit", "-q", "-m", msg + "\nLands-Chunks: C1\n")


def _manifest_rows(repo):
    return json.loads((repo / RECORD_DIR_ROOT / REFIRE_RUN / "manifest.json").read_text(encoding="utf-8"))["rows"]


def test_open_item_stages_one_derived_row_and_keeps_arm_s(repo):  # noqa: F811
    _halted(repo, [f"fix {OWED} so the check passes"])

    out = ask_gate._handler({"sizing_path": REL, "run_id": REFIRE_RUN}, repo)

    resume = out["resume_ledger"]
    assert out["arm"] == "s" and resume[RESUME_LEDGER_ARM_FIELD] == "s", out
    assert resume[RESUME_LEDGER_DERIVED_FIELD] == ["LR1"], resume
    assert "LR1" in (repo / PLAN_REL).read_text(encoding="utf-8")
    staged = [r for r in _manifest_rows(repo) if r["id"] == "LR1"]
    assert len(staged) == 1 and OWED in staged[0]["writes"], staged
    assert resume[RESUME_LEDGER_LEG_FIELD]["action"] == LEG_DISPATCH
    assert OWED in resume[RESUME_LEDGER_LEG_FIELD]["writes"]


def test_a_second_gate_call_never_duplicates_the_derived_row(repo):  # noqa: F811
    _halted(repo, [f"fix {OWED}"])

    ask_gate._handler({"sizing_path": REL, "run_id": REFIRE_RUN}, repo)
    ask_gate._handler({"sizing_path": REL, "run_id": REFIRE_RUN}, repo)

    assert (repo / PLAN_REL).read_text(encoding="utf-8").count("id: LR1") == 1


def test_case_variant_writes_derive_in_the_tracked_spelling_and_uncreated_paths_are_refused(repo):  # noqa: F811
    _halted(repo, ["fix coordinator/bin/BLOCKED.py and coordinator/bin/tests/test_BLOCKED.py"])
    (repo / "coordinator" / "bin").mkdir(parents=True)
    (repo / "coordinator" / "bin" / "blocked.py").write_text("b = 1\n", encoding="utf-8")
    (repo / "coordinator" / "bin" / "publish.py").write_text("p = 1\n", encoding="utf-8")
    plan = repo / PLAN_REL
    plan.write_text(
        "---\nrun_id: fixture\n---\n\n## Tasks\n\n```yaml plan-tasks\n"
        "- id: C1\n  title: TC1\n  disposition: open\n  register_rows:\n    - R1\n  writes:\n"
        "    - coordinator/bin/Publish.py\n```\n",
        encoding="utf-8",
        newline="\n",
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "track bin")

    out = ask_gate._handler({"sizing_path": REL, "run_id": REFIRE_RUN}, repo)

    staged = [r for r in _manifest_rows(repo) if r["id"] == "LR1"]
    assert len(staged) == 1, out
    writes = staged[0]["writes"]
    assert "coordinator/bin/publish.py" in writes, writes
    assert not any(w in writes for w in (
        "coordinator/bin/Publish.py", "coordinator/bin/BLOCKED.py", "coordinator/bin/tests/test_BLOCKED.py",
    )), writes
    assert "coordinator/bin/tests/test_BLOCKED.py" in (repo / PLAN_REL).read_text(encoding="utf-8")


def test_an_underivable_item_routes_to_unserved_rows_without_a_manifest_error(repo):  # noqa: F811
    _halted(repo, ["do the thing"])

    out = ask_gate._handler({"sizing_path": REL, "run_id": REFIRE_RUN}, repo)

    assert "R1" in out["resume_ledger"][RESUME_LEDGER_UNSERVED_FIELD], out
    assert "error" not in (out.get("manifest") or {}), out
    assert "LR1" not in (repo / PLAN_REL).read_text(encoding="utf-8")

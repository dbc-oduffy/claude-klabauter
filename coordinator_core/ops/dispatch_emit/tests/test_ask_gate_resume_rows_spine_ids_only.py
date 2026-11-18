"""A ledger resume stages only spine row ids: a ledger item owed by a spine row resolves to that row."""

from __future__ import annotations

import json

import pytest

from coordinator_core.ops.dispatch_emit import ask_gate
from coordinator_core.ops.dispatch_emit import requirements_ledger as rl
from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT, halted_record_message
from coordinator_core.ops.dispatch_emit.halted_run import write_halt_marker
from coordinator_core.ops.dispatch_emit.tests.test_ask_gate import ACCEPTED, REL, _put, repo  # noqa: F401
from coordinator_core.ops.dispatch_emit.tests.test_ask_gate_resume import PLAN_REL
from coordinator_core.ops.dispatch_emit.tests.test_refire_after_ledger_halt_executes_unmet_rows_incident import (
    HALTED_RUN,
    REFIRE_RUN,
    REGISTER,
)
from coordinator_core.ops.dispatch_emit.tests.test_refire_after_ledger_halt_resumes_incident import _git

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

PROSE = "R1: blocking, owed by C4 (some/C4.py slow-disk test)"
SPINE = (
    "- id: C1\n  title: TC1\n  disposition: open\n  register_rows: [R1]\n  writes:\n    - some/C1.py\n"
    "- id: C4\n  title: TC4\n  disposition: open\n  register_rows: [R1]\n  writes:\n    - some/C4.py\n"
)


def _halted(repo) -> None:
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t.example")
    _git(repo, "config", "user.name", "t")
    plan = repo / PLAN_REL
    plan.parent.mkdir(parents=True, exist_ok=True)
    plan.write_text(
        f"---\nrun_id: fixture\n---\n\n## Tasks\n\n```yaml plan-tasks\n{SPINE}```\n", encoding="utf-8", newline="\n"
    )
    _put(repo, tshirt="S", route="spec-dispatch", accepted=ACCEPTED, plan=PLAN_REL, requirement_register=REGISTER)
    (repo / "some").mkdir(parents=True, exist_ok=True)
    (repo / "some" / "C1.py").write_text("x = 1\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    rl.start(repo, HALTED_RUN)
    record = repo / RECORD_DIR_ROOT / HALTED_RUN
    write_halt_marker(
        record, halted="ledger-check", sizing_rel=REL, plan_rel=PLAN_REL, blocking=("R1",),
        blocking_items=[{"id": "R1", "remaining": ["finish some/C4.py"], "evidence": "e"}],
    )
    (record / "manifest.json").write_text(
        json.dumps({"rows": [{"id": "C1", "writes": ["some/C1.py"]}, {"id": "C4", "writes": ["some/C4.py"]}]}),
        encoding="utf-8",
    )
    _git(repo, "add", "-A")
    msg = halted_record_message(HALTED_RUN, {"halted": "ledger-check", "plan": PLAN_REL})
    _git(repo, "commit", "-q", "-m", msg + "\nLands-Chunks: C1\n")


def _fire(repo, monkeypatch, unlanded):
    """Run the gate's ledger-resume path with `unlanded` appended to the ledger's unlanded list; return `(reply, only_rows stage saw)`."""
    real_ledger, real_stage, seen = ask_gate._resume_ledger, ask_gate.stage, {}

    def _ledger(*a, **k):
        out = real_ledger(*a, **k)
        field = ask_gate.RESUME_LEDGER_UNLANDED_FIELD
        out[field] = list(dict.fromkeys([*out[field], *unlanded]))
        return out

    def _stage(*a, **k):
        seen["only_rows"] = list(k["only_rows"])
        return real_stage(*a, **k)

    monkeypatch.setattr(ask_gate, "_resume_ledger", _ledger)
    monkeypatch.setattr(ask_gate, "stage", _stage)
    out = ask_gate._handler({"sizing_path": REL, "run_id": REFIRE_RUN}, repo)
    return out, seen.get("only_rows", [])


@pytest.mark.parametrize("entry", ["C4", "R1", PROSE])
def test_a_ledger_item_owed_by_a_spine_row_stages_that_row_not_the_item(repo, monkeypatch, entry):  # noqa: F811
    _halted(repo)

    out, only_rows = _fire(repo, monkeypatch, [entry])

    assert "error" not in out.get("manifest", {}), out
    assert "manifest_handle" in out, out
    assert "C4" in only_rows
    assert PROSE not in only_rows and "R1" not in only_rows, only_rows
    spine = (repo / PLAN_REL).read_text(encoding="utf-8")
    assert all(f"- id: {row}\n" in spine for row in only_rows), only_rows


def test_a_resume_whose_every_entry_drops_stages_no_row_not_every_row(repo, monkeypatch):  # noqa: F811
    _halted(repo)
    real_ledger, real_stage, seen = ask_gate._resume_ledger, ask_gate.stage, {}

    def _ledger(*a, **k):
        out = real_ledger(*a, **k)
        out[ask_gate.RESUME_LEDGER_UNLANDED_FIELD] = ["R9: blocking"]
        out["blocking"] = [*(out.get("blocking") or []), "R9"]
        return out

    def _stage(*a, **k):
        seen["only_rows"] = k["only_rows"]
        manifest = real_stage(*a, **k)
        seen["staged"] = [r.id for r in manifest.rows]
        return manifest

    monkeypatch.setattr(ask_gate, "_resume_ledger", _ledger)
    monkeypatch.setattr(ask_gate, "stage", _stage)
    monkeypatch.setattr(ask_gate, "_derive_resume_rows", lambda *a, **k: [])

    ask_gate._handler({"sizing_path": REL, "run_id": REFIRE_RUN}, repo)

    assert seen.get("only_rows") == [], seen
    assert not seen.get("staged"), seen


def test_an_entry_no_ledger_item_names_passes_through_for_stage_to_refuse(repo):  # noqa: F811
    _halted(repo)
    resume_out = {"blocking": ["R1"], "ledger_item_ids": ["R1"], "rulable_ids": ["R1"]}

    kept = ask_gate._spine_resume_rows(repo, PLAN_REL, ["ZZ9", "C1", "R1: owed by C4"], resume_out)

    assert kept == ["ZZ9", "C1", "C4"], kept


def test_an_item_no_open_spine_row_serves_is_dropped(repo):  # noqa: F811
    _halted(repo)

    kept = ask_gate._spine_resume_rows(repo, PLAN_REL, ["R9: blocking"], {"blocking": ["R9"]})

    assert kept == []

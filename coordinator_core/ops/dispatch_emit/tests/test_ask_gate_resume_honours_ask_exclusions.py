"""A ledger-halt resume emits the sizing's ask exclusions and the remaining leg honours them.

Derived rows are checked by calling `derive_ledger_rows` with the emitted field, not through `_derive_resume_rows`.
"""

from __future__ import annotations

import pytest

from coordinator_core.ops.dispatch_emit import ask_gate, ledger_remaining_paths as lrp
from coordinator_core.ops.dispatch_emit.ask_contract import RESUME_LEDGER_LEG_FIELD
from coordinator_core.ops.dispatch_emit.tests import test_ask_gate_resume_ledger_remaining_leg as base
from coordinator_core.ops.dispatch_emit.tests.test_ask_gate import REL, repo  # noqa: F401
from coordinator_core.ops.dispatch_emit.tests.test_refire_after_ledger_halt_executes_unmet_rows_incident import (
    REFIRE_RUN,
)

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

EXCLUDED = "global-doctrine/CLAUDE.md"


def _excluding_register(monkeypatch):
    register = {
        "sources": [{"doc": "ask", "ref": "ask"}],
        "rows": [{"id": "R1", "source_text": "do not touch global-doctrine", "surface": "internal", "status": "open"}],
    }
    monkeypatch.setattr(base, "REGISTER", register)


def test_resume_omits_excluded_path_from_leg_and_derived_rows(repo, monkeypatch):  # noqa: F811
    _excluding_register(monkeypatch)
    (repo / "global-doctrine").mkdir()
    (repo / "global-doctrine" / "CLAUDE.md").write_text("x\n", encoding="utf-8")
    base._halted(repo, [f"edit {EXCLUDED}", "update some/C1.py"])

    out = ask_gate._handler({"sizing_path": REL, "run_id": REFIRE_RUN}, repo)

    resume = out["resume_ledger"]
    assert resume[ask_gate.RESUME_EXCLUDE_FIELD] == ["global-doctrine"]
    leg = resume[RESUME_LEDGER_LEG_FIELD]
    assert EXCLUDED not in leg["writes"] + leg["em_owed"] + leg["unwritable"], leg
    rows, _ = lrp.derive_ledger_rows(
        resume["remaining"], resume["revise_notes"], ["R1"], {"R1": [EXCLUDED, "some/C1.py"]}, [], repo,
        exclude=resume[ask_gate.RESUME_EXCLUDE_FIELD],
    )
    assert all(EXCLUDED not in r["writes"] for r in rows)
    assert rows and rows[0]["writes"] == ["some/C1.py"]

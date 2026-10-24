"""A re-fire after workflow.revise minted a register row no plan row serves reports it in resume_ledger.unserved_rows at arms S and XS."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from coordinator_core.ops.dispatch_emit import ask_gate
from coordinator_core.ops.dispatch_emit import requirements_ledger as rl
from coordinator_core.ops.dispatch_emit.ask_contract import RESUME_LEDGER_UNSERVED_FIELD
from coordinator_core.ops.dispatch_emit.tests.test_ask_gate import REL, repo  # noqa: F401
from coordinator_core.ops.dispatch_emit.tests.test_refire_ledger_halt_replays_20261022 import (
    REFIRE_RUN,
    S_RUN,
    XS_OWED,
    XS_RUN,
    XS_SPINE,
    _s_replay_repo,
    _spine,
    _xs_replay_repo,
)
from coordinator_core.ops.dispatch_emit.tests.test_ask_gate_resume import PLAN_REL
from coordinator_core.ops.dispatch_emit.tests.test_refire_after_ledger_halt_resumes_incident import _git

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

VERBATIM = "also cover the Windows paths"


def _revise(repo, run):
    """Mint R2 through workflow.revise on the halted run's bound ledger."""
    rl.bind(repo, run, REL, ask_text="the owed fix")
    out = rl._handler({"run_id": run, "verbatim": VERBATIM}, repo_root=repo)
    assert out["ok"] is True, out
    return out["item"]


def _unserved(out):
    return (out.get("resume_ledger") or {}).get(RESUME_LEDGER_UNSERVED_FIELD)


def test_s_arm_revise_row_no_plan_row_serves_is_unserved(repo):  # noqa: F811
    _s_replay_repo(repo)
    item = _revise(repo, S_RUN)

    out = ask_gate._handler({"sizing_path": REL, "run_id": REFIRE_RUN}, repo)

    assert _unserved(out) == [item], out["resume_ledger"]
    assert "error" not in (out.get("manifest") or {}), out


def test_xs_arm_revise_row_no_record_spine_row_serves_is_unserved(repo):  # noqa: F811
    _xs_replay_repo(repo)
    item = _revise(repo, XS_RUN)

    out = ask_gate._handler({"sizing_path": REL, "run_id": REFIRE_RUN, "writes": [XS_OWED]}, repo)

    assert out["arm"] == "xs" and _unserved(out) == [item], out["resume_ledger"]
    assert "error" not in (out.get("manifest") or {}), out


def test_a_revise_row_a_plan_row_already_serves_is_not_unserved(repo):  # noqa: F811
    _xs_replay_repo(repo)
    item = _revise(repo, XS_RUN)
    _spine(
        repo / XS_SPINE,
        [("X1", "open", [XS_OWED], ["R1"]), ("X2", "voided", ["some/X2.py"], []), ("X3", "coded", [XS_OWED], [item])],
    )

    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "serve the revise row")

    out = ask_gate._handler({"sizing_path": REL, "run_id": REFIRE_RUN, "writes": [XS_OWED]}, repo)

    assert "resume_ledger" in out and _unserved(out) is None, out


def test_an_unreadable_plan_adds_nothing(repo):  # noqa: F811
    (repo / PLAN_REL).parent.mkdir(parents=True, exist_ok=True)
    (repo / PLAN_REL).write_text("no spine here\n", encoding="utf-8")
    halt = SimpleNamespace(plan_rel=PLAN_REL)

    assert ask_gate._unserved_revise_rows(["R2"], {}, [], halt, repo) == []
    assert ask_gate._unserved_revise_rows(["R2"], {"R2": {"remaining": ["x"]}}, [], halt, repo) == []

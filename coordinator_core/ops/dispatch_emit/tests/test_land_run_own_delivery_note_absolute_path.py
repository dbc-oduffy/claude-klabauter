"""A remnant leg that reports its own delivery note by absolute in-repo path never blocks the run's
landing, while a foreign path changed after measure still refuses `landing-tree-differs-from-measured`."""

from __future__ import annotations

import json

import pytest

from coordinator_core.ops.dispatch_emit import land_run
from coordinator_core.ops.dispatch_emit import ledger_recovery as lr
from coordinator_core.ops.dispatch_emit.ask_contract import RUN_DIR_ROOT
from coordinator_core.ops.dispatch_emit.tests.test_ask_ledger_resume_e2e import (  # noqa: F401
    RUN2,
    halted,
)
from coordinator_core.ops.dispatch_emit.tests.test_ask_run_product_commit import _git, _write, repo  # noqa: F401
from coordinator_core.ops.dispatch_emit.tests.test_land_run_measured_tree_gate import (
    _VERDICT,
    _record,
    _stage,
)
from coordinator_core.ops.dispatch_emit.tests.test_land_run_met_over_footprint_breach_incident import (
    C3_PATH,
    C4_PATH,
    LANE,
    LANE_TEST,
)
from coordinator_core.ops.dispatch_emit.tests.test_ledger_remaining_leg_footprint_fence import _terminal_with

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

NOTE = f"docs/delivery/{RUN2}.md"
MEASURED = [C3_PATH, C4_PATH, LANE, LANE_TEST]


def _remnant_record(root, reported: str) -> None:
    remnant = {"status": "fixed", "footprint": [NOTE], "paths": [reported], "live_tree_writes": [], "still_open": []}
    _write(root, f"{RUN_DIR_ROOT}/{RUN2}/{lr.RECOVERY_RECORD_FILE}", json.dumps({"remnant": remnant}))


def test_own_delivery_note_reported_by_absolute_path_after_measure_still_lands(halted):  # noqa: F811
    resume, token = _stage(halted)
    _record(halted, MEASURED)
    _write(halted, NOTE, "# delivery\n")
    _remnant_record(halted, (halted / NOTE).as_posix())

    out = _terminal_with(halted, token, resume, _VERDICT)

    assert out["committed"] is True, out
    assert out.get("refused") is None
    assert land_run._remnant_breach(halted, halted / RUN_DIR_ROOT / RUN2) == []


def test_foreign_path_changed_after_measure_is_still_refused(halted):  # noqa: F811
    resume, token = _stage(halted)
    _record(halted, [C3_PATH, C4_PATH])
    _write(halted, NOTE, "# delivery\n")
    _remnant_record(halted, (halted / LANE).as_posix())
    head = _git(halted, "rev-parse", "HEAD")

    out = _terminal_with(halted, token, resume, _VERDICT)

    assert out["committed"] is False and out["refused"] == land_run.LANDING_TREE_DIFFERS, out
    assert LANE in out["paths"]
    assert _git(halted, "rev-parse", "HEAD") == head

"""P-D replay: repo-wide invariant tests are selected for the diffs that change their measured set.

Witnesses: doe's every-session floor (first red dc548166dd) and review-findings pin (first red
b6f0694c43), and the blitz 20261024T235752Z base red test_machinery_paths (tracked state/ segments
apm-rulings, rulings, superseding-reviews off the allowlist). The doe tests are vendored as empty
fixture files; nothing reads another repo. Selection is asserted through retest_targeted.main.
"""
from __future__ import annotations


import pytest


pytestmark = [pytest.mark.complete]

RUN_ID = "run-pd"
MACHINERY = "coordinator_core/session/tests/test_machinery_paths.py"
MACHINERY_NODE = MACHINERY + "::test_every_tracked_state_first_segment_is_on_the_allowlist"
RAW_WRITES = "coordinator_core/tests/test_raw_writes_have_a_disposition.py"
FLOOR = "coordinator/tests/test_every_session_floor.py"
PIN = "coordinator/tests/test_review_findings_pin.py"


def test_a_hand_built_join_in_an_engine_module_selects_the_record_home_invariant():
    from coordinator_core.ops.dispatch_emit import invariant_measured_paths as imp

    node = imp.invariants_for_paths(["coordinator_core/ops/dispatch_emit/stage_receipts.py"])
    assert any("test_no_hand_built_record_home_joins" in n for n in node)
    assert imp.invariants_for_paths(["coordinator_core/ops/tests/test_other.py"]) == []

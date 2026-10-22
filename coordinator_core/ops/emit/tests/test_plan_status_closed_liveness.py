"""A plan with status `closed` is terminal: DONE in liveness() and in the published mapping."""

from coordinator_core.ops.emit_artifact_shape_contract import LIVENESS_MAPPING
from coordinator_core.ops.records_query import liveness


def test_closed_plan_is_done():
    assert liveness({"status": "closed"}, "plan") == "DONE"


def test_closed_plan_in_published_mapping_as_done():
    assert LIVENESS_MAPPING["types"]["plan"]["mapping"]["closed"] == "DONE"

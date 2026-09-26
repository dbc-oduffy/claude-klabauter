
from coordinator_core.workstream_complete import build_review_scale_judgment_point
from coordinator_core.workstream_complete.directives_review import ReviewScaleDecision


def _decision(*, partition_mandatory):
    return ReviewScaleDecision(
        row=4,
        scale="partitioned",
        partition_mandatory=partition_mandatory,
        commit_message_names_change=False,
        reason="brightline exceeded",
        resolved=True,
    )


def test_partition_mandatory_recommendation_carries_the_satisfaction_clause():
    jp = build_review_scale_judgment_point(_decision(partition_mandatory=True))

    assert "satisfied, not overridden" in jp["recommendation"]["rationale"]


def test_non_mandatory_recommendation_omits_the_clause():
    jp = build_review_scale_judgment_point(_decision(partition_mandatory=False))

    assert "satisfied, not overridden" not in jp["recommendation"]["rationale"]

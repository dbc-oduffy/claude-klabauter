
from coordinator_core.workstream_complete import directives_review


def _unresolved():
    return directives_review._decide_review_scale_core(
        gross_loc=None,
        code_loc=None,
        commit_count=None,
        surface_count=None,
        executor_dispatched=False,
        shared_schema_touched=False,
        chain_disposition="single-session",
    )


def test_the_unresolved_arm_does_not_report_a_measured_negative():
    d = _unresolved()

    assert d.resolved is False
    assert d.scale == "unresolved"
    assert d.row is None
    assert d.partition_mandatory is None


def test_none_stays_falsy_so_no_truthiness_reader_changes_behaviour():
    d = _unresolved()

    assert not d.partition_mandatory


def test_a_resolved_small_diff_still_reports_a_real_false():
    d = directives_review._decide_review_scale_core(
        gross_loc=10,
        code_loc=10,
        commit_count=1,
        surface_count=1,
        executor_dispatched=False,
        shared_schema_touched=False,
        chain_disposition="single-session",
    )

    assert d.resolved is True
    assert d.partition_mandatory is False

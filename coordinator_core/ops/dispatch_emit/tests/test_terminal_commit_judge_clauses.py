"""The terminal judge's status is derived from its clauses, never taken from the reply alone."""

from coordinator_core.ops.dispatch_emit.terminal_commit import _derive_judge_status


def _review(**criterion):
    return {"criterion": criterion}


def test_met_with_a_not_met_clause_derives_not_met():
    review = _review(status="met", clauses=[{"status": "met"}, {"status": "not_met"}], observed=["x"])
    assert _derive_judge_status(review)
    assert review["criterion"]["status"] == "not_met"


def test_met_with_an_indeterminate_clause_derives_indeterminate():
    review = _review(status="met", clauses=[{"status": "indeterminate"}], observed=["x"])
    assert _derive_judge_status(review)
    assert review["criterion"]["status"] == "indeterminate"


def test_met_with_no_clauses_derives_indeterminate():
    review = _review(status="met", clauses=[], observed=["x"])
    assert _derive_judge_status(review)
    assert review["criterion"]["status"] == "indeterminate"


def test_met_with_nothing_observed_derives_indeterminate():
    review = _review(status="met", clauses=[{"status": "met"}], observed=[])
    assert _derive_judge_status(review)
    assert review["criterion"]["status"] == "indeterminate"


def test_disagreement_is_recorded_as_a_deviation():
    review = _review(status="met", clauses=[{"status": "not_met"}], observed=["x"])
    note = _derive_judge_status(review)
    assert review["criterion"]["deviations"] == [note]


def test_agreeing_met_is_untouched():
    review = _review(status="met", clauses=[{"status": "met"}], observed=["x"])
    assert _derive_judge_status(review) is None
    assert review["criterion"] == {"status": "met", "clauses": [{"status": "met"}], "observed": ["x"]}


def test_met_reply_without_a_clauses_key_is_left_alone():
    review = _review(status="met", observation="a.py exists")
    assert _derive_judge_status(review) is None
    assert review["criterion"]["status"] == "met"


def test_non_met_reply_is_never_changed():
    review = _review(status="not_met", clauses=[{"status": "met"}], observed=["x"])
    assert _derive_judge_status(review) is None
    assert review["criterion"]["status"] == "not_met"

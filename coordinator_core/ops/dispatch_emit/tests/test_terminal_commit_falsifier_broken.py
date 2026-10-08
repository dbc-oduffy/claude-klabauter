"""dispatch.terminal_commit withholds `implemented` from a plan the pre-dispatch
falsifier-integrity review called BROKEN.

BROKEN is advisory (DoE ruling, memo 2026-10-07-content-root-em-falsifier-integrity-
advisory): the plan executes, and only the judge's self-stamp is withheld. These pin
the plan-path match that decides it, and that the digest relays the mark.
"""

from __future__ import annotations

from coordinator_core.ops.dispatch_emit import terminal_commit, wake_digest
from coordinator_core.ops.dispatch_emit.terminal_commit import _falsifier_broken_tells


def test_a_marked_plan_returns_its_tells_under_any_path_spelling():
    marks = [{"plan": "docs/plans/p.md", "tells": ["SCOPE-WIDER-THAN-CLAIM"]}]
    for spelling in ("docs/plans/p.md", "./docs/plans/p.md", r"docs\plans\p.md"):
        assert _falsifier_broken_tells(marks, spelling) == ["SCOPE-WIDER-THAN-CLAIM"]


def test_an_unmarked_plan_or_absent_marks_return_none():
    assert _falsifier_broken_tells([{"plan": "docs/plans/other.md", "tells": []}], "docs/plans/p.md") is None
    assert _falsifier_broken_tells(None, "docs/plans/p.md") is None
    assert _falsifier_broken_tells([], None) is None


def test_a_mark_with_no_tells_still_withholds():
    assert _falsifier_broken_tells([{"plan": "docs/plans/p.md", "tells": []}], "docs/plans/p.md") == []


def test_terminal_commit_accepts_the_param():
    assert any(f.name == "falsifier_broken" for f in terminal_commit._PARAM_FIELDS)


def test_the_digest_relays_the_mark_only_with_predispatch():
    kw = dict(
        has_commit_request=True, review_vars=None, test_var=None, falsifier_var=None,
        verification_var="_verifications", test_absent_status="not_run",
    )
    _, _, with_pre = wake_digest.next_action_parts(predispatch=True, **kw)
    _, _, without = wake_digest.next_action_parts(predispatch=False, **kw)
    assert "falsifier_broken: [..._falsifierBroken.entries()]" in with_pre
    assert "falsifier_broken" not in without

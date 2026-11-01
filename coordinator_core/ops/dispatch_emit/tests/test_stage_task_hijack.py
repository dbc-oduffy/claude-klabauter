"""Pins the stage-task hijack classifier and its JS twin."""

import pytest

from coordinator_core.ops.dispatch_emit import stage_task_hijack as sth

HIJACKED = [
    (
        "plan-blitz reported no ready plan: The relayed user request only asks for a status relay of PR #210 "
        "(\"please relay back what we're working on here\"). It does not ask for a plan, so I wrote no plan "
        "and no sidecar."
    ),
    (
        "The user's relayed request asked for a status relay on PR #210, not a plan. The user's request takes "
        "precedence over the computed plan-authoring task, so no plan was written."
    ),
    (
        "Commit not run. The relayed user request (status relay on PR #210) takes precedence over the "
        "computed commit-stage task, so I ran only read-only `gh pr view 210 --comments`."
    ),
]

NOT_HIJACKED = [
    "plan-blitz reported no ready plan: scaffold markers remain",
    "land refused: deadline exceeded before the base moved; retry after the next wave",
    "plan-blitz refused: the user request names plan docs/plans/x.md, which has scaffold markers",
    "land refused: the user message arrived mid-run; base moved, retry the commit",
    "",
    None,
]


@pytest.mark.parametrize("text", HIJACKED)
def test_memo_replies_classify_as_hijacked(text):
    excerpt = sth.classify(text)
    assert excerpt
    assert excerpt in text


@pytest.mark.parametrize("text", NOT_HIJACKED)
def test_ordinary_refusals_and_empty_are_not_hijacked(text):
    assert sth.classify(text) is None


def test_decline_without_user_reference_is_not_hijacked():
    assert sth.classify("I declined to run: the sandbox denied the write.") is None


def test_user_reference_without_decline_is_not_hijacked():
    assert sth.classify("Done. The user request was served by the plan at docs/plans/x.md.") is None


def test_halt_reason_is_named():
    assert sth.HIJACK_HALT_REASON == "stage-task-hijacked"


def test_js_twin_embeds_shared_pattern_source():
    js = sth.hijack_test_js("reply.text")
    assert sth.HIJACK_SOURCE.replace("/", "\\/") in js
    assert js.startswith("(/") and "/i.test(" in js
    assert "reply.text" in js


def test_pattern_source_has_no_python_only_syntax():
    for token in ("(?i)", "(?P<", "(?<=", "(?<!"):
        assert token not in sth.HIJACK_SOURCE

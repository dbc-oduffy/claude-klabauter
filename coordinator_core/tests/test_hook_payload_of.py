"""coordinator_core/tests/test_hook_payload_of.py — `_hook_envelope.payload_of`,
the normaliser between the two params shapes a `hooks.*` handler receives.

Why this exists rather than living inside one handler's test file: the shape gap
is not a property of any single op. Both engine doors send
`{"payload": <event>}` and the cold DoE guard chain sends the event flat, and a
handler that assumes one fail-OPENS through the other — reading no `tool_input`,
matching nothing, and returning exactly the no-op envelope a clean pass returns.
That was measured on `hooks.preuse_bash_dispatch` (deny and allow byte-identical
through the doors) and reproduced on `hooks.block_worktree_tool`.
"""

from __future__ import annotations

import pytest

from coordinator_core._hook_envelope import payload_of


_EVENT = {
    "hook_event_name": "PreToolUse",
    "tool_name": "Bash",
    "tool_input": {"command": "git worktree add /tmp/x"},
    "cwd": "/tmp",
    "session_id": "s",
}


def test_the_wrapped_shape_yields_the_event():
    assert payload_of({"payload": _EVENT}) == _EVENT


def test_the_flat_shape_yields_itself():
    assert payload_of(_EVENT) == _EVENT


def test_the_two_shapes_agree():
    """The property that matters. A handler reading through this helper cannot
    behave differently on one door than the other, which is the whole defect."""
    assert payload_of({"payload": _EVENT}) == payload_of(_EVENT)


def test_a_real_event_is_never_mistaken_for_a_wrapper():
    """Discrimination is by the `payload` key alone, so it is only sound while a
    real hook event carries none. Pinned against the builder that makes them."""
    from coordinator_core.warm.hook_http import payload_from_event

    assert "payload" not in payload_from_event(_EVENT)


@pytest.mark.parametrize("bad", [None, "", [], 0, "a string", ("t",)])
def test_a_non_dict_is_an_empty_payload_not_a_raise(bad):
    """Callers read fields straight off the result. Raising here would convert a
    malformed event into a crashed guard, which the hook contract answers with
    fail-open anyway — so return the shape that fails open honestly."""
    assert payload_of(bad) == {}


def test_a_non_dict_payload_value_is_neither_shape_and_is_not_guessed():
    """No producer sends `{"payload": <non-dict>}`, and a real event has no
    `payload` key, so the outer dict is not treated as the event either."""
    assert payload_of({"payload": "not-a-dict", "tool_name": "Bash"}) == {}


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
    assert payload_of({"payload": _EVENT}) == payload_of(_EVENT)


def test_a_real_event_is_never_mistaken_for_a_wrapper():
    from coordinator_core.warm.hook_http import payload_from_event

    assert "payload" not in payload_from_event(_EVENT)


@pytest.mark.parametrize("bad", [None, "", [], 0, "a string", ("t",)])
def test_a_non_dict_is_an_empty_payload_not_a_raise(bad):
    assert payload_of(bad) == {}


def test_a_non_dict_payload_value_is_neither_shape_and_is_not_guessed():
    assert payload_of({"payload": "not-a-dict", "tool_name": "Bash"}) == {}

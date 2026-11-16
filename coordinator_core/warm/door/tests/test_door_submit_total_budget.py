"""The submit op's door legs all charge one total budget below the harness hook timeout, in both door twins.

Source-level: door.c cannot be built or run off Windows, and the stall this bounds needs a legs-sum that no
single-leg stub reproduces. The Python twin constant is pinned in hooks/tests/test_submit_engine_ask_door_deadline.py.
"""

from __future__ import annotations

import re

import pytest

from coordinator_core.hooks import submit_engine_ask as hook
from coordinator_core.warm.tests.door_test_support import DOOR_POSIX_C, DOOR_WINDOWS_C

_TWINS = pytest.mark.parametrize("source", [DOOR_WINDOWS_C, DOOR_POSIX_C], ids=lambda p: p.name)

#: Door start to its answer on stdout beyond the budgeted legs: process start, spool write, emit.
_ANSWER_MARGIN_S = 1.0


def _text(source) -> str:
    return source.read_text(encoding="utf-8")


def _function(text: str, signature: str) -> str:
    start = text.index(signature)
    return text[start: text.index("\n}\n", start)]


def _define(text: str, macro: str) -> int:
    match = re.search(rf"^#define\s+{macro}\s+(\d+)\s*$", text, re.MULTILINE)
    assert match, macro
    return int(match.group(1))


@_TWINS
def test_the_total_budget_covers_the_read_leg_and_leaves_the_answer_margin_under_the_harness_timeout(source):
    text = _text(source)
    total_s = _define(text, "DOOR_HOOK_SUBMIT_TOTAL_MS") / 1000
    assert _define(text, "DOOR_HOOK_SUBMIT_READ_DEADLINE_MS") / 1000 < total_s
    assert total_s + _ANSWER_MARGIN_S <= hook._CALLER_BUDGET_S


def test_both_twins_carry_the_same_total_budget():
    values = {_define(_text(s), "DOOR_HOOK_SUBMIT_TOTAL_MS") for s in (DOOR_WINDOWS_C, DOOR_POSIX_C)}
    assert len(values) == 1


@_TWINS
def test_the_total_clamp_binds_the_submit_op_alone(source):
    text = _text(source)
    left = _function(text, "static DWORD door_hook_total_left_ms(void) {" if source is DOOR_WINDOWS_C
                     else "static int door_hook_total_left_ms(void) {")
    assert "DOOR_SUBMIT_ASK_OP" in left and "g_door_hook_mode" in left
    assert "DOOR_HOOK_SUBMIT_TOTAL_MS" in left


@_TWINS
def test_marker_drain_write_and_read_each_charge_the_total_budget(source):
    text = _text(source)
    drain = _function(text, "static int door_drain_publish_hold(")
    write = _function(text, "static int write_frame_bounded(")
    assert "door_hook_clamp_to_total(DOOR_DRAIN_WAIT_MS)" in drain
    assert "door_hook_clamp_to_total(remaining_ms(started, DOOR_WRITE_DEADLINE_MS))" in write
    if source is DOOR_WINDOWS_C:
        assert "door_hook_clamp_to_total(remaining_ms(read_started, read_total_ms))" in text
    else:
        read = _function(text, "static int read_hook_line(")
        assert read.count("door_hook_clamp_to_total(") == 2


@_TWINS
def test_a_spent_total_budget_is_named_in_the_fail_open_detail(source):
    text = _text(source)
    assert "no response within the %ums submit budget" in text
    assert "no response within %ums of delivery" in text


@_TWINS
def test_the_unspooled_submit_answer_names_the_state_warp_remedy_before_resubmitting(source):
    text = _text(source)
    block = text[text.index("#define DOOR_SUBMIT_UNSPOOLED_ADVISORY"):]
    block = block[: block.index("\n\n")]
    literal = "".join(re.findall(r'"((?:[^"\\]|\\.)*)"', block))
    assert "list recent runs under state/warp, then re-submit if none" in literal
    assert "command allowed" not in literal

"""Both door twins read a hook answer through door_core's shared probe-then-extend plan, never a flat 500ms read.

Source-level: door.c cannot be built or run off Windows. The POSIX twin's behaviour is driven end to end in
test_hook_live_engine_slow_guard_gets_verdict.py.
"""

from __future__ import annotations

import re

import pytest

from coordinator_core.warm.tests.door_test_support import DOOR_POSIX_C, DOOR_WINDOWS_C

_TWINS = pytest.mark.parametrize("source", [DOOR_WINDOWS_C, DOOR_POSIX_C], ids=lambda p: p.name)


def _text(source) -> str:
    return source.read_text(encoding="utf-8")


def _define(text: str, macro: str) -> int:
    match = re.search(rf"^#define\s+{macro}\s+(\d+)\s*$", text, re.MULTILINE)
    assert match, macro
    return int(match.group(1))


@_TWINS
def test_the_hook_read_site_asks_the_shared_plan_for_its_first_bound_and_its_extension(source):
    text = _text(source)
    assert "door_hook_first_read_ms(g_hook_op)" in text
    assert "door_hook_read_plan(g_hook_op," in text


@_TWINS
def test_no_read_is_given_the_flat_probe_macro_as_its_bound(source):
    text = _text(source)
    assert not re.search(r"(read_line_bounded|remaining_ms)\([^;]*DOOR_HOOK_READ_DEADLINE_MS", text)
    assert "DOOR_READ_DEADLINE_FOR_MODE_MS" not in text


@_TWINS
def test_the_twin_macros_equal_the_shared_header_bounds(source):
    header = (DOOR_POSIX_C.parent / "door_core.h").read_text(encoding="utf-8")
    text = _text(source)
    assert _define(text, "DOOR_HOOK_READ_DEADLINE_MS") == _define(header, "DOOR_HOOK_PROBE_MS")
    assert _define(text, "DOOR_HOOK_SUBMIT_READ_DEADLINE_MS") == _define(header, "DOOR_HOOK_LIVE_READ_MS")


def test_the_windows_twin_probes_the_pipe_and_the_server_process():
    text = _text(DOOR_WINDOWS_C)
    assert "PeekNamedPipe(" in text and "GetNamedPipeServerProcessId(" in text
    assert "hook_pipe_open(pipe)" in text and "hook_engine_alive(pipe)" in text


def test_the_posix_twin_probes_the_socket_and_the_peer_process():
    text = _text(DOOR_POSIX_C)
    assert "MSG_PEEK" in text and "kill(peer, 0)" in text
    assert "hook_peer_open(fd)" in text and "hook_engine_alive(fd)" in text

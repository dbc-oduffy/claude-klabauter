"""Both door twins give a non-hook dead-engine cold leg the same mechanism and the same named budget.

Source-level: door.c cannot be built or run off Windows. The mechanism is the RESPAWN ON MISS non-hook arm
setting COORDINATOR_WARM_BOOT_WAIT_SECS=0 (caller-set value wins) for the cold child; hook mode is untouched.
"""

from __future__ import annotations

import re

import pytest

from coordinator_core.warm.tests.door_test_support import DOOR_POSIX_C, DOOR_WINDOWS_C

_TWINS = pytest.mark.parametrize("source", [DOOR_WINDOWS_C, DOOR_POSIX_C], ids=lambda p: p.name)
_SUFFIX = {DOOR_WINDOWS_C.name: "_W", DOOR_POSIX_C.name: ""}
_QUOTE = {DOOR_WINDOWS_C.name: 'L"', DOOR_POSIX_C.name: '"'}


def _text(source) -> str:
    return source.read_text(encoding="utf-8")


def _define_value(text: str, macro: str) -> str:
    match = re.search(rf'^#define\s+{macro}\s+L?"([^"]*)"\s*$', text, re.MULTILINE)
    assert match, macro
    return match.group(1)


def _non_hook_arm(source) -> str:
    """The RESPAWN ON MISS spawn call through the end of its non-hook env block."""
    text = _text(source)
    start = text.index("door_maybe_spawn_server(engine_root", text.index("RESPAWN ON MISS"))
    end = text.index("fall_through", start)
    return text[start:end]


@_TWINS
def test_the_budget_constants_are_named_beside_the_drain_wait(source):
    text = _text(source)
    suffix = _SUFFIX[source.name]
    assert _define_value(text, f"DOOR_COLD_BOOT_WAIT_ENV{suffix}") == "COORDINATOR_WARM_BOOT_WAIT_SECS"
    assert _define_value(text, f"DOOR_COLD_BOOT_WAIT_SECS{suffix}") == "0"
    assert text.index("#define DOOR_DRAIN_WAIT_MS") < text.index(f"#define DOOR_COLD_BOOT_WAIT_ENV{suffix}")


def test_the_twins_hand_the_cold_child_the_same_env_name_and_value():
    posix, windows = _text(DOOR_POSIX_C), _text(DOOR_WINDOWS_C)
    assert _define_value(posix, "DOOR_COLD_BOOT_WAIT_ENV") == _define_value(windows, "DOOR_COLD_BOOT_WAIT_ENV_W")
    assert _define_value(posix, "DOOR_COLD_BOOT_WAIT_SECS") == _define_value(windows, "DOOR_COLD_BOOT_WAIT_SECS_W")


@_TWINS
def test_the_non_hook_arm_sets_the_budget_only_outside_hook_mode(source):
    arm = _non_hook_arm(source)
    suffix = _SUFFIX[source.name]
    assert "!g_door_hook_mode" in arm
    assert f"DOOR_COLD_BOOT_WAIT_ENV{suffix}" in arm and f"DOOR_COLD_BOOT_WAIT_SECS{suffix}" in arm
    assert arm.index("door_maybe_spawn_server(") < arm.index("!g_door_hook_mode")


def test_a_caller_set_value_wins_in_both_twins():
    assert re.search(r"setenv\(DOOR_COLD_BOOT_WAIT_ENV,\s*DOOR_COLD_BOOT_WAIT_SECS,\s*0\)", _non_hook_arm(DOOR_POSIX_C))
    windows = _non_hook_arm(DOOR_WINDOWS_C)
    assert "ERROR_ENVVAR_NOT_FOUND" in windows
    assert windows.index("GetEnvironmentVariableW(") < windows.index("SetEnvironmentVariableW(")


@_TWINS
def test_the_budget_is_set_in_exactly_one_place(source):
    text = _text(source)
    suffix = _SUFFIX[source.name]
    assert len(re.findall(rf"(?:setenv|SetEnvironmentVariableW)\(\s*DOOR_COLD_BOOT_WAIT_ENV{suffix}\b", text)) == 1


@_TWINS
def test_the_spawn_trigger_set_is_unchanged(source):
    text = _text(source)
    if source is DOOR_POSIX_C:
        assert "if (g_connect_no_server && chosen_dir[0] != '\\0')" in text
    else:
        assert "if (pipe_err == ERROR_FILE_NOT_FOUND)" in text

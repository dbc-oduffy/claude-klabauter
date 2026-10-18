"""Regression for check_trailing_background_ampersand."""

import time

import pytest

from coordinator_core.bash_guards.guard_trailing_background_ampersand import (
    OVERRIDE_ENV,
    check_trailing_background_ampersand as check,
)


@pytest.mark.parametrize("cmd", ["sleep 5 &", "python x.py &", "a && b &", "sleep 5 & \n"])
def test_fires_on_job_ampersand(cmd):
    out = check(cmd, "s", {"tool_input": {"command": cmd}})
    assert out is not None
    ctx = out["hookSpecificOutput"]["additionalContext"]
    assert "run_in_background" in ctx


@pytest.mark.parametrize(
    "cmd",
    [
        "a && b",
        "cmd 2>&1",
        "cmd &> f",
        "cmd >&2",
        "echo 'a & b'",
        "echo 'a &'",
        'echo "a &"',
        "cat <<'EOF'\nfoo &\nEOF",
        "cat <<EOF\nfoo &\nEOF\necho done",
        "a &&",
        "a & b",
    ],
)
def test_silent_on_controls(cmd):
    assert check(cmd, "s", {"tool_input": {"command": cmd}}) is None


def test_silent_when_run_in_background():
    p = {"tool_input": {"command": "sleep 5 &", "run_in_background": True}}
    assert check("sleep 5 &", "s", p) is None


def test_override_env():
    assert check("sleep 5 &", "s", {"env": {OVERRIDE_ENV: "1"}}) is None


def test_under_one_ms():
    t = time.process_time_ns()
    for _ in range(100):
        check("a && b &", "s", {})
    assert (time.process_time_ns() - t) / 1e9 / 100 < 0.001

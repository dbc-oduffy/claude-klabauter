#!/usr/bin/env python3
"""tests/test_check_multi_event_hook_hardcoded_event.py — Tests for
bin/check-multi-event-hook-hardcoded-event.py.

Purpose: Verifies the guard flags a multi-event-registered hook script that
hardcodes a literal `hookEventName` (exit 1), passes a multi-event script
that echoes a variable (exit 0), passes a single-event script that
hardcodes (legitimate — exit 0), does not crash on a registered-but-missing
script (skip/warn, exit 0), and passes the live repo's real hooks.json
(regression lock proving the guard is clean against the fixed tree).

Uses synthetic fixture directories (own hooks.json + own script files) for
every case except the live-tree regression lock — never depends on the
live repo's real hooks.json contents drifting.

Spec backlink: origin incident 2026-07-20, runtime-tripwire-em-check.py
hardcoded "hookEventName": "PostToolUse" while registered on Stop,
UserPromptSubmit, PostToolUse:Agent.

Run: python3 -m pytest <settings-home>/coordinator/bin/tests/test_check_multi_event_hook_hardcoded_event.py
"""

import json
import os
import subprocess
import sys
import tempfile

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
GUARD = os.path.normpath(os.path.join(_HERE, "..", "check-multi-event-hook-hardcoded-event.py"))
LIVE_HOOKS_JSON = os.path.normpath(os.path.join(_HERE, "..", "..", "hooks", "hooks.json"))


def _run_guard(*args, cwd=None):
    proc = subprocess.run(
        [sys.executable, GUARD, *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=cwd,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return proc.returncode, proc.stdout, proc.stderr


def _write(path, content):
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)
    return path


def _make_fixture(tmp, hooks_config, scripts):
    """hooks_config: dict for the "hooks" top-level key of hooks.json.
    scripts: {basename: source_text} written under <tmp>/scripts/.
    Returns path to the fixture hooks.json.
    """
    scripts_dir = os.path.join(tmp, "scripts")
    os.makedirs(scripts_dir, exist_ok=True)
    for name, text in scripts.items():
        _write(os.path.join(scripts_dir, name), text)
    hooks_json_path = os.path.join(tmp, "hooks.json")
    _write(hooks_json_path, json.dumps({"hooks": hooks_config}, indent=2))
    return hooks_json_path


def _command_for(script_name):
    return "python3 ${{CLAUDE_PLUGIN_ROOT}}/hooks/scripts/{}".format(script_name)


# ---------------------------------------------------------------------------
# Test 1: multi-event script with hardcoded literal -> exit 1, violation reported
# ---------------------------------------------------------------------------

def test_multi_event_hardcoded_literal_exits_nonzero():
    with tempfile.TemporaryDirectory() as tmp:
        script_src = (
            "def emit():\n"
            '    out = {"hookSpecificOutput": {"hookEventName": "PostToolUse"}}\n'
        )
        hooks_json = _make_fixture(
            tmp,
            {
                "Stop": [{"matcher": "", "hooks": [{"type": "command", "command": _command_for("bad.py")}]}],
                "UserPromptSubmit": [{"matcher": "", "hooks": [{"type": "command", "command": _command_for("bad.py")}]}],
            },
            {"bad.py": script_src},
        )
        rc, out, err = _run_guard("--hooks-json", hooks_json)
        assert rc != 0 and "bad.py" in err and "hookEventName" in err, (
            "rc={} out={} err={}".format(rc, out, err)
        )


# ---------------------------------------------------------------------------
# Test 2: multi-event script echoing a variable -> exit 0
# ---------------------------------------------------------------------------

def test_multi_event_variable_echo_exits_zero():
    with tempfile.TemporaryDirectory() as tmp:
        script_src = (
            "def emit(event):\n"
            '    out = {"hookSpecificOutput": {"hookEventName": event}}\n'
        )
        hooks_json = _make_fixture(
            tmp,
            {
                "Stop": [{"matcher": "", "hooks": [{"type": "command", "command": _command_for("good.py")}]}],
                "UserPromptSubmit": [{"matcher": "", "hooks": [{"type": "command", "command": _command_for("good.py")}]}],
            },
            {"good.py": script_src},
        )
        rc, out, err = _run_guard("--hooks-json", hooks_json)
        assert rc == 0, "rc={} out={} err={}".format(rc, out, err)


# ---------------------------------------------------------------------------
# Test 3: single-event script with hardcoded literal -> exit 0 (legitimate)
# ---------------------------------------------------------------------------

def test_single_event_hardcoded_literal_exits_zero():
    with tempfile.TemporaryDirectory() as tmp:
        script_src = (
            "def emit():\n"
            '    out = {"hookSpecificOutput": {"hookEventName": "PreCompact"}}\n'
        )
        hooks_json = _make_fixture(
            tmp,
            {
                "PreCompact": [{"matcher": "", "hooks": [{"type": "command", "command": _command_for("single.py")}]}],
            },
            {"single.py": script_src},
        )
        rc, out, err = _run_guard("--hooks-json", hooks_json)
        assert rc == 0, "rc={} out={} err={}".format(rc, out, err)


# ---------------------------------------------------------------------------
# Test 4: registered script missing from disk -> exit 0 with a skip/warn, no crash
# ---------------------------------------------------------------------------

def test_missing_script_skips_gracefully():
    with tempfile.TemporaryDirectory() as tmp:
        hooks_json = _make_fixture(
            tmp,
            {
                "Stop": [{"matcher": "", "hooks": [{"type": "command", "command": _command_for("ghost.py")}]}],
                "UserPromptSubmit": [{"matcher": "", "hooks": [{"type": "command", "command": _command_for("ghost.py")}]}],
            },
            {},  # ghost.py deliberately not written
        )
        rc, out, err = _run_guard("--hooks-json", hooks_json)
        assert rc == 0 and ("WARN" in err or "not found" in err), (
            "rc={} out={} err={}".format(rc, out, err)
        )


# ---------------------------------------------------------------------------
# Test 5 (regression lock): the real repo's live hooks.json -> exit 0
# ---------------------------------------------------------------------------

def test_live_repo_hooks_json_exits_zero():
    if not os.path.isfile(LIVE_HOOKS_JSON):
        pytest.skip("live hooks.json not found at {} (not in repo checkout?)".format(LIVE_HOOKS_JSON))
    rc, out, err = _run_guard("--hooks-json", LIVE_HOOKS_JSON)
    assert rc == 0, "guard exited {} against live hooks.json\nstdout: {}\nstderr: {}".format(rc, out, err)


# ---------------------------------------------------------------------------
# Test 6: negative control — the exact pre-fix bug shape must be caught
# ---------------------------------------------------------------------------

def test_negative_control_matches_pre_fix_bug_shape():
    with tempfile.TemporaryDirectory() as tmp:
        # Reproduces the pre-2026-07-20 runtime-tripwire-em-check.py bug:
        # hardcoded "hookEventName": "PostToolUse" while registered on
        # Stop, UserPromptSubmit, AND PostToolUse.
        script_src = (
            '"""pre-fix reproduction fixture."""\n'
            "def _emit_advisory(parts):\n"
            "    out = {\n"
            '        "hookSpecificOutput": {\n'
            '            "hookEventName": "PostToolUse",\n'
            '            "additionalContext": "x",\n'
            "        }\n"
            "    }\n"
        )
        hooks_json = _make_fixture(
            tmp,
            {
                "Stop": [{"matcher": "", "hooks": [{"type": "command", "command": _command_for("pre-fix.py")}]}],
                "UserPromptSubmit": [{"matcher": "", "hooks": [{"type": "command", "command": _command_for("pre-fix.py")}]}],
                "PostToolUse": [{"matcher": "Agent", "hooks": [{"type": "command", "command": _command_for("pre-fix.py")}]}],
            },
            {"pre-fix.py": script_src},
        )
        rc, out, err = _run_guard("--hooks-json", hooks_json)
        assert rc != 0 and "pre-fix.py" in err and "3 events" in err, (
            "rc={} out={} err={}".format(rc, out, err)
        )



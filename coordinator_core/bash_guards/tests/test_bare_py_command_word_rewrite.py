"""A bare ``*.py`` command word is rewritten to run under python by the real Bash
dispatch, so it never reaches bash's ENOEXEC fallback (the self-re-invoking
bash chain from running a shebang-less coordinator/bin script directly).
"""
from __future__ import annotations

import json

import pytest

from coordinator_core.bash_guards import _dc_rewrites, dispatch
from coordinator_core.bash_guards._rewrite_support import _bt_python3_invocation

SCRIPT = "C:/dev/claude-klabauter/coordinator/bin/plan-spine-check.py"  # abs-path-ok: synthetic command text under test, never executed


def _dispatch(cmd, tmp_path):
    payload = {
        "tool_name": "Bash",
        "tool_input": {"command": cmd},
        "session_id": "bare-py-probe",
        "cwd": str(tmp_path),
    }
    return dispatch.evaluate_payload_json(json.dumps(payload))


def _updated(out):
    if out is None:
        return None
    return (out.get("hookSpecificOutput") or {}).get("updatedInput", {}).get("command")


def test_dispatch_rewrites_incident_command_to_run_under_python(tmp_path):
    cmd = "%s docs/plans/x.md | tail -5" % SCRIPT
    new = _updated(_dispatch(cmd, tmp_path))
    assert new is not None, "dispatch left the bare script command untouched"
    interp = _bt_python3_invocation()
    assert new.startswith("%s %s docs/plans/x.md" % (interp, SCRIPT))
    assert "| tail -5" in new


@pytest.mark.parametrize(
    "cmd, expected_points",
    [
        ("./tool.py a b", 1),
        ("cd E:/repo && coordinator/bin/x.py --flag", 1),
        ("foo --a | grep m; %s p | tail -5" % SCRIPT, 1),
        ("FOO=1 BAR=2 bin/x.py", 1),
        ('"C:/dev/my dir/x.py" arg', 1),
        ("a.py && b.py; c.py | d.py", 4),
        ("if true; then bin/x.py; fi", 1),
        ("echo $(bin/x.py)", 1),
    ],
)
def test_command_positions_are_found(cmd, expected_points):
    assert len(_dc_rewrites._bare_py_insert_points(cmd)) == expected_points


@pytest.mark.parametrize(
    "cmd",
    [
        "python bin/x.py",
        "python3 %s a" % SCRIPT,
        "py -3 bin/x.py",
        "env FOO=1 bin/x.py",
        "sudo bin/x.py",
        "nohup bin/x.py",
        "git add bin/x.py",
        "echo bin/x.py",
        "cat x.py > out.py",
        "ls 2> err.py",
        "# bin/x.py",
        "echo 'a; b.py'",
        "cat <<'EOF'\nbin/x.py\nEOF",
        "pytest tests/test_x.py",
        "x.pyc",
        "",
    ],
)
def test_interpreter_led_or_non_command_words_are_left_alone(cmd):
    assert _dc_rewrites._bare_py_insert_points(cmd) == []
    assert _dc_rewrites._bare_py_command_word_rewrite(cmd, None) is None


def test_rewrite_only_advises_and_never_denies(tmp_path):
    out = _dispatch("bin/x.py", tmp_path)
    hso = out["hookSpecificOutput"]
    assert hso["permissionDecision"] == "allow"
    assert hso["updatedInput"]["command"].endswith(" bin/x.py")


def test_rewrite_also_applies_when_grep_override_is_set(tmp_path, monkeypatch):
    monkeypatch.setenv("COORDINATOR_ALLOW_GREP_VIA_BASH", "1")
    assert _updated(_dispatch("bin/x.py", tmp_path)) is not None

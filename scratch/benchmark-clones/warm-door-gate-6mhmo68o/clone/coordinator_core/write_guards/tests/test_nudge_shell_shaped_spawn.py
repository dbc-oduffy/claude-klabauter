"""Behavioral tests for coordinator_core.write_guards.nudge_shell_shaped_spawn.

Covers docs/plans/2026-08-06-shell-spawn-regrowth-gate.md § C5:

  1. A shell=True call fires the advisory, with the shlex.split alternative
     leading the message.
  2. A shell-binary argv[0] (bash/-c) fires the advisory, with the direct-argv
     alternative leading the message.
  3. A plain, non-shell-shaped subprocess call does not fire.
  4. Non-.py files never fire, regardless of content.
  5. Envelope shape is ADVISORY (additionalContext), never permissionDecision.
  6. Message order: alternative first, carve-out doc second, "Found" last;
     never opens with "You cannot".
  7. Fail-open: unparseable Python source does not raise.
  8. Whole-file Edit reconstruction sees a shell-shaped call introduced by
     the edit even when the fragment alone wouldn't show enough context.
  9. Oversized content (over the shared byte cap) does not fire.

Grep anchors: SHELL-SPAWN-REGROWTH-GATE
"""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.write_guards import nudge_shell_shaped_spawn as guard


_SHELL_TRUE_CALL = (
    "import subprocess\n"
    "def f():\n"
    "    subprocess.run('echo hi', shell=True)\n"
)

_SHELL_BINARY_CALL = (
    "import subprocess\n"
    "def f():\n"
    "    subprocess.run(['bash', '-c', 'echo hi'])\n"
)

_PLAIN_SPAWN_CALL = (
    "import subprocess\n"
    "def f():\n"
    "    subprocess.run(['ls', '-la'])\n"
)


def _payload(tool_name, tool_input):
    return {"tool_name": tool_name, "tool_input": tool_input}


def _is_advisory_envelope(result: dict) -> bool:
    hso = result.get("hookSpecificOutput", {})
    return (
        hso.get("hookEventName") == "PreToolUse"
        and "additionalContext" in hso
        and "permissionDecision" not in hso
    )


def test_shell_true_fires_with_shlex_alternative():
    result = guard.check(
        _payload("Write", {"file_path": "/repo/mod.py", "content": _SHELL_TRUE_CALL})
    )
    assert result is not None
    assert _is_advisory_envelope(result)
    text = result["hookSpecificOutput"]["additionalContext"]
    assert "shlex.split" in text


def test_shell_binary_fires_with_direct_argv_alternative():
    result = guard.check(
        _payload("Write", {"file_path": "/repo/mod.py", "content": _SHELL_BINARY_CALL})
    )
    assert result is not None
    text = result["hookSpecificOutput"]["additionalContext"]
    assert "argv[0]" in text


def test_plain_spawn_does_not_fire():
    result = guard.check(
        _payload("Write", {"file_path": "/repo/mod.py", "content": _PLAIN_SPAWN_CALL})
    )
    assert result is None


def test_non_py_file_never_fires():
    result = guard.check(
        _payload("Write", {"file_path": "/repo/mod.sh", "content": _SHELL_TRUE_CALL})
    )
    assert result is None


def test_message_order_leads_with_alternative():
    result = guard.check(
        _payload("Write", {"file_path": "/repo/mod.py", "content": _SHELL_TRUE_CALL})
    )
    text = result["hookSpecificOutput"]["additionalContext"]
    assert not text.lower().startswith("you cannot")
    alt_idx = text.index("shlex.split")
    carveout_idx = text.index("docs/reference/shell-out-carve-outs.md")
    found_idx = text.index("Found")
    assert alt_idx < carveout_idx < found_idx


def test_unparseable_source_fails_open():
    result = guard.check(
        _payload("Write", {"file_path": "/repo/mod.py", "content": "def f(:\n    pass\n"})
    )
    assert result is None


def test_edit_reconstructs_whole_file(tmp_path: Path):
    target = tmp_path / "mod.py"
    target.write_text(
        "import subprocess\ndef f():\n    subprocess.run(['ls'])\n",
        encoding="utf-8",
    )
    result = guard.check(
        _payload(
            "Edit",
            {
                "file_path": str(target),
                "old_string": "subprocess.run(['ls'])",
                "new_string": "subprocess.run('ls', shell=True)",
            },
        )
    )
    assert result is not None
    text = result["hookSpecificOutput"]["additionalContext"]
    assert "shlex.split" in text


def test_oversized_content_does_not_fire():
    padding = "# x\n" * (guard._MAX_WHOLE_FILE_BYTES // 4 + 10)
    content = _SHELL_TRUE_CALL + padding
    result = guard.check(
        _payload("Write", {"file_path": "/repo/mod.py", "content": content})
    )
    assert result is None


def test_malformed_payload_returns_none():
    assert guard.check({}) is None
    assert guard.check({"tool_name": "Write", "tool_input": "not-a-dict"}) is None

"""Behavioral tests for coordinator_core.write_guards.nudge_unmarked_spawning_test.

Covers docs/plans/2026-08-20-the-spawn-ratchet-stops-accumulating-arrears.md § C4:

  1. An unmarked spawning test function fires the advisory.
  2. A marked spawning test function (per-function decorator) does not fire.
  3. A file-wide `pytestmark = [pytest.mark.spawns_process]` does not fire.
  4. A non-test-tree `.py` file never fires, even with a spawn site.
  5. A non-`.py` file never fires.
  6. Envelope shape is ADVISORY (additionalContext), never permissionDecision.
  7. Fail-open: unparseable Python source does not raise.
  8. The guard is registered and actually FIRES through the engine's public
     `evaluate_payload_json` entry point (not merely registered/inert).

Grep anchors: THE-SPAWN-RATCHET-STOPS-ACCUMULATING-ARREARS, C4
"""

from __future__ import annotations

import json

import pytest

from coordinator_core.write_guards import engine as write_guards_engine
from coordinator_core.write_guards import nudge_unmarked_spawning_test as guard


_UNMARKED_SPAWN_TEST = (
    "import subprocess\n"
    "def test_thing():\n"
    "    subprocess.run(['git', 'status'])\n"
)

_MARKED_SPAWN_TEST = (
    "import subprocess\n"
    "import pytest\n"
    "@pytest.mark.spawns_process\n"
    "@pytest.mark.cadence\n"
    "def test_thing():\n"
    "    subprocess.run(['git', 'status'])\n"
)

_FILE_WIDE_MARKED_SPAWN_TEST = (
    "import subprocess\n"
    "import pytest\n"
    "pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]\n"
    "def test_thing():\n"
    "    subprocess.run(['git', 'status'])\n"
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


def test_unmarked_spawning_test_fires():
    result = guard.check(
        _payload(
            "Write",
            {"file_path": "/repo/coordinator_core/tests/test_thing.py", "content": _UNMARKED_SPAWN_TEST},
        )
    )
    assert result is not None
    assert _is_advisory_envelope(result)
    text = result["hookSpecificOutput"]["additionalContext"]
    assert "test_thing" in text
    assert "spawns_process" in text


def test_marked_spawning_test_does_not_fire():
    result = guard.check(
        _payload(
            "Write",
            {"file_path": "/repo/coordinator_core/tests/test_thing.py", "content": _MARKED_SPAWN_TEST},
        )
    )
    assert result is None


def test_file_wide_pytestmark_does_not_fire():
    result = guard.check(
        _payload(
            "Write",
            {
                "file_path": "/repo/coordinator_core/tests/test_thing.py",
                "content": _FILE_WIDE_MARKED_SPAWN_TEST,
            },
        )
    )
    assert result is None


def test_non_test_tree_py_file_never_fires():
    result = guard.check(
        _payload(
            "Write",
            {"file_path": "/repo/coordinator_core/spawn_policy/detect.py", "content": _UNMARKED_SPAWN_TEST},
        )
    )
    assert result is None


def test_non_py_file_never_fires():
    result = guard.check(
        _payload("Write", {"file_path": "/repo/test_thing.sh", "content": _UNMARKED_SPAWN_TEST})
    )
    assert result is None


def test_unparseable_source_fails_open():
    result = guard.check(
        _payload(
            "Write",
            {"file_path": "/repo/coordinator_core/tests/test_thing.py", "content": "def f(:\n    pass\n"},
        )
    )
    assert result is None


def test_malformed_payload_returns_none():
    assert guard.check({}) is None
    assert guard.check({"tool_name": "Write", "tool_input": "not-a-dict"}) is None


def test_guard_fires_through_engine_evaluate_payload_json():
    """AC5/AC6: proves this guard is not merely registered but actually
    reachable and firing through the production PreToolUse entry point --
    a registered guard can still be inert if its params are not threaded
    (staff-eng F5 caveat)."""
    names, import_failed = write_guards_engine.discover_guard_names()
    assert not import_failed, f"write_guards import failure(s): {import_failed}"
    assert "nudge_unmarked_spawning_test" in names

    payload_text = json.dumps(
        _payload(
            "Write",
            {"file_path": "/repo/coordinator_core/tests/test_thing.py", "content": _UNMARKED_SPAWN_TEST},
        )
    )
    skipped: list[str] = []
    result = write_guards_engine.evaluate_payload_json(payload_text, skipped_out=skipped)
    assert not skipped, f"write_guards module(s) failed to import: {skipped}"
    assert result is not None
    assert _is_advisory_envelope(result)
    text = result["hookSpecificOutput"]["additionalContext"]
    assert "test_thing" in text

    # Control: a marked file, same seam, must not fire.
    control_payload_text = json.dumps(
        _payload(
            "Write",
            {"file_path": "/repo/coordinator_core/tests/test_thing.py", "content": _MARKED_SPAWN_TEST},
        )
    )
    control_result = write_guards_engine.evaluate(json.loads(control_payload_text))
    assert control_result is None

"""Behavioral tests for coordinator_core.write_guards.block_tracker_edit --
the generated handoff-tracker advisory guard (see the module's own docstring
for the design decision this is the discharge of).

Mirrors the structure of test_block_priority_ledger_edit.py, a sibling
path-tail-regex advisory guard: no repo fixture is needed since the match is
a pure path-tail regex, independent of cwd/git-root resolution.

CLASS is "advisory" per DR-277 (2026-08-06 guard-class census); was
"hard-deny" at PRIORITY 60, re-slotted to 113 in the advisory phase. This
guard had zero regression coverage at HEAD -- no test file existed for it at
all (2026-08-06 guard-class census review, C4). <!-- Review:
coordinator:code-reviewer -- test_block_tracker_edit.py is absent on disk
while every sibling flip in the slice has one; add coverage for the
advisory envelope shape, the reworded "OFFER:" prefix, and the removed
`permissionDecision` key. -->

Spec backlink: docs/plans/2026-08-06-apply-guard-class-census.md, chunk C4.
"""

from __future__ import annotations

import pytest

from coordinator_core.write_guards import block_tracker_edit as guard


_OVERRIDE_ENV = "COORDINATOR_OVERRIDE_TRACKER_EDIT"


@pytest.fixture(autouse=True)
def _clear_override_env(monkeypatch):
    monkeypatch.delenv(_OVERRIDE_ENV, raising=False)


class TestAdvisesTrackerEdit:
    def test_write_to_handoff_tracker_advised(self):
        payload = {
            "tool_name": "Write",
            "tool_input": {
                "file_path": "state/handoff-tracker.md",
                "content": "hand-edited\n",
            },
        }

        result = guard.check(payload)

        assert result is not None
        assert "permissionDecision" not in result["hookSpecificOutput"]
        reason = result["hookSpecificOutput"]["additionalContext"]
        assert reason.startswith("OFFER:")
        assert "render-handoff-tracker.py" in reason

    def test_write_to_doe_handoff_tracker_advised(self):
        payload = {
            "tool_name": "Write",
            "tool_input": {
                "file_path": "state/doe-handoff-tracker.md",
                "content": "hand-edited\n",
            },
        }

        result = guard.check(payload)

        assert result is not None
        assert "permissionDecision" not in result["hookSpecificOutput"]

    def test_edit_to_tracker_advised(self):
        payload = {
            "tool_name": "Edit",
            "tool_input": {
                "file_path": "state/handoff-tracker.md",
                "old_string": "a",
                "new_string": "b",
            },
        }

        result = guard.check(payload)

        assert result is not None
        assert "permissionDecision" not in result["hookSpecificOutput"]

    def test_multiedit_to_tracker_advised(self):
        payload = {
            "tool_name": "MultiEdit",
            "tool_input": {
                "file_path": "state/handoff-tracker.md",
                "edits": [{"old_string": "a", "new_string": "b"}],
            },
        }

        result = guard.check(payload)

        assert result is not None
        assert "permissionDecision" not in result["hookSpecificOutput"]

    def test_notebookedit_to_tracker_advised(self):
        payload = {
            "tool_name": "NotebookEdit",
            "tool_input": {
                "notebook_path": "state/handoff-tracker.md",
            },
        }

        result = guard.check(payload)

        assert result is not None
        assert "permissionDecision" not in result["hookSpecificOutput"]

    def test_absolute_path_advised(self):
        payload = {
            "tool_name": "Write",
            "tool_input": {
                "file_path": "/Users/someone/X/claude-klabauter/state/handoff-tracker.md",
                "content": "hand-edited\n",
            },
        }

        result = guard.check(payload)

        assert result is not None
        assert "permissionDecision" not in result["hookSpecificOutput"]

    def test_backslash_path_advised(self):
        """Windows-style separators normalize before the tail match."""
        payload = {
            "tool_name": "Write",
            "tool_input": {
                "file_path": r"C:\Users\someone\X\claude-klabauter\state\handoff-tracker.md",
                "content": "hand-edited\n",
            },
        }

        result = guard.check(payload)

        assert result is not None
        assert "permissionDecision" not in result["hookSpecificOutput"]


class TestPassThrough:
    def test_tasks_dir_passes_through(self):
        """Reference-hook parity: does NOT match under tasks/ -- pre-split
        relic, deliberately not honored (see module docstring)."""
        payload = {
            "tool_name": "Write",
            "tool_input": {
                "file_path": "tasks/handoff-tracker.md",
                "content": "hand-edited\n",
            },
        }

        assert guard.check(payload) is None

    def test_unrelated_file_passes_through(self):
        payload = {
            "tool_name": "Edit",
            "tool_input": {
                "file_path": "state/handoff-tracker-notes.md",
                "old_string": "a",
                "new_string": "b",
            },
        }

        assert guard.check(payload) is None

    def test_non_matching_tool_passes_through(self):
        payload = {
            "tool_name": "Read",
            "tool_input": {"file_path": "state/handoff-tracker.md"},
        }

        assert guard.check(payload) is None

    def test_override_env_set_passes_through(self, monkeypatch):
        monkeypatch.setenv(_OVERRIDE_ENV, "1")
        payload = {
            "tool_name": "Write",
            "tool_input": {
                "file_path": "state/handoff-tracker.md",
                "content": "hand-edited\n",
            },
        }

        assert guard.check(payload) is None

    def test_missing_file_path_passes_through(self):
        payload = {"tool_name": "Write", "tool_input": {}}

        assert guard.check(payload) is None

    def test_malformed_payload_fails_open(self):
        payload = {"tool_name": "Write", "tool_input": "not-a-dict"}

        assert guard.check(payload) is None

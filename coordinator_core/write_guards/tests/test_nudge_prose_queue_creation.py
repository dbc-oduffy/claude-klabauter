"""Behavioral tests for coordinator_core.write_guards.nudge_prose_queue_creation
-- the DR-115 part 3 creation-advisory guard (CLASS flipped from hard-deny
to advisory by DR-277; detection and PRIORITY unchanged).

Covers: fires on a genuinely new prose queue file across all three families,
Windows-separator and case-variation robustness, silence when the target
already exists, silence under archive/, silence on non-queue-shaped and
content-free writes, and the operator override.

Spec: coordinator-claude docs/decisions/DR-115-queue-shape-is-a-scope-collision-not-a-staleness.md
Spec: docs/decisions/DR-277-guards-are-advisory-by-default-two-named.md
"""

from __future__ import annotations

import os

import pytest

from coordinator_core.write_guards import nudge_prose_queue_creation as guard


def _payload(tool_name, tool_input, cwd=None):
    payload = {"tool_name": tool_name, "tool_input": tool_input}
    if cwd is not None:
        payload["cwd"] = cwd
    return payload


def _deny_reason(result: dict) -> str:
    hso = result["hookSpecificOutput"]
    assert hso["hookEventName"] == "PreToolUse"
    assert "permissionDecision" not in hso
    assert "additionalContext" in hso
    return hso["additionalContext"]


@pytest.fixture(autouse=True)
def _clear_override_env():
    os.environ.pop(guard._OVERRIDE_ENV_VAR, None)
    yield
    os.environ.pop(guard._OVERRIDE_ENV_VAR, None)


class TestCreationDenyFires:
    @pytest.mark.parametrize(
        "basename,schema",
        [
            ("improvement-queue.md", "improvement-queue"),
            ("bug-backlog.md", "bug-backlog"),
            ("debt-backlog.md", "debt-backlog"),
        ],
    )
    def test_fires_on_new_file_all_three_families(self, tmp_path, basename, schema):
        target = tmp_path / "state" / basename
        result = guard.check(
            _payload(
                "Write",
                {
                    "file_path": str(target),
                    "content": "- 2026-07-31 | new entry | details",
                },
            )
        )
        reason = _deny_reason(result)
        assert f"--schema {schema}" in reason
        assert f"state/{schema}/" in reason

    def test_fires_on_windows_separators(self, tmp_path):
        target = str(tmp_path) + "\\state\\bug-backlog.md"
        result = guard.check(
            _payload(
                "Write",
                {
                    "file_path": target,
                    "content": "- 2026-07-31 | new entry | details",
                },
            )
        )
        _deny_reason(result)

    def test_fires_on_case_variation(self, tmp_path):
        target = tmp_path / "State" / "Bug-Backlog.md"
        result = guard.check(
            _payload(
                "Write",
                {
                    "file_path": str(target),
                    "content": "- 2026-07-31 | new entry | details",
                },
            )
        )
        _deny_reason(result)

    @pytest.mark.parametrize(
        "basename",
        [
            "bug-queue.md",
            "debt-queue.md",
            "improvement-backlog.md",
        ],
    )
    def test_fires_on_generic_family_variant(self, tmp_path, basename):
        # Generic regex covers the OTHER queue/backlog suffix for each of
        # the three known families -- not any *-queue.md/*-backlog.md.
        target = tmp_path / "state" / basename
        result = guard.check(
            _payload(
                "Write",
                {
                    "file_path": str(target),
                    "content": "- 2026-07-31 | new entry | details",
                },
            )
        )
        _deny_reason(result)


class TestCreationDenySilent:
    def test_silent_when_target_already_exists(self, tmp_path):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        target = state_dir / "improvement-queue.md"
        target.write_text("- 2026-07-01 | existing | entry\n")
        result = guard.check(
            _payload(
                "Write",
                {
                    "file_path": str(target),
                    "content": "- 2026-07-01 | existing | entry\n- 2026-07-31 | new | entry",
                },
            )
        )
        assert result is None

    def test_silent_under_archive(self, tmp_path):
        target = tmp_path / "archive" / "improvement-queue" / "2026-07" / "improvement-queue.md"
        result = guard.check(
            _payload(
                "Write",
                {
                    "file_path": str(target),
                    "content": "- 2026-07-31 | new entry | details",
                },
            )
        )
        assert result is None

    def test_silent_on_non_write_tool(self, tmp_path):
        target = tmp_path / "state" / "bug-backlog.md"
        result = guard.check(
            _payload(
                "Edit",
                {
                    "file_path": str(target),
                    "old_string": "a",
                    "new_string": "- 2026-07-31 | new entry | details",
                },
            )
        )
        assert result is None

    def test_silent_on_non_queue_shaped_basename(self, tmp_path):
        target = tmp_path / "state" / "README.md"
        result = guard.check(
            _payload(
                "Write",
                {
                    "file_path": str(target),
                    "content": "- 2026-07-31 | not a queue row shape technically | but wrong dir",
                },
            )
        )
        assert result is None

    def test_silent_on_unrelated_generic_backlog_name(self, tmp_path):
        # The false-positive class code-reviewer flagged: an unrelated
        # -backlog.md/-queue.md file outside the three named families must
        # stay silent, even with a dated-pipe-row line present.
        target = tmp_path / "state" / "release-backlog.md"
        result = guard.check(
            _payload(
                "Write",
                {
                    "file_path": str(target),
                    "content": "- 2026-07-31 | shipped X | details",
                },
            )
        )
        assert result is None

    def test_silent_when_no_dated_row_present(self, tmp_path):
        target = tmp_path / "state" / "improvement-queue.md"
        result = guard.check(
            _payload(
                "Write",
                {
                    "file_path": str(target),
                    "content": "This file has been retired. See structured queue instead.",
                },
            )
        )
        assert result is None

    def test_silent_when_override_env_set(self, tmp_path):
        os.environ[guard._OVERRIDE_ENV_VAR] = "1"
        target = tmp_path / "state" / "improvement-queue.md"
        result = guard.check(
            _payload(
                "Write",
                {
                    "file_path": str(target),
                    "content": "- 2026-07-31 | new entry | details",
                },
            )
        )
        assert result is None


class TestFailOpen:
    def test_non_dict_payload_shape_returns_none(self):
        assert guard.check({}) is None

    def test_missing_file_path_returns_none(self):
        assert guard.check(_payload("Write", {"content": "- 2026-07-31 | x | y"})) is None

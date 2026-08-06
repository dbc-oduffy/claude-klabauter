"""Behavioral tests for coordinator_core.write_guards.nudge_prose_queue_append
-- the DR-115 § PM direction (A) append-advisory guard.

Covers: fires when a dated row is genuinely added to an existing prose
queue (Write/Edit/MultiEdit); stays silent on a prune, a pure reformat, a
closure deletion, an Edit touching non-row prose, any YAML-directory write,
and a nonexistent target (the creation-deny sibling's job); the punt
override.

Spec: example-doctrine-repo docs/decisions/DR-115-queue-shape-is-a-scope-collision-not-a-staleness.md
"""

from __future__ import annotations

import os

import pytest

from coordinator_core.write_guards import nudge_prose_queue_append as guard


def _payload(tool_name, tool_input, cwd=None):
    payload = {"tool_name": tool_name, "tool_input": tool_input}
    if cwd is not None:
        payload["cwd"] = cwd
    return payload


def _advisory_text(result: dict) -> str:
    hso = result["hookSpecificOutput"]
    assert hso["hookEventName"] == "PreToolUse"
    assert "permissionDecision" not in hso
    assert "additionalContext" in hso
    return hso["additionalContext"]


@pytest.fixture(autouse=True)
def _clear_punt_env():
    os.environ.pop(guard._ESCAPE_HATCH_ENV_VAR, None)
    yield
    os.environ.pop(guard._ESCAPE_HATCH_ENV_VAR, None)


def _make_queue(tmp_path, basename, body):
    state_dir = tmp_path / "state"
    state_dir.mkdir(exist_ok=True)
    target = state_dir / basename
    target.write_text(body)
    return target


class TestAppendAdvisoryFires:
    def test_fires_on_write_adding_a_dated_row(self, tmp_path):
        target = _make_queue(tmp_path, "bug-backlog.md", "- 2026-07-01 | old | entry\n")
        result = guard.check(
            _payload(
                "Write",
                {
                    "file_path": str(target),
                    "content": "- 2026-07-01 | old | entry\n- 2026-07-31 | new | entry\n",
                },
            )
        )
        text = _advisory_text(result)
        assert "migrate_prose_queue.py" in text
        assert "--schema bug-backlog" in text

    def test_fires_on_edit_adding_a_dated_row(self, tmp_path):
        target = _make_queue(tmp_path, "debt-backlog.md", "- 2026-07-01 | old | entry\n")
        result = guard.check(
            _payload(
                "Edit",
                {
                    "file_path": str(target),
                    "old_string": "- 2026-07-01 | old | entry\n",
                    "new_string": "- 2026-07-01 | old | entry\n- 2026-07-31 | new | entry\n",
                },
            )
        )
        _advisory_text(result)

    def test_fires_on_multiedit_adding_a_dated_row(self, tmp_path):
        target = _make_queue(tmp_path, "improvement-queue.md", "- 2026-07-01 | old | entry\n")
        result = guard.check(
            _payload(
                "MultiEdit",
                {
                    "file_path": str(target),
                    "edits": [
                        {
                            "old_string": "- 2026-07-01 | old | entry\n",
                            "new_string": "- 2026-07-01 | old | entry\n- 2026-07-31 | new | entry\n",
                        }
                    ],
                },
            )
        )
        _advisory_text(result)


class TestAppendAdvisorySilent:
    def test_silent_on_prune_row_count_decreases(self, tmp_path):
        target = _make_queue(
            tmp_path,
            "bug-backlog.md",
            "- 2026-07-01 | old | entry\n- 2026-07-02 | second | entry\n",
        )
        result = guard.check(
            _payload(
                "Write",
                {
                    "file_path": str(target),
                    "content": "- 2026-07-01 | old | entry\n",
                },
            )
        )
        assert result is None

    def test_silent_on_pure_reformat_count_unchanged(self, tmp_path):
        target = _make_queue(tmp_path, "bug-backlog.md", "- 2026-07-01 | old | entry\n")
        result = guard.check(
            _payload(
                "Write",
                {
                    "file_path": str(target),
                    "content": "- 2026-07-01 | old   | entry (reformatted)\n",
                },
            )
        )
        assert result is None

    def test_silent_on_closure_deletion(self, tmp_path):
        target = _make_queue(
            tmp_path,
            "debt-backlog.md",
            "- 2026-07-01 | old | entry\n- 2026-07-02 | closed | entry\n",
        )
        result = guard.check(
            _payload(
                "Edit",
                {
                    "file_path": str(target),
                    "old_string": "- 2026-07-02 | closed | entry\n",
                    "new_string": "",
                },
            )
        )
        assert result is None

    def test_silent_on_edit_touching_non_row_prose(self, tmp_path):
        target = _make_queue(
            tmp_path, "bug-backlog.md", "# Bug backlog\n\n- 2026-07-01 | old | entry\n"
        )
        result = guard.check(
            _payload(
                "Edit",
                {
                    "file_path": str(target),
                    "old_string": "# Bug backlog\n",
                    "new_string": "# Bug backlog (updated header)\n",
                },
            )
        )
        assert result is None

    def test_silent_on_yaml_directory_write(self, tmp_path):
        state_dir = tmp_path / "state" / "improvement-queue"
        state_dir.mkdir(parents=True)
        target = state_dir / "2026-07-31-entry.yaml"
        target.write_text("title: existing\n")
        result = guard.check(
            _payload(
                "Write",
                {
                    "file_path": str(target),
                    "content": "title: existing\njustification: updated\n",
                },
            )
        )
        assert result is None

    def test_silent_when_target_does_not_exist(self, tmp_path):
        target = tmp_path / "state" / "bug-backlog.md"
        result = guard.check(
            _payload(
                "Write",
                {
                    "file_path": str(target),
                    "content": "- 2026-07-31 | new | entry",
                },
            )
        )
        assert result is None

    def test_silent_when_punt_env_set_non_trivial(self, tmp_path):
        os.environ[guard._ESCAPE_HATCH_ENV_VAR] = "repo not migrating yet, tracked separately"
        target = _make_queue(tmp_path, "bug-backlog.md", "- 2026-07-01 | old | entry\n")
        result = guard.check(
            _payload(
                "Write",
                {
                    "file_path": str(target),
                    "content": "- 2026-07-01 | old | entry\n- 2026-07-31 | new | entry\n",
                },
            )
        )
        assert result is None


class TestFailOpen:
    def test_non_write_tool_returns_none(self):
        assert guard.check(_payload("Read", {"file_path": "state/bug-backlog.md"})) is None

    def test_missing_file_path_returns_none(self):
        assert guard.check(_payload("Write", {"content": "- 2026-07-31 | x | y"})) is None

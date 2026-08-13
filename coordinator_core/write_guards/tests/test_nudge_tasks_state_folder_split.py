"""Behavioral tests for
coordinator_core.write_guards.nudge_tasks_state_folder_split -- the
load-bearing-surface-under-tasks/ advisory guard.

Spec: coordinator-claude docs/plans/2026-07-27-claude-md-altitude-triage.md § C14

Covers: non-write-tool/non-tasks-path passthrough, literal-basename surface
matches (orientation_cache, lessons), directory-form surface matches
(handoffs/, memos/, review-trail/, week-changelog/, audits/, recovery/,
scratch/, debt-backlog/, bug-backlog/, improvement-queue/), category-word
substring matches (trackers/queues/ledgers via their singular on-disk
basenames), the tasks/<sid>/ per-session mirror exemption, path-segment
anchoring (no bare-substring false positive), backslash-path normalization,
and the module contract (CLASS/PRIORITY/MATCHERS).
"""

from __future__ import annotations

import pytest

from coordinator_core.write_guards import nudge_tasks_state_folder_split as guard


def _payload(tool_name, file_path, **extra):
    tool_input = {"file_path": file_path}
    tool_input.update(extra)
    return {"tool_name": tool_name, "tool_input": tool_input}


def _advisory_text(result: dict) -> str:
    hso = result["hookSpecificOutput"]
    assert hso["hookEventName"] == "PreToolUse"
    assert "permissionDecision" not in hso
    assert "additionalContext" in hso
    return hso["additionalContext"]


class TestGateOnToolAndPath:
    @pytest.mark.parametrize(
        "tool_name,file_path",
        [
            ("Read", "tasks/lessons.md"),
            ("Write", "state/lessons.md"),
            ("Write", ""),
            ("Write", "tasks/2026-07-27-adhoc-scratch-notes.md"),
        ],
        ids=[
            "non_write_tool",
            "already_under_state",
            "file_path_empty",
            "genuine_tasks_ephemera",
        ],
    )
    def test_passes_through(self, tool_name, file_path):
        assert guard.check(_payload(tool_name, file_path)) is None

    def test_tool_input_not_dict_passes_through(self):
        assert guard.check({"tool_name": "Write", "tool_input": "not-a-dict"}) is None


class TestLiteralBasenameSurfaceMatches:
    def test_orientation_cache_write_advises(self):
        result = guard.check(_payload("Write", "tasks/orientation_cache.md"))
        text = _advisory_text(result)
        assert "state/orientation_cache" in text

    def test_lessons_write_advises(self):
        result = guard.check(_payload("Write", "tasks/lessons.md"))
        text = _advisory_text(result)
        assert "state/lessons" in text

    def test_lessons_edit_advises(self):
        result = guard.check(
            _payload(
                "Edit",
                "tasks/lessons.md",
                old_string="x",
                new_string="y",
            )
        )
        _advisory_text(result)


class TestDirectoryFormSurfaceMatches:
    @pytest.mark.parametrize(
        "file_path,surface",
        [
            ("tasks/handoffs/2026-07-27_foo.md", "handoffs"),
            ("tasks/memos/some-memo.md", "memos"),
            ("tasks/review-trail/entry.md", "review-trail"),
            ("tasks/week-changelog/2026-07-27.md", "week-changelog"),
            ("tasks/audits/2026-07-27-audit.md", "audits"),
            ("tasks/recovery/notes.md", "recovery"),
            ("tasks/scratch/deep-architecture-survey/x.md", "scratch"),
            ("tasks/debt-backlog/item.yaml", "debt-backlog"),
            ("tasks/bug-backlog/item.yaml", "bug-backlog"),
            ("tasks/improvement-queue/item.yaml", "improvement-queue"),
        ],
    )
    def test_directory_form_surface_advises(self, file_path, surface):
        result = guard.check(_payload("Write", file_path))
        text = _advisory_text(result)
        assert f"state/{surface}" in text


class TestCategoryWordSubstringMatches:
    def test_tracker_basename_advises(self):
        result = guard.check(_payload("Write", "tasks/handoff-tracker.md"))
        text = _advisory_text(result)
        assert "state/trackers" in text

    def test_ledger_basename_advises(self):
        result = guard.check(_payload("Write", "tasks/health-ledger.md"))
        text = _advisory_text(result)
        assert "state/ledgers" in text

    def test_queue_basename_outside_dir_form_advises(self):
        result = guard.check(_payload("Write", "tasks/some-custom-queue.md"))
        text = _advisory_text(result)
        assert "state/queues" in text


class TestSessionIdMirrorExemption:
    def test_uuid_shaped_first_segment_exempt(self):
        assert (
            guard.check(
                _payload(
                    "Write",
                    "tasks/4d1871b0-a05a-4603-93ab-3c5114e4cccd/completeness-checklist.md",
                )
            )
            is None
        )

    def test_short_hex_session_id_exempt(self):
        assert guard.check(_payload("Write", "tasks/abc12345/notes.md")) is None


class TestPathSegmentAnchoring:
    def test_substring_coincidence_does_not_match(self):
        """A path containing the literal substring 'tasks/' where the
        directory is not actually named 'tasks' must not match (mirrors
        nudge_baton_body_bar's anchoring discipline)."""
        assert guard.check(_payload("Write", "vendor/mytasks/lessons.md")) is None

    def test_nested_tasks_prefix_still_anchors(self):
        result = guard.check(_payload("Write", "/repo/tasks/lessons.md"))
        _advisory_text(result)

    def test_non_surface_file_under_tasks_passes_through(self):
        assert guard.check(_payload("Write", "tasks/some-uuid-dir/scratch.md")) is None


class TestBackslashPathNormalization:
    def test_backslash_path_still_matched(self):
        result = guard.check(_payload("Write", "tasks\\lessons.md"))
        _advisory_text(result)


class TestModuleContract:
    def test_class_is_advisory(self):
        assert guard.CLASS == "advisory"

    def test_priority_and_matchers(self):
        assert guard.PRIORITY == 140
        assert guard.MATCHERS == ["Write", "Edit", "MultiEdit", "NotebookEdit"]

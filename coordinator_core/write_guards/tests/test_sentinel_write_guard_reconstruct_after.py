"""Behavioral tests for
coordinator_core.write_guards._sentinel_write_guard.reconstruct_after --
the consolidated home for an idiom six example-doctrine-repo guards each hand-copied a
local ``_reconstruct_after`` for (DR-047: claude-klabauter owns guard logic, example-doctrine-repo owns
plumbing).

Spec backlink: dispatch brief "Add shared reconstruct_after to
_sentinel_write_guard.py" (2026-08-13), consolidating
guard-oss-payload-locality.py, guard-prompt-surface-citations.py,
guard-doctrine-changelog-prose.py, guard-test-tree-git-fixture-spawn.py,
nudge-plan-test-surface-tier.py, guard-python-syntax-on-write.py.
"""

from __future__ import annotations

from coordinator_core.write_guards import _sentinel_write_guard as helper


class TestReconstructAfterWrite:
    def test_write_uses_content_verbatim(self):
        assert (
            helper.reconstruct_after("Write", {"content": "new file body"}, "old body")
            == "new file body"
        )

    def test_write_non_string_content_returns_none(self):
        assert helper.reconstruct_after("Write", {"content": ["not", "a", "string"]}, "before") is None

    def test_write_missing_content_returns_none(self):
        assert helper.reconstruct_after("Write", {}, "before") is None


class TestReconstructAfterEdit:
    def test_single_replace_when_replace_all_false(self):
        before = "foo bar foo"
        result = helper.reconstruct_after(
            "Edit",
            {"old_string": "foo", "new_string": "baz", "replace_all": False},
            before,
        )
        assert result == "baz bar foo"

    def test_all_occurrences_replaced_when_replace_all_true(self):
        before = "foo bar foo"
        result = helper.reconstruct_after(
            "Edit",
            {"old_string": "foo", "new_string": "baz", "replace_all": True},
            before,
        )
        assert result == "baz bar baz"

    def test_replace_all_absent_defaults_to_single_replace(self):
        before = "foo bar foo"
        result = helper.reconstruct_after(
            "Edit", {"old_string": "foo", "new_string": "baz"}, before
        )
        assert result == "baz bar foo"

    def test_empty_old_string_replaces_whole_text(self):
        result = helper.reconstruct_after(
            "Edit", {"old_string": "", "new_string": "seeded content"}, "anything"
        )
        assert result == "seeded content"

    def test_absent_old_string_returns_none(self):
        result = helper.reconstruct_after(
            "Edit", {"old_string": "not-present", "new_string": "x"}, "before text"
        )
        assert result is None

    def test_non_string_old_string_returns_none(self):
        result = helper.reconstruct_after(
            "Edit", {"old_string": 123, "new_string": "x"}, "before"
        )
        assert result is None

    def test_non_string_new_string_returns_none(self):
        result = helper.reconstruct_after(
            "Edit", {"old_string": "before", "new_string": None}, "before"
        )
        assert result is None

    def test_non_dict_tool_input_returns_none(self):
        assert helper.reconstruct_after("Edit", "not-a-dict", "before") is None  # type: ignore[arg-type]


class TestReconstructAfterMultiEdit:
    def test_ordered_replay_across_edits(self):
        before = "one two three"
        edits = [
            {"old_string": "one", "new_string": "1"},
            {"old_string": "two", "new_string": "2"},
            {"old_string": "three", "new_string": "3"},
        ]
        result = helper.reconstruct_after("MultiEdit", {"edits": edits}, before)
        assert result == "1 2 3"

    def test_each_edit_honours_its_own_replace_all(self):
        before = "a a b b"
        edits = [
            {"old_string": "a", "new_string": "x", "replace_all": True},
            {"old_string": "b", "new_string": "y", "replace_all": False},
        ]
        result = helper.reconstruct_after("MultiEdit", {"edits": edits}, before)
        assert result == "x x y b"

    def test_later_edit_absent_old_string_returns_none(self):
        before = "one two"
        edits = [
            {"old_string": "one", "new_string": "1"},
            {"old_string": "not-present-after-first-edit", "new_string": "z"},
        ]
        assert helper.reconstruct_after("MultiEdit", {"edits": edits}, before) is None

    def test_empty_old_string_mid_sequence_replaces_running_text(self):
        before = "start"
        edits = [
            {"old_string": "", "new_string": "seeded"},
            {"old_string": "seeded", "new_string": "final"},
        ]
        assert helper.reconstruct_after("MultiEdit", {"edits": edits}, before) == "final"

    def test_non_list_edits_returns_none(self):
        assert helper.reconstruct_after("MultiEdit", {"edits": "not-a-list"}, "before") is None

    def test_non_dict_edit_entry_returns_none(self):
        edits = ["not-a-dict"]
        assert helper.reconstruct_after("MultiEdit", {"edits": edits}, "before") is None

    def test_missing_edits_key_returns_none(self):
        assert helper.reconstruct_after("MultiEdit", {}, "before") is None

    def test_non_dict_tool_input_returns_none(self):
        assert helper.reconstruct_after("MultiEdit", "not-a-dict", "before") is None  # type: ignore[arg-type]


class TestReconstructAfterUnknownOrMalformed:
    def test_unknown_tool_name_returns_none(self):
        assert helper.reconstruct_after("NotebookEdit", {"content": "x"}, "before") is None

    def test_empty_tool_name_returns_none(self):
        assert helper.reconstruct_after("", {"content": "x"}, "before") is None

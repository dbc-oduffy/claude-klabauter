"""Behavioral tests for
coordinator_core.write_guards.block_duplicate_decision_record_id -- the
hard-deny backstop for a hand-authored `docs/decisions/*.md` record
claiming an `id:` a sibling already holds. See the guard module's own
docstring for the incident this discharges.

Uses real files under `tmp_path` (the guard reads sibling `.md` files off
disk to detect a collision) rather than a payload-only fixture, unlike
`block_priority_ledger_edit`'s pure-regex shape.
"""

from __future__ import annotations

from pathlib import Path

from coordinator_core.write_guards import block_duplicate_decision_record_id as guard


def _decisions_dir(tmp_path: Path) -> Path:
    d = tmp_path / "docs" / "decisions"
    d.mkdir(parents=True)
    return d


class TestWriteNewFile:
    def test_new_record_with_fresh_id_is_allowed(self, tmp_path: Path) -> None:
        decisions = _decisions_dir(tmp_path)
        (decisions / "DR-1-first.md").write_bytes(b"---\nid: DR-1\n---\n")

        payload = {
            "tool_name": "Write",
            "tool_input": {
                "file_path": str(decisions / "DR-2-second.md"),
                "content": "---\nid: DR-2\n---\n\n# Second\n",
            },
        }
        assert guard.check(payload) is None

    def test_new_record_reusing_an_existing_id_is_denied(self, tmp_path: Path) -> None:
        decisions = _decisions_dir(tmp_path)
        (decisions / "DR-352-form-4.md").write_bytes(
            b"---\nid: DR-352\ntitle: Form 4\n---\n"
        )

        payload = {
            "tool_name": "Write",
            "tool_input": {
                "file_path": str(decisions / "DR-352-shipped-in-stamping.md"),
                "content": "---\nid: DR-352\ntitle: shipped_in stamping\n---\n",
            },
        }
        result = guard.check(payload)
        assert result is not None
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
        reason = result["hookSpecificOutput"]["permissionDecisionReason"]
        assert "DR-352" in reason
        assert "DR-352-form-4.md" in reason

    def test_no_id_in_content_is_allowed(self, tmp_path: Path) -> None:
        decisions = _decisions_dir(tmp_path)
        (decisions / "DR-1-first.md").write_bytes(b"---\nid: DR-1\n---\n")

        payload = {
            "tool_name": "Write",
            "tool_input": {
                "file_path": str(decisions / "DR-2-draft.md"),
                "content": "no frontmatter here at all\n",
            },
        }
        assert guard.check(payload) is None


class TestEditExistingFile:
    def test_edit_that_does_not_touch_id_is_allowed(self, tmp_path: Path) -> None:
        decisions = _decisions_dir(tmp_path)
        target = decisions / "DR-1-first.md"
        target.write_bytes(b"---\nid: DR-1\ntitle: Old title\n---\n\nbody\n")
        (decisions / "DR-2-second.md").write_bytes(b"---\nid: DR-2\n---\n")

        payload = {
            "tool_name": "Edit",
            "tool_input": {
                "file_path": str(target),
                "old_string": "body",
                "new_string": "revised body",
            },
        }
        assert guard.check(payload) is None

    def test_edit_that_retargets_id_onto_a_taken_number_is_denied(self, tmp_path: Path) -> None:
        decisions = _decisions_dir(tmp_path)
        target = decisions / "DR-1-first.md"
        target.write_bytes(b"---\nid: DR-1\n---\n\nbody\n")
        (decisions / "DR-2-second.md").write_bytes(b"---\nid: DR-2\n---\n")

        payload = {
            "tool_name": "Edit",
            "tool_input": {
                "file_path": str(target),
                "old_string": "id: DR-1",
                "new_string": "id: DR-2",
            },
        }
        result = guard.check(payload)
        assert result is not None
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"

    def test_edit_leaving_own_id_unchanged_never_collides_with_self(self, tmp_path: Path) -> None:
        decisions = _decisions_dir(tmp_path)
        target = decisions / "DR-1-first.md"
        target.write_bytes(b"---\nid: DR-1\ntitle: Old\n---\n\nbody\n")

        payload = {
            "tool_name": "Edit",
            "tool_input": {
                "file_path": str(target),
                "old_string": "title: Old",
                "new_string": "title: New",
            },
        }
        assert guard.check(payload) is None


class TestMultiEdit:
    def test_multi_edit_retargeting_id_onto_taken_number_is_denied(self, tmp_path: Path) -> None:
        decisions = _decisions_dir(tmp_path)
        target = decisions / "DR-1-first.md"
        target.write_bytes(b"---\nid: DR-1\n---\n\nbody\n")
        (decisions / "DR-9-ninth.md").write_bytes(b"---\nid: DR-9\n---\n")

        payload = {
            "tool_name": "MultiEdit",
            "tool_input": {
                "file_path": str(target),
                "edits": [
                    {"old_string": "id: DR-1", "new_string": "id: DR-9"},
                    {"old_string": "body", "new_string": "revised"},
                ],
            },
        }
        result = guard.check(payload)
        assert result is not None
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"


class TestScopeAndMatchers:
    def test_path_outside_docs_decisions_is_ignored(self, tmp_path: Path) -> None:
        other = tmp_path / "docs" / "plans"
        other.mkdir(parents=True)
        (other / "DR-1-not-a-decision.md").write_bytes(b"---\nid: DR-1\n---\n")

        payload = {
            "tool_name": "Write",
            "tool_input": {
                "file_path": str(other / "DR-2-also-not.md"),
                "content": "---\nid: DR-1\n---\n",
            },
        }
        assert guard.check(payload) is None

    def test_unmatched_tool_name_is_ignored(self, tmp_path: Path) -> None:
        decisions = _decisions_dir(tmp_path)
        (decisions / "DR-1-first.md").write_bytes(b"---\nid: DR-1\n---\n")

        payload = {
            "tool_name": "NotebookEdit",
            "tool_input": {
                "file_path": str(decisions / "DR-2.md"),
                "content": "---\nid: DR-1\n---\n",
            },
        }
        assert guard.check(payload) is None

    def test_malformed_payload_fails_open(self) -> None:
        assert guard.check({}) is None
        assert guard.check({"tool_name": "Write"}) is None
        assert guard.check({"tool_name": "Write", "tool_input": "not-a-dict"}) is None

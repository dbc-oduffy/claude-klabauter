"""Behavioral tests for
coordinator_core.write_guards.nudge_outbox_draft_frontmatter_shape.

Covers: fires (advisory, never deny) on a state/memo-outbox/<topic>.md draft
whose frontmatter would fail cross-repo-memo send's validation — including
the incident's own status: open case — across Write/Edit/MultiEdit; stays
silent on a valid draft, on state/memo-outbox/sent/<topic>.md (archived,
already-delivered), on a non-outbox path, and on a buffer with no
frontmatter block yet.

Spec backlink: docs/plans/2026-08-07-cross-repo-memo-outbox-frontmatter-shape.md [DEAD-CITATION: plan file never committed to this repo]
"""

from __future__ import annotations

import pytest

from coordinator_core.write_guards import nudge_outbox_draft_frontmatter_shape as guard

_VALID_DRAFT = (
    "---\n"
    "title: \"a memo\"\n"
    "from: \"claude-klabauter-em\"\n"
    "to: \"some-em\"\n"
    "created: 2026-08-07\n"
    "status: draft\n"
    "delivery_mode: receiver-repo\n"
    "summary: \"a summary\"\n"
    "---\n"
    "body\n"
)

_OPEN_STATUS_DRAFT = _VALID_DRAFT.replace("status: draft", "status: open")


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


def _outbox_dir(tmp_path):
    d = tmp_path / "state" / "memo-outbox"
    d.mkdir(parents=True)
    return d


class TestFires:
    def test_fires_on_write_with_status_open(self, tmp_path):
        d = _outbox_dir(tmp_path)
        target = d / "some-topic.md"
        result = guard.check(
            _payload("Write", {"file_path": str(target), "content": _OPEN_STATUS_DRAFT})
        )
        text = _advisory_text(result)
        assert "status must be 'draft'" in text

    def test_fires_on_edit_flipping_status_to_open(self, tmp_path):
        d = _outbox_dir(tmp_path)
        target = d / "some-topic.md"
        target.write_text(_VALID_DRAFT)
        result = guard.check(
            _payload(
                "Edit",
                {
                    "file_path": str(target),
                    "old_string": "status: draft",
                    "new_string": "status: open",
                },
            )
        )
        text = _advisory_text(result)
        assert "status must be 'draft'" in text

    def test_fires_on_multiedit_flipping_status_to_open(self, tmp_path):
        d = _outbox_dir(tmp_path)
        target = d / "some-topic.md"
        target.write_text(_VALID_DRAFT)
        result = guard.check(
            _payload(
                "MultiEdit",
                {
                    "file_path": str(target),
                    "edits": [
                        {"old_string": "status: draft", "new_string": "status: open"},
                    ],
                },
            )
        )
        text = _advisory_text(result)
        assert "status must be 'draft'" in text

    def test_fires_on_missing_required_field(self, tmp_path):
        d = _outbox_dir(tmp_path)
        target = d / "some-topic.md"
        broken = _VALID_DRAFT.replace('title: "a memo"\n', "")
        result = guard.check(
            _payload("Write", {"file_path": str(target), "content": broken})
        )
        text = _advisory_text(result)
        assert "title" in text


class TestSilent:
    def test_silent_on_valid_draft(self, tmp_path):
        d = _outbox_dir(tmp_path)
        target = d / "some-topic.md"
        result = guard.check(
            _payload("Write", {"file_path": str(target), "content": _VALID_DRAFT})
        )
        assert result is None

    def test_silent_on_sent_archive_copy(self, tmp_path):
        sent_dir = tmp_path / "state" / "memo-outbox" / "sent"
        sent_dir.mkdir(parents=True)
        target = sent_dir / "some-topic.md"
        # Archived copy carries status: sent — would fail validation if the
        # path gate didn't exclude it first.
        content = _VALID_DRAFT.replace("status: draft", "status: sent")
        result = guard.check(
            _payload("Write", {"file_path": str(target), "content": content})
        )
        assert result is None

    def test_silent_on_non_outbox_path(self, tmp_path):
        d = tmp_path / "cross-repo" / "inbox"
        d.mkdir(parents=True)
        target = d / "2026-08-07-some-topic.md"
        result = guard.check(
            _payload("Write", {"file_path": str(target), "content": _OPEN_STATUS_DRAFT})
        )
        assert result is None

    def test_silent_when_no_frontmatter_block_yet(self, tmp_path):
        d = _outbox_dir(tmp_path)
        target = d / "some-topic.md"
        result = guard.check(
            _payload("Write", {"file_path": str(target), "content": "not yet frontmatter\n"})
        )
        assert result is None


class TestFailOpen:
    def test_non_write_tool_returns_none(self):
        assert (
            guard.check(_payload("Read", {"file_path": "state/memo-outbox/x.md"})) is None
        )

    def test_missing_file_path_returns_none(self):
        assert guard.check(_payload("Write", {"content": _OPEN_STATUS_DRAFT})) is None

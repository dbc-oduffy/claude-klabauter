"""Behavioral tests for coordinator_core.write_guards.nudge_baton_body_bar
-- the bare-row-list baton-body advisory guard.

Spec: docs/plans/2026-07-23-queue-triage-terminus-ops.md § C9

Covers: non-write-tool/non-baton-path passthrough, archived-baton passthrough,
Write-with-bare-row-list-body advisory, Write-with-authored-prose passthrough,
Edit whole-file reconstruction (bare-row-list post-edit fires; a body with
prose anywhere does not), MultiEdit reconstruction, and the
COORDINATOR_BATON_BODY_PUNT escape hatch (non-trivial suppresses, trivial
denies-with-hint... i.e. still advises, with the trivial hint appended).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from coordinator_core.write_guards import nudge_baton_body_bar as guard


def _payload(tool_name, tool_input):
    return {"tool_name": tool_name, "tool_input": tool_input}


def _advisory_text(result: dict) -> str:
    hso = result["hookSpecificOutput"]
    assert hso["hookEventName"] == "PreToolUse"
    assert "permissionDecision" not in hso
    assert "additionalContext" in hso
    return hso["additionalContext"]


_FM = "---\ntitle: test\nstatus: open\n---\n"

_BARE_ROWS_BODY = (
    "- 2026-07-23 | did thing one\n"
    "- 2026-07-23 | did thing two\n"
    "- 2026-07-23 | did thing three\n"
)

_TABLE_ONLY_BODY = (
    "| date | note |\n"
    "| --- | --- |\n"
    "| 2026-07-23 | one |\n"
    "| 2026-07-23 | two |\n"
    "| 2026-07-23 | three |\n"
)

_AUTHORED_BODY = (
    "## Goal\n\n"
    "This session picked up the queue-triage work and finished C1-C8.\n\n"
    "- 2026-07-23 | did thing one\n"
    "- 2026-07-23 | did thing two\n"
    "- 2026-07-23 | did thing three\n\n"
    "Next step: land C9 and hand off.\n"
)

# Review: code-reviewer — Finding 3 (P2): a narrative-bullet-only body (every
# line syntactically matches _BULLET_RE, but each bullet is a full sentence
# with real reasoning) must NOT be classified as a bare row-list.
_NARRATIVE_BULLETS_BODY = (
    "- Decided to defer the migration because the schema change would break "
    "the existing readers until the new format lands.\n"
    "- Blocked on the upstream API; filed a ticket and pinged the owning team "
    "for an ETA before resuming this thread.\n"
    "- Next step is to re-run the full suite once the pinned dependency "
    "bump lands in the sibling repo.\n"
)


@pytest.fixture(autouse=True)
def _clear_punt_env():
    os.environ.pop(guard._ESCAPE_HATCH_ENV_VAR, None)
    yield
    os.environ.pop(guard._ESCAPE_HATCH_ENV_VAR, None)


class TestGateOnToolAndPath:
    @pytest.mark.parametrize(
        "tool_name,tool_input",
        [
            ("Read", {"file_path": "state/handoffs/foo.md"}),
            ("Write", {"file_path": "state/improvement-queue/foo.yaml", "content": "x"}),
            ("Write", "not-a-dict"),
            ("Write", {"file_path": ""}),
            ("Write", {"file_path": "archive/handoffs/2026-07/foo.md", "content": _FM + _BARE_ROWS_BODY}),
        ],
        ids=[
            "non_write_tool",
            "non_baton_path",
            "tool_input_not_dict",
            "file_path_empty",
            "archived_baton_path",
        ],
    )
    def test_passes_through(self, tool_name, tool_input):
        assert guard.check(_payload(tool_name, tool_input)) is None


class TestWriteBareRowListAdvises:
    def test_bullet_list_only_advises(self):
        result = guard.check(
            _payload(
                "Write",
                {"file_path": "state/handoffs/2026-07-23_foo.md", "content": _FM + _BARE_ROWS_BODY},
            )
        )
        text = _advisory_text(result)
        assert "bare rows" in text
        assert "next step" in text.lower()

    def test_table_only_advises(self):
        result = guard.check(
            _payload(
                "Write",
                {"file_path": "state/handoffs/2026-07-23_bar.md", "content": _FM + _TABLE_ONLY_BODY},
            )
        )
        _advisory_text(result)

    def test_empty_body_no_advise(self):
        result = guard.check(
            _payload(
                "Write",
                {"file_path": "state/handoffs/2026-07-23_empty.md", "content": _FM},
            )
        )
        assert result is None

    def test_too_few_rows_no_advise(self):
        body = "- one row only\n"
        result = guard.check(
            _payload(
                "Write",
                {"file_path": "state/handoffs/2026-07-23_short.md", "content": _FM + body},
            )
        )
        assert result is None


class TestAuthoredBodyPassesThrough:
    def test_prose_plus_rows_no_advise(self):
        result = guard.check(
            _payload(
                "Write",
                {"file_path": "state/handoffs/2026-07-23_ok.md", "content": _FM + _AUTHORED_BODY},
            )
        )
        assert result is None

    def test_narrative_bullets_only_no_advise(self):
        """Review: code-reviewer — Finding 3 (P2): bullets that are full
        sentences with real reasoning must not false-positive as a bare
        row-list, even though they syntactically match _BULLET_RE."""
        result = guard.check(
            _payload(
                "Write",
                {
                    "file_path": "state/handoffs/2026-07-23_narrative.md",
                    "content": _FM + _NARRATIVE_BULLETS_BODY,
                },
            )
        )
        assert result is None


class TestPathGateAnchoring:
    def test_substring_coincidence_does_not_match(self):
        """Review: code-reviewer — Finding 4 (P2): fnmatch's `*` crosses path
        separators, so the unanchored glob previously matched any path
        containing the literal substring `state/handoffs/` — including an
        unrelated directory like `vendor/upstate/handoffs/`."""
        result = guard.check(
            _payload(
                "Write",
                {
                    "file_path": "/repo/vendor/upstate/handoffs/notes.md",
                    "content": _FM + _BARE_ROWS_BODY,
                },
            )
        )
        assert result is None


class TestEditReconstruction:
    def test_edit_reconstructs_bare_row_list(self, tmp_path: Path):
        target = tmp_path / "2026-07-23_edit.md"
        target.write_text(_FM + "- 2026-07-23 | did thing one\n- 2026-07-23 | did thing two\n")
        result = guard.check(
            _payload(
                "Edit",
                {
                    "file_path": str(target),
                    "old_string": "- 2026-07-23 | did thing two\n",
                    "new_string": "- 2026-07-23 | did thing two\n- 2026-07-23 | did thing three\n",
                },
            )
        )
        # file_path here is an absolute tmp path, not under state/handoffs/ —
        # the path gate must reject it (defends against false positives on
        # any file that merely happens to reconstruct bare-row-list content).
        assert result is None

    def test_edit_under_state_handoffs_reconstructs_and_advises(self, tmp_path: Path, monkeypatch):
        handoffs_dir = tmp_path / "state" / "handoffs"
        handoffs_dir.mkdir(parents=True)
        target = handoffs_dir / "2026-07-23_edit.md"
        target.write_text(_FM + "- 2026-07-23 | did thing one\n- 2026-07-23 | did thing two\n")
        result = guard.check(
            _payload(
                "Edit",
                {
                    "file_path": str(target),
                    "old_string": "- 2026-07-23 | did thing two\n",
                    "new_string": "- 2026-07-23 | did thing two\n- 2026-07-23 | did thing three\n",
                },
            )
        )
        _advisory_text(result)

    def test_edit_read_failure_no_advise(self):
        result = guard.check(
            _payload(
                "Edit",
                {
                    "file_path": "state/handoffs/does-not-exist-on-disk.md",
                    "old_string": "x",
                    "new_string": "y",
                },
            )
        )
        assert result is None

    def test_edit_prose_no_advise(self, tmp_path: Path):
        handoffs_dir = tmp_path / "state" / "handoffs"
        handoffs_dir.mkdir(parents=True)
        target = handoffs_dir / "2026-07-23_prose.md"
        target.write_text(_FM + _AUTHORED_BODY)
        result = guard.check(
            _payload(
                "Edit",
                {
                    "file_path": str(target),
                    "old_string": "Next step: land C9 and hand off.\n",
                    "new_string": "Next step: land C9, hand off, and celebrate.\n",
                },
            )
        )
        assert result is None


class TestMultiEditReconstruction:
    def test_multiedit_reconstructs_bare_row_list(self, tmp_path: Path):
        handoffs_dir = tmp_path / "state" / "handoffs"
        handoffs_dir.mkdir(parents=True)
        target = handoffs_dir / "2026-07-23_multi.md"
        target.write_text(_FM + "- row one\n- row two\n")
        result = guard.check(
            _payload(
                "MultiEdit",
                {
                    "file_path": str(target),
                    "edits": [
                        {"old_string": "- row one\n", "new_string": "- row one\n- row zero\n"},
                        {"old_string": "- row two\n", "new_string": "- row two\n- row three\n"},
                    ],
                },
            )
        )
        _advisory_text(result)

    def test_multiedit_bad_edit_no_advise(self, tmp_path: Path):
        handoffs_dir = tmp_path / "state" / "handoffs"
        handoffs_dir.mkdir(parents=True)
        target = handoffs_dir / "2026-07-23_multibad.md"
        target.write_text(_FM + "- row one\n- row two\n- row three\n")
        result = guard.check(
            _payload(
                "MultiEdit",
                {
                    "file_path": str(target),
                    "edits": [
                        {"old_string": "not-present-anywhere", "new_string": "x"},
                    ],
                },
            )
        )
        assert result is None


class TestPuntEscapeHatch:
    def test_non_trivial_reason_suppresses(self):
        os.environ[guard._ESCAPE_HATCH_ENV_VAR] = "pure data table, context lives in the plan"
        result = guard.check(
            _payload(
                "Write",
                {"file_path": "state/handoffs/2026-07-23_punt.md", "content": _FM + _BARE_ROWS_BODY},
            )
        )
        assert result is None

    def test_trivial_reason_still_advises_with_hint(self):
        os.environ[guard._ESCAPE_HATCH_ENV_VAR] = "ok"
        result = guard.check(
            _payload(
                "Write",
                {"file_path": "state/handoffs/2026-07-23_punt2.md", "content": _FM + _BARE_ROWS_BODY},
            )
        )
        text = _advisory_text(result)
        assert "trivial" in text.lower() or guard._ESCAPE_HATCH_ENV_VAR in text


class TestModuleContract:
    def test_class_is_advisory(self):
        assert guard.CLASS == "advisory"

    def test_priority_and_matchers(self):
        assert guard.PRIORITY == 130
        assert guard.MATCHERS == ["Write", "Edit", "MultiEdit"]

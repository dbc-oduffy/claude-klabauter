"""Behavioral tests for coordinator_core.write_guards.nudge_improvement_queue_write
-- the improvement-queue five-question friction guard, post-DR-077-terms twin
retirement.

Migrated from the retired coordinator_core/hooks/nudge_improvement_queue_write.py
DARK twin's test_hooks_roundtrip.py coverage (8 tests), per the retirement
authorized in
cross-repo/inbox/2026-07-22-claude-central-em-retire-improvement-queue-twin-authorized.md:
the hooks/ copy was registered but never wired to any live dispatch path
(no hooks.json PreToolUse entry anywhere); coordinator_core/write_guards/
nudge_improvement_queue_write.py, auto-discovered by write_guards/engine.py, is
the sole live surface. Every behavioral assertion from the 8 originals is
preserved here against the live `check()` entry point.

Covers: non-write-tool passthrough, non-queue-path passthrough, YAML/legacy-md
Write denial, YAML Edit passthrough (field-level edits are lower-friction),
MultiEdit-as-Write denial, and the COORDINATOR_QUEUE_PUNT escape hatch (both
non-trivial-suppresses and trivial-denies-with-hint).

Grep anchors: IMPROVEMENT-QUEUE-WRITE DR-077
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from coordinator_core.write_guards import nudge_improvement_queue_write as guard


def _payload(tool_name, tool_input):
    return {"tool_name": tool_name, "tool_input": tool_input}


def _advisory_context(result: dict) -> str:
    hso = result["hookSpecificOutput"]
    assert hso["hookEventName"] == "PreToolUse"
    assert "permissionDecision" not in hso
    assert "additionalContext" in hso
    return hso["additionalContext"]


@pytest.fixture(autouse=True)
def _clear_punt_env():
    """COORDINATOR_QUEUE_PUNT must not leak between tests (env-hatch is
    session-scoped, not payload-scoped -- see guard._ESCAPE_HATCH_ENV_VAR)."""
    os.environ.pop(guard._ESCAPE_HATCH_ENV_VAR, None)
    yield
    os.environ.pop(guard._ESCAPE_HATCH_ENV_VAR, None)


class TestGateOnToolAndPath:
    """Non-write tools and non-queue paths clear silently (None)."""

    @pytest.mark.parametrize(
        "tool_name,tool_input",
        [
            ("Read", {"file_path": "state/improvement-queue/foo.yaml"}),
            ("Write", {"file_path": "state/handoffs/foo.md", "content": "x"}),
            ("Write", "not-a-dict"),
            ("Write", {"file_path": ""}),
        ],
        ids=["non_write_tool", "non_queue_path", "tool_input_not_dict", "file_path_empty"],
    )
    def test_passes_through(self, tool_name, tool_input):
        assert guard.check(_payload(tool_name, tool_input)) is None


class TestQueueWriteDenied:
    """Write/MultiEdit to a queue file (structured YAML or legacy prose) denies
    with the five-question friction message."""

    def test_yaml_write_deny(self):
        result = guard.check(
            _payload(
                "Write",
                {
                    "file_path": "state/improvement-queue/new-item.yaml",
                    "content": "title: test\ndescription: a thing",
                },
            )
        )
        reason = _advisory_context(result)
        # 2026-07-30 escape-mechanism rework: the five questions moved out of
        # the inline deny text to docs/reference/queue-admission-five-questions.md
        # (see that module's docstring) -- the deny text now points there and
        # leads with the content-based `justification:` escape instead.
        assert "queue-admission-five-questions.md" in reason
        assert "justification:" in reason
        assert "state/improvement-queue/new-item.yaml" in reason

    def test_multiedit_queue_deny(self):
        result = guard.check(
            _payload(
                "MultiEdit",
                {"file_path": "state/improvement-queue/batch-edit.yaml"},
            )
        )
        _advisory_context(result)

    def test_legacy_md_path_write_deny(self):
        result = guard.check(
            _payload(
                "Write",
                {
                    "file_path": "state/improvement-queue.md",
                    "content": "- 2026-07-04 | new entry | details",
                },
            )
        )
        _advisory_context(result)


class TestYamlEditIsLowerFriction:
    def test_edit_yaml_no_deny(self):
        """Field-level Edit to an existing structured YAML entry passes
        silently -- only new-entry Writes get the nudge."""
        result = guard.check(
            _payload(
                "Edit",
                {
                    "file_path": "state/improvement-queue/existing.yaml",
                    "new_string": "updated_field: new_value",
                    "old_string": "updated_field: old_value",
                },
            )
        )
        assert result is None


class TestMultiEditJustificationScopeIsWholeCall:
    """Review: code-reviewer P2 -- `_gather_new_content()` joins every edit's
    `new_string` for a MultiEdit call, so a `justification:` line attached to
    one edit satisfies the escape for a co-occurring, unrelated, unjustified
    entry in the SAME call. This is a documented, KEPT trade (module
    docstring: "a single MultiEdit call is one atomic write"), not a bug --
    this test pins the adversarial shape explicitly so the next reader sees
    it was a deliberate choice, not an oversight nobody exercised."""

    def test_justification_on_one_edit_escapes_unjustified_sibling_edit(self):
        """Adversarial shape: two edits in one MultiEdit call, only one of
        which carries a non-trivial `justification:` line. Current, KEPT
        behavior: the write is allowed, because the guard scans the joined
        text of the whole call, not each edit independently."""
        result = guard.check(
            _payload(
                "MultiEdit",
                {
                    "file_path": "state/improvement-queue/2026-07-30-batch.yaml",
                    "edits": [
                        {
                            "old_string": "old unrelated field",
                            "new_string": "unrelated_field: unrelated_value\n",
                        },
                        {
                            "old_string": "",
                            "new_string": (
                                "- 2026-07-30 | brand new unjustified entry\n"
                                "  justification: this line belongs to a "
                                "DIFFERENT entry in this same call\n"
                            ),
                        },
                    ],
                },
            )
        )
        assert result is None, (
            "documented trade: a justification: anywhere in a MultiEdit's "
            "joined new_string text satisfies the escape for the whole call -- "
            "if this assertion starts failing, _gather_new_content's per-call "
            "join semantics changed and this test (and the module docstring "
            "trade it pins) need to be revisited together"
        )


class TestPuntEscapeHatch:
    def test_non_trivial_reason_suppresses(self):
        os.environ[guard._ESCAPE_HATCH_ENV_VAR] = "genuinely cross-cutting, needs separate plan"
        result = guard.check(
            _payload("Write", {"file_path": "state/improvement-queue/item.yaml"})
        )
        assert result is None

    def test_trivial_reason_advises_with_hint(self):
        os.environ[guard._ESCAPE_HATCH_ENV_VAR] = "ok"
        result = guard.check(
            _payload("Write", {"file_path": "state/improvement-queue/item.yaml"})
        )
        reason = _advisory_context(result)
        assert "trivial" in reason.lower() or guard._ESCAPE_HATCH_ENV_VAR in reason


class TestAuthoringSkillSuppression:
    """A transcript tail showing an active authoring-skill invocation suppresses
    the deny to None (reference hook lines 196-214) -- mirrors
    test_hooks_roundtrip.py's test_unauthorized_handoff_command_tag_in_transcript_suppresses
    / ..._coordinator_skill_in_tail_suppresses convention for nudge_unauthorized_handoff."""

    def _write_transcript(self, tmp_path: Path, tail: str) -> str:
        transcript = tmp_path / "transcript.jsonl"
        transcript.write_text(tail)
        return str(transcript)

    def test_command_name_tag_in_transcript_suppresses(self, tmp_path: Path):
        transcript_path = self._write_transcript(
            tmp_path, "<command-name>distill</command-name>\n"
        )
        result = guard.check(
            {
                "tool_name": "Write",
                "tool_input": {"file_path": "state/improvement-queue/new-item.yaml"},
                "transcript_path": transcript_path,
            }
        )
        assert result is None

    def test_coordinator_skill_in_tail_suppresses(self, tmp_path: Path):
        transcript_path = self._write_transcript(
            tmp_path, "invoking coordinator:workstream-complete\n"
        )
        result = guard.check(
            {
                "tool_name": "Write",
                "tool_input": {"file_path": "state/improvement-queue/new-item.yaml"},
                "transcript_path": transcript_path,
            }
        )
        assert result is None


class TestModuleContract:
    """Cheap regression net against an accidental CLASS/PRIORITY/MATCHERS edit --
    parity with test_nudge_windows_subprocess_popup.py's
    TestClassAndAllowlistStillWork.test_class_is_advisory."""

    def test_class_is_advisory(self):
        assert guard.CLASS == "advisory"

    def test_priority_and_matchers(self):
        assert guard.PRIORITY == 120
        assert guard.MATCHERS == ["Write", "Edit", "MultiEdit"]

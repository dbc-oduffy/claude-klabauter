"""Behavioral tests for
coordinator_core.write_guards.nudge_session_display_name_as_identifier --
the session-display-name-as-identifier advisory guard.

Spec backlink: dispatch brief "session citation stops depending on a name"
  (Deliverable-Id: dlv-session-citation-stops-depending-on-a-name-1c3053)

Covers: a name-shaped token firing in a targeted record body, a uuid never
firing, exclusion of `state/subagent-share/`/`archive/**`/`docs/research/`,
the fenced-code/inline-code exclusion vs. the quoted-prose non-exclusion,
non-Write-tool passthrough, oversized-content passthrough, and registry
enrollment (module cannot be silently unwired).
"""

from __future__ import annotations

import pytest

from coordinator_core.write_guards import (
    nudge_session_display_name_as_identifier as guard,
)


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


class TestModuleContract:
    def test_class_is_advisory(self):
        assert guard.CLASS == "advisory"

    def test_matchers(self):
        assert set(guard.MATCHERS) == {"Write", "Edit", "MultiEdit"}

    def test_priority_is_int(self):
        assert isinstance(guard.PRIORITY, int)

    def test_enrolled_in_discovery(self):
        from coordinator_core.write_guards import engine

        names, errors = engine.discover_guard_names()
        assert errors == []
        assert "nudge_session_display_name_as_identifier" in names


class TestFiresOnTargetedRecordBody:
    @pytest.mark.parametrize(
        "file_path",
        [
            "state/bug-backlog/2026-08-30-foo-abc123.yaml",
            "state/debt-backlog/2026-08-30-foo-abc123.yaml",
            "state/lessons/some-lesson.md",
            "docs/plans/2026-08-30-a-plan.md",
            "state/sizings/2026-08-30-a-sizing.yaml",
            "state/handoffs/2026-08-30-a-handoff.md",
            "docs/decisions/DR-999-a-decision.md",
        ],
    )
    def test_name_in_body_fires(self, file_path):
        content = "ESTABLISHED AND FIXED BY claude-klabauter-49"
        result = guard.check(
            _payload("Write", file_path, content=content)
        )
        assert result is not None
        text = _advisory_text(result)
        assert "claude-klabauter-49" in text
        assert "uuid" in text.lower()

    def test_fires_via_edit_new_string(self):
        result = guard.check(
            _payload(
                "Edit",
                "state/lessons/foo.md",
                old_string="x",
                new_string="fixed by doe-claude-3a",
            )
        )
        assert result is not None
        assert "doe-claude-3a" in _advisory_text(result)


class TestNarrativeMentionIsSilent:
    """Pins the module docstring's own negative-spec claim ("A session
    named in narrative... is silent; pinned by
    `TestNarrativeMentionIsSilent`"). Fixture is a REAL excerpt, not
    synthesized -- lifted verbatim from
    `state/bug-backlog/2026-08-19-a-fix-is-not-live-until-it-is-published.yaml`,
    one of the 441 records the coordinator's corpus sweep found firing
    under the pre-narrowing (bare-mention) predicate.
    """

    def test_real_narrative_excerpt_is_silent(self):
        content = (
            "FIELD COST, MEASURED TODAY, INDEPENDENT OF THIS SESSION. "
            "doe-claude-3e hit real divergence while working their own "
            "baton: they resolved to the mirror, read CONTRACT_VERSION "
            "3.14.0, and correctly declined to run the emitter."
        )
        result = guard.check(
            _payload("Write", "state/bug-backlog/foo.yaml", content=content)
        )
        assert result is None


class TestScaffolderAuthorFieldIsSilent:
    """`author:` is deliberately EXCLUDED from `_ATTRIB_FIELDS` (2026-08-30
    coordinator ruling, second pass): `coordinator/bin/coordinator-doc-new.py
    :: _resolve_plan_author()` stamps this field automatically at scaffold
    time with the session's own display name -- it is tool-written, not a
    human crediting claim, and the author of the record cannot act on an
    advisory pointed at a line a tool wrote before they touched the file.
    If this test starts failing because someone re-added `author` to
    `_ATTRIB_FIELDS`, read `_ATTRIB_FIELDS`'s own module-level comment
    before reverting this fixture -- the fix belongs in
    `_resolve_plan_author`, not here.
    """

    def test_scaffolder_written_author_line_is_silent(self):
        content = (
            "---\n"
            "author: claude-klabauter-49\n"
            "created: 2026-08-30\n"
            "---\n\n"
            "Plan body, no other display-name mention.\n"
        )
        result = guard.check(
            _payload("Write", "docs/plans/foo.md", content=content)
        )
        assert result is None


class TestUuidNeverFires:
    def test_uuid_alone_is_silent(self):
        content = "claimed_by: a1b2c3d4-e5f6-7890-abcd-ef1234567890"
        result = guard.check(
            _payload("Write", "state/handoffs/foo.md", content=content)
        )
        assert result is None

    def test_uuid_plus_prose_is_silent(self):
        content = (
            "Fixed by session a1b2c3d4-e5f6-7890-abcd-ef1234567890, "
            "no display name mentioned."
        )
        result = guard.check(
            _payload("Write", "docs/plans/foo.md", content=content)
        )
        assert result is None


class TestExcludedPaths:
    @pytest.mark.parametrize(
        "file_path",
        [
            "state/subagent-share/some-session/coordinatorexecutor.abc.md",
            "archive/completed/2026-08/2026-08-30-adhoc-abc123.md",
            "docs/research/2026-08-30-some-research.md",
        ],
    )
    def test_excluded_path_is_silent(self, file_path):
        content = "ESTABLISHED AND FIXED BY claude-klabauter-49"
        result = guard.check(_payload("Write", file_path, content=content))
        assert result is None


class TestCodeSpanExclusion:
    def test_fenced_code_block_is_silent(self):
        content = (
            "Some prose.\n\n```\nESTABLISHED AND FIXED BY claude-klabauter-49\n```\n"
        )
        result = guard.check(
            _payload("Write", "state/lessons/foo.md", content=content)
        )
        assert result is None

    def test_inline_code_span_is_silent(self):
        content = "Log line: `claude-klabauter-49 did the fix` (pasted output)."
        result = guard.check(
            _payload("Write", "state/lessons/foo.md", content=content)
        )
        assert result is None

    def test_quoted_prose_still_fires(self):
        content = 'The row said "ESTABLISHED AND FIXED BY claude-klabauter-49".'
        result = guard.check(
            _payload("Write", "state/bug-backlog/foo.yaml", content=content)
        )
        assert result is not None
        assert "claude-klabauter-49" in _advisory_text(result)


class TestPassthrough:
    def test_non_write_tool_passes_through(self):
        result = guard.check(
            _payload("Read", "state/bug-backlog/foo.yaml", content="claude-klabauter-49")
        )
        assert result is None

    def test_out_of_scope_path_passes_through(self):
        result = guard.check(
            _payload("Write", "coordinator_core/foo.py", content="claude-klabauter-49")
        )
        assert result is None

    def test_no_display_name_passes_through(self):
        result = guard.check(
            _payload(
                "Write", "state/bug-backlog/foo.yaml", content="ordinary prose, no names"
            )
        )
        assert result is None

    def test_tool_input_not_dict_passes_through(self):
        result = guard.check({"tool_name": "Write", "tool_input": "not-a-dict"})
        assert result is None

    def test_empty_file_path_passes_through(self):
        result = guard.check(_payload("Write", "", content="claude-klabauter-49"))
        assert result is None

    def test_oversized_content_passes_through(self):
        big = "x" * (guard._MAX_WHOLE_FILE_BYTES + 1) + " claude-klabauter-49"
        result = guard.check(
            _payload("Write", "state/bug-backlog/foo.yaml", content=big)
        )
        assert result is None

    def test_role_suffix_without_digit_passes_through(self):
        result = guard.check(
            _payload(
                "Write",
                "state/bug-backlog/foo.yaml",
                content="see claude-klabauter-em for the EM's own thread",
            )
        )
        assert result is None


class TestScopeIsCaseInsensitive:
    """The scope check must fold case on both sides.

    On Windows and APFS `State/Bug-Backlog/x.yaml` names the SAME file as
    `state/bug-backlog/x.yaml`. An unfolded comparison let a caller walk
    around the entire guard with nothing but a shift key, and the
    casefold-bypass lint did not catch it -- that lint keys on a narrower
    set of comparison shapes than `marker in normalized`, so this class is
    the only thing standing between the guard and that bypass.
    """

    _BODY = "body: ESTABLISHED AND FIXED BY claude-klabauter-49 at 11f1a761e6"

    def _check(self, path):
        return guard.check(
            {"tool_name": "Write", "tool_input": {"file_path": path, "content": self._BODY}}
        )

    def test_upper_case_in_scope_path_still_fires(self):
        assert self._check("STATE/BUG-BACKLOG/x.yaml") is not None

    def test_mixed_case_in_scope_path_still_fires(self):
        assert self._check("State/Bug-Backlog/x.yaml") is not None

    def test_backslash_separator_still_fires(self):
        assert self._check(r"state\bug-backlog\x.yaml") is not None

    def test_upper_case_out_of_scope_path_stays_silent(self):
        assert self._check("STATE/SUBAGENT-SHARE/x.yaml") is None

"""Behavioral tests for coordinator_core.write_guards.block_subagent_archive_write
-- the wrap-up self-log backstop guard (see the module's own docstring for
the reference-hook port this is a faithful engine-ification of).

Covers the 2026-08-03 widening (memo
cross-repo/inbox/2026-08-03-example-doctrine-repo-em-archive-write-guard-pincer.md):

  (A) the fire condition now gates on RAW agent_id presence, not the
      bare-hex-only format guard -- a named-teammate agent_id
      (a<name>-<16hex>) is no longer treated as "no agent_id" (allow).
  (B) a resolved agent whose back-pointer subagent_type is exactly
      coordinator:review-integrator gets a sanctioned archive/ write path,
      with an asymmetric fail-open discipline: a failed/missing
      back-pointer lookup must NOT allow (falls through to deny), the
      opposite fail direction from block_subagent_plan_body_write.

No dedicated behaviour test file previously existed for this guard --
only test_deny_text_reachable_override.py and
test_guard_registry_manifest.py referenced it in passing.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.write_guards import block_subagent_archive_write as guard

_BARE_HEX_AGENT_ID = "deadbeef0123abcd"
_NAMED_TEAMMATE_AGENT_ID = "aexecutor-teammate-1234567890abcdef"


def _payload(
    repo_root: Path,
    rel_file_path: str,
    agent_id: str = _BARE_HEX_AGENT_ID,
    session_id: str = "sess-12345678",
    tool_name: str = "Write",
) -> dict:
    return {
        "tool_name": tool_name,
        "tool_input": {"file_path": rel_file_path, "content": "x"},
        "cwd": str(repo_root),
        "agent_id": agent_id,
        "session_id": session_id,
    }


@pytest.fixture(autouse=True)
def _clear_override_env(monkeypatch):
    monkeypatch.delenv("COORDINATOR_OVERRIDE_SUBAGENT_ARCHIVE", raising=False)


def _stub_git_root(repo_root: Path):
    def _fake(cwd):
        return str(repo_root)

    return _fake


def _stub_subagent_type(subagent_type: str):
    def _fake(git_root, agent_id):
        return subagent_type

    return _fake


def _stub_subagent_type_raises():
    def _fake(git_root, agent_id):
        raise AssertionError("back-pointer lookup should not have been called")

    return _fake


class TestBareHexIdentityStillDenies:
    """Pre-existing behaviour preserved: a bare-hex agent_id writing under
    archive/ outside the carve-outs is denied.
    """

    def test_bare_hex_agent_id_archive_write_denied(self, tmp_path, monkeypatch):
        monkeypatch.setattr(guard, "_resolve_git_root", _stub_git_root(tmp_path))
        monkeypatch.setattr(guard, "_read_backpointer_subagent_type", _stub_subagent_type(""))
        monkeypatch.setattr(guard, "_write_block_log", lambda *a, **kw: None)

        payload = _payload(tmp_path, "archive/foo.md", agent_id=_BARE_HEX_AGENT_ID)
        result = guard.check(payload)

        assert result is not None
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
        reason = result["hookSpecificOutput"]["permissionDecisionReason"]
        assert _BARE_HEX_AGENT_ID in reason
        assert "archive/foo.md" in reason


class TestNamedTeammateIdentityNowDenies:
    """The regression this fixes -- would have passed as ALLOW before
    2026-08-03 (bare-hex-only format gate treated a named-teammate agent_id
    as no-agent-id).
    """

    def test_named_teammate_agent_id_archive_write_denied(self, tmp_path, monkeypatch):
        monkeypatch.setattr(guard, "_resolve_git_root", _stub_git_root(tmp_path))
        monkeypatch.setattr(guard, "_read_backpointer_subagent_type", _stub_subagent_type(""))
        monkeypatch.setattr(guard, "_write_block_log", lambda *a, **kw: None)

        payload = _payload(
            tmp_path,
            "archive/foo.md",
            agent_id=_NAMED_TEAMMATE_AGENT_ID,
            session_id="3819c0e9-edc6-449d-a6f5-1bb3ae2c3ca0",
        )
        result = guard.check(payload)

        assert result is not None
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
        reason = result["hookSpecificOutput"]["permissionDecisionReason"]
        # Resolved canonical identity, not the raw a<name>-<16hex> shape.
        assert "executor-teammate@session-3819c0e9" in reason
        assert "archive/foo.md" in reason


class TestNoAgentIdAllows:
    def test_em_write_no_agent_id_allowed(self, tmp_path, monkeypatch):
        monkeypatch.setattr(guard, "_read_backpointer_subagent_type", _stub_subagent_type_raises())

        payload = {
            "tool_name": "Write",
            "tool_input": {"file_path": "archive/foo.md", "content": "x"},
            "cwd": str(tmp_path),
        }
        assert guard.check(payload) is None

    def test_em_write_empty_agent_id_allowed(self, tmp_path, monkeypatch):
        monkeypatch.setattr(guard, "_read_backpointer_subagent_type", _stub_subagent_type_raises())

        payload = _payload(tmp_path, "archive/foo.md", agent_id="")
        assert guard.check(payload) is None


class TestCarveOutsAllowForBothIdentityShapes:
    @pytest.mark.parametrize("agent_id", [_BARE_HEX_AGENT_ID, _NAMED_TEAMMATE_AGENT_ID])
    def test_daily_summaries_carve_out_allows(self, tmp_path, monkeypatch, agent_id):
        monkeypatch.setattr(guard, "_resolve_git_root", _stub_git_root(tmp_path))
        monkeypatch.setattr(guard, "_read_backpointer_subagent_type", _stub_subagent_type_raises())

        payload = _payload(tmp_path, "archive/daily-summaries/2026-08-03.md", agent_id=agent_id)
        assert guard.check(payload) is None

    @pytest.mark.parametrize("agent_id", [_BARE_HEX_AGENT_ID, _NAMED_TEAMMATE_AGENT_ID])
    def test_daily_summaries_carve_out_with_machine_suffix_allows(
        self, tmp_path, monkeypatch, agent_id
    ):
        monkeypatch.setattr(guard, "_resolve_git_root", _stub_git_root(tmp_path))
        monkeypatch.setattr(guard, "_read_backpointer_subagent_type", _stub_subagent_type_raises())

        payload = _payload(
            tmp_path, "archive/daily-summaries/2026-08-03-machine-b.md", agent_id=agent_id
        )
        assert guard.check(payload) is None

    @pytest.mark.parametrize("agent_id", [_BARE_HEX_AGENT_ID, _NAMED_TEAMMATE_AGENT_ID])
    def test_completed_fallback_carve_out_allows(self, tmp_path, monkeypatch, agent_id):
        monkeypatch.setattr(guard, "_resolve_git_root", _stub_git_root(tmp_path))
        monkeypatch.setattr(guard, "_read_backpointer_subagent_type", _stub_subagent_type_raises())

        payload = _payload(
            tmp_path, "archive/completed/2026-08/2026-08-03-slug.md", agent_id=agent_id
        )
        assert guard.check(payload) is None


class TestReviewIntegratorAllowCondition:
    """2026-08-03 widening: coordinator:review-integrator gets a sanctioned
    archive/ write path, with fail-CLOSED lookup semantics (opposite of
    block_subagent_plan_body_write).
    """

    def test_review_integrator_backpointer_allows(self, tmp_path, monkeypatch):
        monkeypatch.setattr(guard, "_resolve_git_root", _stub_git_root(tmp_path))
        monkeypatch.setattr(
            guard,
            "_read_backpointer_subagent_type",
            _stub_subagent_type("coordinator:review-integrator"),
        )

        payload = _payload(tmp_path, "archive/specs/foo.md")
        assert guard.check(payload) is None

    def test_executor_backpointer_still_denies(self, tmp_path, monkeypatch):
        monkeypatch.setattr(guard, "_resolve_git_root", _stub_git_root(tmp_path))
        monkeypatch.setattr(
            guard, "_read_backpointer_subagent_type", _stub_subagent_type("coordinator:executor")
        )
        monkeypatch.setattr(guard, "_write_block_log", lambda *a, **kw: None)

        payload = _payload(tmp_path, "archive/specs/foo.md")
        result = guard.check(payload)

        assert result is not None
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"

    def test_missing_backpointer_lookup_denies_not_allows(self, tmp_path, monkeypatch):
        """Asymmetric fail-open discipline: a lookup-fail (empty string,
        missing/unreadable back-pointer) must NOT allow here -- it falls
        through to the normal deny path.
        """
        monkeypatch.setattr(guard, "_resolve_git_root", _stub_git_root(tmp_path))
        monkeypatch.setattr(guard, "_read_backpointer_subagent_type", _stub_subagent_type(""))
        monkeypatch.setattr(guard, "_write_block_log", lambda *a, **kw: None)

        payload = _payload(tmp_path, "archive/specs/foo.md")
        result = guard.check(payload)

        assert result is not None
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"

    def test_unresolvable_git_root_denies_not_allows(self, tmp_path, monkeypatch):
        """git_root resolution failure -> back-pointer lookup skipped
        entirely -> still denies (never allows on lookup-unreachable)."""
        monkeypatch.setattr(guard, "_resolve_git_root", lambda cwd: None)
        monkeypatch.setattr(guard, "_read_backpointer_subagent_type", _stub_subagent_type_raises())
        monkeypatch.setattr(guard, "_write_block_log", lambda *a, **kw: None)

        payload = _payload(tmp_path, "archive/specs/foo.md")
        result = guard.check(payload)

        assert result is not None
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"


class TestWeekChangelogsCarveOut:
    """2026-08-06 widening (memo
    cross-repo/inbox/2026-08-06-example-cockpit-repo-em-archive-write-guard-week-changelogs-gap.md):
    week-changelogs carve-out and deny-message routing.
    """

    @pytest.mark.parametrize("agent_id", [_BARE_HEX_AGENT_ID, _NAMED_TEAMMATE_AGENT_ID])
    def test_dated_daily_block_allows(self, tmp_path, monkeypatch, agent_id):
        monkeypatch.setattr(guard, "_resolve_git_root", _stub_git_root(tmp_path))
        monkeypatch.setattr(guard, "_read_backpointer_subagent_type", _stub_subagent_type_raises())

        payload = _payload(
            tmp_path,
            "archive/week-changelogs/2026-07-20/2026-07-21-machine-b.md",
            agent_id=agent_id,
        )
        assert guard.check(payload) is None

    def test_week_summary_allows(self, tmp_path, monkeypatch):
        monkeypatch.setattr(guard, "_resolve_git_root", _stub_git_root(tmp_path))
        monkeypatch.setattr(guard, "_read_backpointer_subagent_type", _stub_subagent_type_raises())

        payload = _payload(tmp_path, "archive/week-changelogs/2026-07-20/WEEK-SUMMARY.md")
        assert guard.check(payload) is None

    def test_week_summary_partial_allows(self, tmp_path, monkeypatch):
        monkeypatch.setattr(guard, "_resolve_git_root", _stub_git_root(tmp_path))
        monkeypatch.setattr(guard, "_read_backpointer_subagent_type", _stub_subagent_type_raises())

        payload = _payload(tmp_path, "archive/week-changelogs/2026-07-20/WEEK-SUMMARY.partial.md")
        assert guard.check(payload) is None

    def test_mixed_case_suffix_allows(self, tmp_path, monkeypatch):
        monkeypatch.setattr(guard, "_resolve_git_root", _stub_git_root(tmp_path))
        monkeypatch.setattr(guard, "_read_backpointer_subagent_type", _stub_subagent_type_raises())

        payload = _payload(
            tmp_path,
            "archive/week-changelogs/2026-07-03/2026-07-03-Machine-b-backfill.md",
        )
        assert guard.check(payload) is None

    def test_undated_directory_denies(self, tmp_path, monkeypatch):
        monkeypatch.setattr(guard, "_resolve_git_root", _stub_git_root(tmp_path))
        monkeypatch.setattr(guard, "_read_backpointer_subagent_type", _stub_subagent_type(""))
        monkeypatch.setattr(guard, "_write_block_log", lambda *a, **kw: None)

        payload = _payload(tmp_path, "archive/week-changelogs/notadate/foo.md")
        result = guard.check(payload)

        assert result is not None
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"

    def test_basename_outside_constrained_set_denies(self, tmp_path, monkeypatch):
        monkeypatch.setattr(guard, "_resolve_git_root", _stub_git_root(tmp_path))
        monkeypatch.setattr(guard, "_read_backpointer_subagent_type", _stub_subagent_type(""))
        monkeypatch.setattr(guard, "_write_block_log", lambda *a, **kw: None)

        payload = _payload(tmp_path, "archive/week-changelogs/2026-07-20/random-notes.md")
        result = guard.check(payload)

        assert result is not None
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"

    def test_header_priorities_not_in_dated_dir_denies(self, tmp_path, monkeypatch):
        monkeypatch.setattr(guard, "_resolve_git_root", _stub_git_root(tmp_path))
        monkeypatch.setattr(guard, "_read_backpointer_subagent_type", _stub_subagent_type(""))
        monkeypatch.setattr(guard, "_write_block_log", lambda *a, **kw: None)

        payload = _payload(tmp_path, "archive/week-changelogs/HEADER.priorities.abc123.md")
        result = guard.check(payload)

        assert result is not None
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"

    def test_denied_week_changelog_deny_text_does_not_route_to_daily_summaries(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(guard, "_resolve_git_root", _stub_git_root(tmp_path))
        monkeypatch.setattr(guard, "_read_backpointer_subagent_type", _stub_subagent_type(""))
        monkeypatch.setattr(guard, "_write_block_log", lambda *a, **kw: None)

        payload = _payload(tmp_path, "archive/week-changelogs/2026-07-20/random-notes.md")
        result = guard.check(payload)

        reason = result["hookSpecificOutput"]["permissionDecisionReason"]
        assert "archive/daily-summaries/" not in reason
        assert "archive/week-changelogs/YYYY-MM-DD/" in reason

    def test_denied_elsewhere_under_archive_keeps_byte_for_byte_daily_summaries_line(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(guard, "_resolve_git_root", _stub_git_root(tmp_path))
        monkeypatch.setattr(guard, "_read_backpointer_subagent_type", _stub_subagent_type(""))
        monkeypatch.setattr(guard, "_write_block_log", lambda *a, **kw: None)

        payload = _payload(tmp_path, "archive/foo.md")
        result = guard.check(payload)

        reason = result["hookSpecificOutput"]["permissionDecisionReason"]
        # Byte-for-byte format drift (not a content/semantic change): the
        # message body edit that landed this text dropped the trailing
        # newline after this line -- it is now the LAST line of the
        # rendered reason, not followed by more text. Still an assert-
        # PRESENT of the real, current byte-for-byte text (not weakened to
        # a substring-of-a-substring or a vacuous check).
        assert reason.endswith(
            "Use instead: `archive/daily-summaries/YYYY-MM-DD.md` (or `-<machine>.md`)."
        )


class TestOtherArchiveSubtreesGetSafeDefaultDenyText:
    """2026-08-06 widening (second): a denied write under an archive/
    subtree that is neither week-changelogs nor daily-summary-shaped must
    not be told to file itself under a carve-out it does not describe --
    see the investigation naming /distill (archive/specs) and
    /update-docs -> /learn-lessons (archive/lessons-archived) as the live
    callers that surfaced this.
    """

    @pytest.mark.parametrize(
        "rel_path",
        [
            "archive/specs/2026-08/foo.md",
            "archive/lessons-archived/2026-08.md",
            "archive/release-notes/2026-07-19-v0.2.0.md",
            "archive/some-new-thing/x.md",
        ],
    )
    def test_safe_default_names_no_writable_path(self, tmp_path, monkeypatch, rel_path):
        monkeypatch.setattr(guard, "_resolve_git_root", _stub_git_root(tmp_path))
        monkeypatch.setattr(guard, "_read_backpointer_subagent_type", _stub_subagent_type(""))
        monkeypatch.setattr(guard, "_write_block_log", lambda *a, **kw: None)

        payload = _payload(tmp_path, rel_path)
        result = guard.check(payload)

        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
        reason = result["hookSpecificOutput"]["permissionDecisionReason"]
        assert "archive/daily-summaries/" not in reason
        assert "archive/week-changelogs/" not in reason
        assert "Use instead: `archive/" not in reason


class TestAllDeniedTargetsStillDeny:
    """A future carve-out must not get smuggled in under a text-only
    change -- every target this module docstring names as denied must
    still be denied after the 2026-08-06 deny-text restructure.
    """

    @pytest.mark.parametrize(
        "rel_path",
        [
            "archive/foo.md",
            "archive/daily-summaries/badname.md",
            "archive/specs/2026-08/foo.md",
            "archive/lessons-archived/2026-08.md",
            "archive/release-notes/2026-07-19-v0.2.0.md",
            "archive/some-new-thing/x.md",
            "archive/week-changelogs/2026-07-20/random-notes.md",
            "archive/week-changelogs/notadate/foo.md",
        ],
    )
    def test_still_denies(self, tmp_path, monkeypatch, rel_path):
        monkeypatch.setattr(guard, "_resolve_git_root", _stub_git_root(tmp_path))
        monkeypatch.setattr(guard, "_read_backpointer_subagent_type", _stub_subagent_type(""))
        monkeypatch.setattr(guard, "_write_block_log", lambda *a, **kw: None)

        payload = _payload(tmp_path, rel_path)
        result = guard.check(payload)

        assert result is not None
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"


class TestPathScopeAndOverride:
    def test_path_not_under_archive_allows(self, tmp_path, monkeypatch):
        monkeypatch.setattr(guard, "_resolve_git_root", _stub_git_root(tmp_path))
        monkeypatch.setattr(guard, "_read_backpointer_subagent_type", _stub_subagent_type_raises())

        payload = _payload(tmp_path, "docs/wiki/foo.md")
        assert guard.check(payload) is None

    def test_override_env_allows(self, tmp_path, monkeypatch):
        monkeypatch.setenv("COORDINATOR_OVERRIDE_SUBAGENT_ARCHIVE", "1")
        monkeypatch.setattr(guard, "_read_backpointer_subagent_type", _stub_subagent_type_raises())

        payload = _payload(tmp_path, "archive/foo.md")
        assert guard.check(payload) is None


class TestPathNormalization:
    def test_windows_backslash_path_denies(self, tmp_path, monkeypatch):
        monkeypatch.setattr(guard, "_resolve_git_root", _stub_git_root(tmp_path))
        monkeypatch.setattr(guard, "_read_backpointer_subagent_type", _stub_subagent_type(""))
        monkeypatch.setattr(guard, "_write_block_log", lambda *a, **kw: None)

        payload = _payload(tmp_path, "archive\\foo.md")
        result = guard.check(payload)

        assert result is not None
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"

    def test_doubled_slash_path_denies(self, tmp_path, monkeypatch):
        monkeypatch.setattr(guard, "_resolve_git_root", _stub_git_root(tmp_path))
        monkeypatch.setattr(guard, "_read_backpointer_subagent_type", _stub_subagent_type(""))
        monkeypatch.setattr(guard, "_write_block_log", lambda *a, **kw: None)

        payload = _payload(tmp_path, "archive//sub//foo.md")
        result = guard.check(payload)

        assert result is not None
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"

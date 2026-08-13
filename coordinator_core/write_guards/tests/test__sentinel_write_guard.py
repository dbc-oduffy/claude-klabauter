"""Behavioral tests for coordinator_core.write_guards._sentinel_write_guard
-- the shared sentinel-path helper consumed by guard-worktree-sentinel-
write, guard-settings-json-write, and guard-doctrine-surface-edits.

Spec backlink: docs/plans/2026-07-29-hook-fan-in-write-path.md (chunk C3)
"""

from __future__ import annotations

from coordinator_core.write_guards import _sentinel_write_guard as helper


class TestExtractTargetPath:
    def test_write_edit_multiedit_use_file_path(self):
        assert (
            helper.extract_target_path({"file_path": "/repo/.sentinel"})
            == "/repo/.sentinel"
        )

    def test_notebook_edit_uses_notebook_path(self):
        assert (
            helper.extract_target_path({"notebook_path": "/repo/nb.ipynb"})
            == "/repo/nb.ipynb"
        )

    def test_file_path_preferred_over_notebook_path(self):
        payload = {"file_path": "/repo/.sentinel", "notebook_path": "/repo/nb.ipynb"}
        assert helper.extract_target_path(payload) == "/repo/.sentinel"

    def test_bare_path_key_supported(self):
        assert helper.extract_target_path({"path": "/repo/.sentinel"}) == "/repo/.sentinel"

    def test_missing_keys_returns_empty_string(self):
        assert helper.extract_target_path({}) == ""

    def test_non_dict_input_returns_empty_string(self):
        assert helper.extract_target_path("not-a-dict") == ""  # type: ignore[arg-type]

    def test_non_string_value_falls_through(self):
        payload = {"file_path": ["a", "list"], "notebook_path": "/repo/nb.ipynb"}
        assert helper.extract_target_path(payload) == "/repo/nb.ipynb"

    def test_blank_string_falls_through(self):
        payload = {"file_path": "   ", "path": "/repo/.sentinel"}
        assert helper.extract_target_path(payload) == "/repo/.sentinel"

    def test_value_is_stripped(self):
        assert helper.extract_target_path({"file_path": "  /repo/.sentinel  "}) == "/repo/.sentinel"


class TestIsSentinelWrite:
    def test_exact_basename_match(self, tmp_path):
        target = str(tmp_path / ".coordinator-override-worktree-guard")
        assert helper.is_sentinel_write(
            target, ".coordinator-override-worktree-guard"
        )

    def test_case_folded_match(self, tmp_path):
        target = str(tmp_path / ".Coordinator-Override-Worktree-Guard")
        assert helper.is_sentinel_write(
            target, ".coordinator-override-worktree-guard"
        )

    def test_no_match_for_unrelated_file(self, tmp_path):
        target = str(tmp_path / "some-other-file.txt")
        assert not helper.is_sentinel_write(
            target, ".coordinator-override-worktree-guard"
        )

    def test_no_substring_or_prefix_match(self, tmp_path):
        target = str(tmp_path / ".coordinator-override-worktree-guard-typo")
        assert not helper.is_sentinel_write(
            target, ".coordinator-override-worktree-guard"
        )

    def test_empty_path_returns_false(self):
        assert not helper.is_sentinel_write("", ".coordinator-override-worktree-guard")

    def test_relative_path_resolved_against_cwd(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        assert helper.is_sentinel_write(
            ".coordinator-override-worktree-guard",
            ".coordinator-override-worktree-guard",
        )

    def test_nonexistent_leaf_does_not_raise(self, tmp_path):
        target = str(tmp_path / "not-yet-created" / ".coordinator-override-worktree-guard")
        assert helper.is_sentinel_write(
            target, ".coordinator-override-worktree-guard"
        )


class TestSentinelWriteDenial:
    """``payload`` is a REQUIRED keyword (no default; 2026-08-13, C4c of
    docs/plans/2026-08-13-guard-messages-stop-handing-agents-the-keys.md) --
    every call below passes it explicitly, matching every real caller in
    this package. A missed keyword raises ``TypeError`` at collection, so
    there is no "omit it" shape left to test.
    """

    def test_returns_none_for_non_sentinel_path(self):
        result = helper.sentinel_write_denial(
            "/repo/README.md",
            ".coordinator-override-worktree-guard",
            "denied for reasons",
            payload=None,
        )
        assert result is None

    def test_returns_nested_deny_envelope_for_sentinel_path(self):
        result = helper.sentinel_write_denial(
            "/repo/.coordinator-override-worktree-guard",
            ".coordinator-override-worktree-guard",
            "denied for reasons",
            payload=None,
        )
        assert result == {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": "denied for reasons",
            }
        }

    def test_reason_string_preserved_byte_for_byte(self):
        reason = "Sentinel write blocked: exact reason text, byte-for-byte."
        result = helper.sentinel_write_denial(
            "/repo/.coordinator-override-worktree-guard",
            ".coordinator-override-worktree-guard",
            reason,
            payload=None,
        )
        assert result["hookSpecificOutput"]["permissionDecisionReason"] == reason

    def test_empty_target_path_passes_through(self):
        assert (
            helper.sentinel_write_denial(
                "", ".coordinator-override-worktree-guard", "reason", payload=None
            )
            is None
        )

    def test_missing_payload_keyword_raises_type_error(self):
        """No default -- a call site missed by the C4c migration must fail
        loudly at call time, never silently keep the pre-migration,
        payload-blind shape."""
        import pytest

        with pytest.raises(TypeError):
            helper.sentinel_write_denial(  # type: ignore[call-arg]
                "/repo/.coordinator-override-worktree-guard",
                ".coordinator-override-worktree-guard",
                "denied for reasons",
            )


class TestSentinelWriteAdvisory:
    def test_returns_none_for_non_sentinel_path(self):
        result = helper.sentinel_write_advisory(
            "/repo/README.md",
            ".coordinator-dev-repo",
            "advisory text",
            payload=None,
        )
        assert result is None

    def test_returns_nested_advisory_envelope_for_sentinel_path(self):
        result = helper.sentinel_write_advisory(
            "/repo/.coordinator-dev-repo",
            ".coordinator-dev-repo",
            "advisory text",
            payload=None,
        )
        assert result == {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "additionalContext": "advisory text",
            }
        }

    def test_missing_payload_keyword_raises_type_error(self):
        import pytest

        with pytest.raises(TypeError):
            helper.sentinel_write_advisory(  # type: ignore[call-arg]
                "/repo/.coordinator-dev-repo",
                ".coordinator-dev-repo",
                "advisory text",
            )

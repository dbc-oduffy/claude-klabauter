"""
coordinator_core.session.tests.test_identity — Python-side equivalent of the
bash oracle's T38a-k identity matrix (§ T38, 11/11 PASS baseline).

Oracle: test-coordinator-session.sh (DoE 1be5a573, 2026-07-21).

This suite is the required pre-cutover Python parity net named by the recipe
(§ identity.py row: "port MUST have a Python-side equivalent test file
before cutover, not just re-run the bash oracle — the bash oracle disappears
once the hub deletes").

Freeze doc: scratch/subagent-sandbox/bash-to-python-engine-migration/
freeze-coordinator-session-identity.md
"""

from __future__ import annotations

from coordinator_core.session import identity


class TestResolveSubagentIdentity:
    def test_t38a_bare_hex_passthrough_session_id_ignored(self):
        agent_id = "abcdef123456"
        assert identity.resolve_subagent_identity(agent_id, "totally-different-session") == agent_id

    def test_t38b_named_teammate_roundtrip_dash_containing_name(self):
        agent_id = "aprobe2-teammate-64cd7f42c270a899"
        session_id = "abcdef1234567890"
        result = identity.resolve_subagent_identity(agent_id, session_id)
        assert result == "probe2-teammate@session-abcdef12"

    def test_t38c_session_id_too_short_fails_closed(self):
        agent_id = "aname-64cd7f42c270a899"
        assert identity.resolve_subagent_identity(agent_id, "short12") == ""

    def test_t38f_session_id_exactly_8_chars_boundary_succeeds(self):
        agent_id = "aname-64cd7f42c270a899"
        session_id = "12345678"
        assert identity.resolve_subagent_identity(agent_id, session_id) == "name@session-12345678"

    def test_t38g_session_id_exactly_7_chars_boundary_fails_closed(self):
        agent_id = "aname-64cd7f42c270a899"
        session_id = "1234567"
        assert identity.resolve_subagent_identity(agent_id, session_id) == ""

    def test_t38d_garbage_agent_id_fails_closed(self):
        assert identity.resolve_subagent_identity("not-a-recognised-shape", "abcdef1234567890") == ""

    def test_t38e_build_canonical_agent_id_direct_smoke(self):
        assert identity.build_canonical_agent_id("myname", "abcdef12") == "myname@session-abcdef12"

    def test_t38h_bare_hex_exactly_12_chars_lower_bound_passthrough(self):
        agent_id = "a" * 12  # 12 lowercase-hex-valid chars
        assert identity.resolve_subagent_identity(agent_id, "") == agent_id

    def test_t38i_bare_hex_11_chars_below_floor_fails_closed(self):
        agent_id = "a" * 11
        assert identity.resolve_subagent_identity(agent_id, "") == ""

    def test_t38j_uppercase_hex_fails_closed(self):
        agent_id = "ABCDEF123456"
        assert identity.resolve_subagent_identity(agent_id, "") == ""

    def test_t38k_empty_agent_id_fails_closed(self):
        assert identity.resolve_subagent_identity("", "abcdef1234567890") == ""

    def test_named_teammate_greedy_capture_dash_in_name(self):
        # (.+) must be greedy so a dash-containing name is captured whole,
        # relying on the fixed-length 16-hex suffix to anchor the boundary.
        agent_id = "afoo-bar-baz-64cd7f42c270a899"
        result = identity.resolve_subagent_identity(agent_id, "abcdef1234567890")
        assert result == "foo-bar-baz@session-abcdef12"

    def test_named_teammate_malformed_suffix_fails_closed(self):
        # 15 hex chars (not 16) -> does not match the named-teammate grammar,
        # and does not match bare-hex either -> path (c).
        agent_id = "aname-64cd7f42c270a89"
        assert identity.resolve_subagent_identity(agent_id, "abcdef1234567890") == ""

    def test_always_succeeds_never_raises(self):
        # Bash contract: always exits 0; failure is signalled by empty
        # output only, never an exception/nonzero code.
        for bad in (None, "", "\n", "😀", "a-16hex-but-not-really"):
            identity.resolve_subagent_identity(bad or "", "abcdef1234567890")


class TestFormatOkAndConst:
    def test_canonical_agent_id_re_matches_bare_hex(self):
        assert identity._format_ok("abcdef123456") is True

    def test_canonical_agent_id_re_rejects_short(self):
        assert identity._format_ok("abcdef12345") is False

    def test_build_canonical_agent_id_requires_both_args(self):
        import pytest

        with pytest.raises(ValueError):
            identity.build_canonical_agent_id("", "abcdef12")
        with pytest.raises(ValueError):
            identity.build_canonical_agent_id("name", "")

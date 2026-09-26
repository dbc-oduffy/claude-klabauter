
from __future__ import annotations

import tempfile

import pytest

from coordinator_core.session import guard_unlock_sentinel as gus


@pytest.fixture(autouse=True)
def _isolated_tempdir(tmp_path, monkeypatch):
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    yield


SID = "sess-abc123"
GUARD = "block_dev_repo_sentinel_write"


class TestSentinelPathShape:
    def test_lives_under_the_resolved_tempdir(self, tmp_path):
        p = gus.sentinel_path(SID, GUARD)
        assert p.parent == tmp_path

    def test_keyed_on_both_session_and_guard(self):
        a = gus.sentinel_path(SID, GUARD)
        b = gus.sentinel_path(SID, "block_tracker_edit")
        c = gus.sentinel_path("some-other-session", GUARD)
        assert a != b
        assert a != c
        assert b != c

    def test_same_pair_resolves_to_the_same_path(self):
        assert gus.sentinel_path(SID, GUARD) == gus.sentinel_path(SID, GUARD)

    def test_underscore_run_in_a_component_does_not_collide_across_the_join(self):
        assert gus.sentinel_path("a", "b__c") != gus.sentinel_path("a__b", "c")

    def test_dot_in_a_component_does_not_collide_across_the_join(self):
        assert gus.sentinel_path("a", "b.c") != gus.sentinel_path("a.b", "c")


class TestPathTraversalShapedInputsSanitizeSafely:
    def test_dotdot_slash_session_id_does_not_escape_tempdir(self, tmp_path):
        p = gus.sentinel_path("../../etc/passwd", GUARD)
        assert p.parent == tmp_path
        assert ".." not in p.name
        assert "/" not in p.name

    def test_dotdot_slash_guard_name_does_not_escape_tempdir(self, tmp_path):
        p = gus.sentinel_path(SID, "../../../etc/shadow")
        assert p.parent == tmp_path
        assert ".." not in p.name
        assert "/" not in p.name

    def test_backslash_separator_sanitized(self, tmp_path):
        p = gus.sentinel_path("sess\\..\\..\\x", GUARD)
        assert p.parent == tmp_path
        assert "\\" not in p.name

    def test_null_byte_sanitized(self, tmp_path):
        p = gus.sentinel_path(SID + "\x00evil", GUARD)
        assert p.parent == tmp_path
        assert "\x00" not in p.name

    def test_empty_components_still_produce_a_single_file(self, tmp_path):
        p = gus.sentinel_path("", "")
        assert p.parent == tmp_path
        assert "/" not in p.name and "\\" not in p.name


class TestConsumeOneShotSemantics:
    def test_absent_sentinel_returns_false(self):
        assert gus.consume(SID, GUARD) is False

    def test_present_sentinel_returns_true_once(self):
        p = gus.sentinel_path(SID, GUARD)
        p.write_text("", encoding="utf-8")
        assert gus.consume(SID, GUARD) is True

    def test_second_consume_after_grant_returns_false(self):
        p = gus.sentinel_path(SID, GUARD)
        p.write_text("", encoding="utf-8")
        assert gus.consume(SID, GUARD) is True
        assert gus.consume(SID, GUARD) is False

    def test_grant_unlinks_the_file(self):
        p = gus.sentinel_path(SID, GUARD)
        p.write_text("", encoding="utf-8")
        gus.consume(SID, GUARD)
        assert not p.exists()

    def test_grant_for_one_guard_does_not_consume_a_peer_guard_sentinel(self):
        p_a = gus.sentinel_path(SID, "guard_a")
        p_a.write_text("", encoding="utf-8")
        assert gus.consume(SID, "guard_b") is False
        assert p_a.exists()

    def test_grant_for_one_session_does_not_consume_a_peer_session_sentinel(self):
        p_peer = gus.sentinel_path("peer-session", GUARD)
        p_peer.write_text("", encoding="utf-8")
        assert gus.consume(SID, GUARD) is False
        assert p_peer.exists()


class TestAnnotateDenyDoesNotNameACodename:

    def _fire(self, **kwargs):
        out = {"hookSpecificOutput": {"permissionDecisionReason": "denied: reason"}}
        return gus.annotate_deny(out, SID, GUARD, "doc-display-text", **kwargs)

    def test_no_doe_claude_codename(self):
        out = self._fire()
        reason = out["hookSpecificOutput"]["permissionDecisionReason"]
        assert "DoE-claude" not in reason

    def test_no_placeholder_codename(self):
        out = self._fire()
        reason = out["hookSpecificOutput"]["permissionDecisionReason"]
        assert "example-doctrine-repo" not in reason

    def test_doe_checkout_present_no_longer_changes_the_pointer(self, tmp_path, monkeypatch):
        import coordinator_core.doe_root_pointer as doe_root_pointer_mod

        doe_root = tmp_path / "doe-claude"
        (doe_root / "coordinator" / "docs" / "wiki").mkdir(parents=True)
        monkeypatch.setattr(
            doe_root_pointer_mod, "read_doe_root_pointer", lambda: str(doe_root)
        )
        out_with_checkout = self._fire()
        reason_with_checkout = out_with_checkout["hookSpecificOutput"]["permissionDecisionReason"]

        out_without_checkout = self._fire()
        reason_without_checkout = out_without_checkout["hookSpecificOutput"]["permissionDecisionReason"]

        assert reason_with_checkout == reason_without_checkout
        assert "DoE-claude" not in reason_with_checkout
        assert reason_with_checkout.startswith("denied: reason")


class TestAnnotateDenyDoesNotInlineTheUnlockRecipe:

    def _reason(self, **kwargs):
        out = {"hookSpecificOutput": {"permissionDecisionReason": "denied: reason"}}
        out = gus.annotate_deny(out, SID, GUARD, "doc-display-text", **kwargs)
        return out["hookSpecificOutput"]["permissionDecisionReason"]

    def test_filename_prefix_shape_is_absent(self):
        assert gus._SENTINEL_PREFIX not in self._reason()

    def test_session_id_is_not_rendered(self):
        assert SID not in self._reason()

    def test_guard_name_is_rendered(self):
        """INVERTED 2026-09-03 (item 11). The guard name was removed by item 7
        as one of TWO data points ("the two data points a filename-shape line
        would let them assemble into the same recipe by hand") -- name plus
        `session_id`, alongside the filename shape. It was never a recipe on
        its own, and the other two pieces stay removed (asserted by the
        siblings in this class), so nothing is assemblable from a bare name.

        It is restored because it is IDENTITY: `session.em_guard_grant`'s CLI
        takes the guard name as its first argument, so with the name rendered
        nowhere the EM-exercisable in-band route could not be invoked from the
        deny that triggered it."""
        assert GUARD in self._reason()

    def test_name_alone_does_not_assemble_the_recipe(self):
        reason = self._reason()
        assert GUARD in reason
        assert SID not in reason
        assert gus._SENTINEL_PREFIX not in reason

    def test_guard_reason_is_preserved_verbatim_as_the_prefix(self):
        assert self._reason().startswith("denied: reason")

    def test_assembled_sentinel_path_literal_is_not_rendered(self, tmp_path, monkeypatch):
        monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
        assembled = str(gus.sentinel_path(SID, GUARD))
        assert assembled not in self._reason()


class TestAnnotateDenyAgentIdSuppression:
    """AC-3 (2026-08-13, C3, item 8): the fast positive-subagent leg is
    unchanged, but the EM decision now routes through
    ``identity.resolves_em_audience`` and its inverted fail direction —
    only a positively-resolved EM audience emits; absence/malformed/
    exception all degrade to terse.

    REFRAMED 2026-09-03 (item 11). Item 10 deleted the identity resolution
    entirely, so since then these cases have differed in no observable way and
    the monkeypatched resolvers are not consulted at all. What the class now
    asserts is the property that outlived the mechanism and is the one worth
    keeping: AUDIENCE DOES NOT CHANGE THE RENDERED TEXT. A subagent, an EM, an
    unresolvable agent_id and a raising resolver all get the same guard NAME
    and no affordance beyond it — the subagent channel gains nothing it could
    act on, which is the whole point the suppression logic used to serve. The
    EM-only in-band route lives in `_write_bump_message.render_em_message`,
    which has its own audience split; it is never rendered here."""

    def _reason(self, **kwargs):
        out = {"hookSpecificOutput": {"permissionDecisionReason": "denied: reason"}}
        out = gus.annotate_deny(out, SID, GUARD, "doc-display-text", **kwargs)
        return out["hookSpecificOutput"]["permissionDecisionReason"]

    def test_resolved_subagent_suppresses_the_block(self, monkeypatch):
        import coordinator_core.session.identity as identity_mod

        monkeypatch.setattr(
            identity_mod, "resolve_subagent_identity", lambda agent_id, session_id: "some-agent"
        )
        reason = self._reason(agent_id="some-agent-id")
        assert reason == self._reason(agent_id="")

    def test_absent_agent_id_resolved_em_still_renders_nothing(self):
        reason = self._reason(agent_id="")
        assert reason.startswith("denied: reason")
        assert "human-only affordance" not in reason
        assert "doctrine violation" not in reason

    def test_malformed_agent_id_degrades_to_terse(self, monkeypatch):
        """AC-3 inversion: a present-but-unresolvable agent_id used to emit
        (old fail direction, item 5). It now degrades -- `resolves_em_audience`
        treats a non-empty raw `agent_id` as "cannot resolve", never as "no
        agent" (DECISIONS.md D1, "ABSENT VS UNRESOLVABLE")."""
        import coordinator_core.session.identity as identity_mod

        monkeypatch.setattr(
            identity_mod, "resolve_subagent_identity", lambda agent_id, session_id: ""
        )
        reason = self._reason(agent_id="not-a-real-agent-id")
        assert reason == self._reason(agent_id="")

    def test_exception_during_resolution_degrades_to_terse(self, monkeypatch):
        import coordinator_core.session.identity as identity_mod

        def _raise(agent_id, session_id):
            raise RuntimeError("boom")

        monkeypatch.setattr(identity_mod, "resolve_subagent_identity", _raise)
        reason = self._reason(agent_id="")
        assert reason.startswith("denied: reason")
        assert GUARD in reason


class TestConsumeNeverRaises:
    def test_vanished_file_between_check_and_unlink_does_not_raise(self, monkeypatch):
        from pathlib import Path

        def _raise_not_found(self):
            raise FileNotFoundError("vanished")

        monkeypatch.setattr(Path, "unlink", _raise_not_found)
        assert gus.consume(SID, GUARD) is False

    def test_permission_error_on_unlink_does_not_raise(self, monkeypatch):
        from pathlib import Path

        def _raise_permission(self):
            raise PermissionError("denied")

        monkeypatch.setattr(Path, "unlink", _raise_permission)
        assert gus.consume(SID, GUARD) is False

    def test_unexpected_exception_on_unlink_does_not_raise(self, monkeypatch):
        from pathlib import Path

        def _raise_generic(self):
            raise RuntimeError("boom")

        monkeypatch.setattr(Path, "unlink", _raise_generic)
        assert gus.consume(SID, GUARD) is False

    def test_unresolvable_sentinel_path_does_not_raise(self, monkeypatch):
        def _raise(session_id, guard_name):
            raise OSError("no temp dir")

        monkeypatch.setattr(gus, "sentinel_path", _raise)
        assert gus.consume(SID, GUARD) is False

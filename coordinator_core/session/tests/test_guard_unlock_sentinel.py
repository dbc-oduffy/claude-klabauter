"""Tests for coordinator_core.session.guard_unlock_sentinel — the
per-(session_id, guard_name) one-shot in-session operator unlock resolver.

Covers AC8's resolver-level slice: one-shot semantics, path-traversal-shaped
inputs sanitizing safely (never letting a separator through and never
escaping the temp directory), and that `consume()` never raises on any
failure mode. The engine-seam (write leg) and dispatcher-seam (bash leg)
coverage lives in `write_guards/tests/` and `bash_guards/tests/` respectively
— this file is resolver-only, does not import either engine.

Isolation discipline: `tempfile.gettempdir` is monkeypatched to `tmp_path`
for every test in this module (autouse fixture) so a failed test can never
leave a live unlock sentinel in the real platform temp dir.

Spec backlink: docs/plans/2026-08-03-in-session-operator-unlock-for-the-hard-.md § C1/C6.
"""

from __future__ import annotations

import tempfile

import pytest

from coordinator_core.session import guard_unlock_sentinel as gus


@pytest.fixture(autouse=True)
def _isolated_tempdir(tmp_path, monkeypatch):
    """Redirect every `sentinel_path()`/`consume()` call in this module to
    `tmp_path` instead of the real platform temp dir — a stray sentinel is
    real (if small) security residue on a developer machine."""
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
        """Regression for the `__`-join collision: a literal `_` survives
        `_sanitize_component` unmodified, so an underscore-based join was
        not a true delimiter — `("a", "b__c")` and `("a__b", "c")` both
        rendered to the same filename. `sentinel_path` now joins on `.`,
        which sanitization always strips from raw components, so distinct
        pairs can never collapse regardless of embedded underscores."""
        assert gus.sentinel_path("a", "b__c") != gus.sentinel_path("a__b", "c")

    def test_dot_in_a_component_does_not_collide_across_the_join(self):
        """A literal `.` in either raw component is sanitized away before
        the join, so it can never masquerade as the join separator."""
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
    """AC6/register: the rendered deny text names no private-repo codename —
    the example-doctrine-repo-source pointer branch is gone (§ EM ruling, branch B) and the
    settings-root pointer is unconditional and codename-free.

    UPDATED 2026-08-13 (C4d, docs/plans/2026-08-13-guard-messages-stop-
    handing-agents-the-keys.md AC-2, item 9 in `annotate_deny`'s
    docstring): `annotate_deny` no longer appends anything at all -- B8
    (`message_register._rules`, leg (d)) fires on ANY doc/wiki pointer into
    the override-key/unlock surface, so even the bare pointer sentence this
    class used to assert PRESENT is gone. These tests now assert the
    envelope is returned byte-identical instead."""

    def _fire(self, **kwargs):
        out = {"hookSpecificOutput": {"permissionDecisionReason": "denied: reason"}}
        return gus.annotate_deny(out, SID, GUARD, "doc-display-text", **kwargs)

    def test_no_example_doctrine_repo_codename(self):
        out = self._fire()
        reason = out["hookSpecificOutput"]["permissionDecisionReason"]
        assert "example-doctrine-repo" not in reason

    def test_no_placeholder_codename(self):
        out = self._fire()
        reason = out["hookSpecificOutput"]["permissionDecisionReason"]
        assert "example-doctrine-repo" not in reason

    def test_doe_checkout_present_no_longer_changes_the_pointer(self, tmp_path, monkeypatch):
        """The example-doctrine-repo-checkout-present branch is gone: presence of a example-doctrine-repo
        checkout on disk must not change the rendered text (now unchanged
        either way, per item 9)."""
        import coordinator_core.doe_root_pointer as doe_root_pointer_mod

        doe_root = tmp_path / "example-doctrine-repo"
        (doe_root / "coordinator" / "docs" / "wiki").mkdir(parents=True)
        monkeypatch.setattr(
            doe_root_pointer_mod, "read_doe_root_pointer", lambda: str(doe_root)
        )
        out_with_checkout = self._fire()
        reason_with_checkout = out_with_checkout["hookSpecificOutput"]["permissionDecisionReason"]

        out_without_checkout = self._fire()
        reason_without_checkout = out_without_checkout["hookSpecificOutput"]["permissionDecisionReason"]

        assert reason_with_checkout == reason_without_checkout
        assert reason_with_checkout == "denied: reason"


class TestAnnotateDenyDoesNotInlineTheUnlockRecipe:
    """AC-3/Task 1 revert (2026-08-13, C3): the 2026-08-12 regression
    re-inlined the sentinel filename shape, drop location, and per-firing
    session id/guard name into the rendered text. This class asserts the
    reverted (pre-regression) shape: none of the recipe pieces render.

    UPDATED 2026-08-13 (C4d, item 9): `test_doc_display_and_wiki_pointer_
    are_rendered` used to assert those pointers PRESENT -- B8 (leg (d))
    fires on that pointer sentence itself, so `annotate_deny` no longer
    renders anything at all; that test now asserts the envelope is
    returned byte-identical instead."""

    def _reason(self, **kwargs):
        out = {"hookSpecificOutput": {"permissionDecisionReason": "denied: reason"}}
        out = gus.annotate_deny(out, SID, GUARD, "doc-display-text", **kwargs)
        return out["hookSpecificOutput"]["permissionDecisionReason"]

    def test_filename_prefix_shape_is_absent(self):
        assert gus._SENTINEL_PREFIX not in self._reason()

    def test_session_id_is_not_rendered(self):
        assert SID not in self._reason()

    def test_guard_name_is_not_rendered(self):
        assert GUARD not in self._reason()

    def test_reason_returned_unchanged(self):
        assert self._reason() == "denied: reason"

    def test_assembled_sentinel_path_literal_is_not_rendered(self, tmp_path, monkeypatch):
        monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
        assembled = str(gus.sentinel_path(SID, GUARD))
        assert assembled not in self._reason()


class TestAnnotateDenyAgentIdSuppression:
    """AC-3 (2026-08-13, C3, item 8): the fast positive-subagent leg is
    unchanged, but the EM decision now routes through
    ``identity.resolves_em_audience`` and its inverted fail direction —
    only a positively-resolved EM audience emits; absence/malformed/
    exception all degrade to terse."""

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
        assert reason == "denied: reason"

    def test_absent_agent_id_resolved_em_still_renders_nothing(self):
        """Inverted 2026-08-13 (C4d, docs/plans/2026-08-13-guard-messages-
        stop-handing-agents-the-keys.md AC-2, item 9 in annotate_deny's
        docstring): a well-formed envelope (session_id present, agent_id
        absent) resolves as a positively-resolved EM audience under
        `resolves_em_audience` -- this used to be the condition that made
        the block EMIT. `annotate_deny` no longer has any render step left
        to gate (B8's leg (d) fires on even the bare doc/wiki pointer this
        block used to carry), so a resolved EM audience now gets the reason
        back byte-identical too, same as every other case."""
        reason = self._reason(agent_id="")
        assert reason == "denied: reason"
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
        assert reason == "denied: reason"

    def test_exception_during_resolution_degrades_to_terse(self, monkeypatch):
        """AC-3 inversion: the `except Exception: pass` branch used to fall
        through to emit (old fail direction, item 5's docstring names this
        exact branch). It now degrades: any exception during identity
        resolution returns `out` unchanged."""
        import coordinator_core.session.identity as identity_mod

        def _raise(agent_id, session_id):
            raise RuntimeError("boom")

        monkeypatch.setattr(identity_mod, "resolve_subagent_identity", _raise)
        reason = self._reason(agent_id="")
        assert reason == "denied: reason"


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

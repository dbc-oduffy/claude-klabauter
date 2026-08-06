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

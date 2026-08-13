"""
coordinator_core.session.tests.test_peer_roster — live peer roster test suite.

Spec backlink: `state/handoffs/2026-08-13-live-peer-roster.md` §§ 1-3, AC.

Every fixture is built via `monkeypatch.setattr(harness_registry, ...)` --
this suite never reads the operator's real `~/.claude/sessions` (mirrors
`session/tests/test_reachability.py`'s own discipline).
"""

from __future__ import annotations

from coordinator_core.session import harness_registry as hr
from coordinator_core.session import peer_roster


def _record(
    name,
    socket,
    cwd="/repo",
    pid=1,
    start_epoch=1000.0,
    status=None,
):
    return hr.RegistryRecord(
        pid=pid,
        start_epoch=start_epoch,
        cwd=cwd,
        name=name,
        messaging_socket_path=socket,
        status=status,
    )


class TestCwdFiltering:
    def test_default_filters_to_current_repo_via_getcwd(self, monkeypatch):
        snap = {
            "sid-a": _record("claude-klabauter-57", "/sock/a.sock", cwd="/repo/claude-klabauter"),
            "sid-b": _record("other-12", "/sock/b.sock", cwd="/repo/other"),
        }
        monkeypatch.setattr(hr, "snapshot", lambda: snap)
        monkeypatch.setattr(hr, "self_record", lambda: None)
        monkeypatch.setattr(peer_roster.os, "getcwd", lambda: "/repo/claude-klabauter")

        rows = peer_roster.build_roster()
        assert {r.session_id for r in rows} == {"sid-a"}

    def test_named_sibling_repo_filters_correctly(self, monkeypatch):
        snap = {
            "sid-a": _record("claude-klabauter-57", "/sock/a.sock", cwd="/repo/claude-klabauter"),
            "sid-b": _record("other-12", "/sock/b.sock", cwd="/repo/coordinator-claude"),
        }
        monkeypatch.setattr(hr, "snapshot", lambda: snap)
        monkeypatch.setattr(hr, "self_record", lambda: None)

        rows = peer_roster.build_roster("/repo/coordinator-claude")
        assert {r.session_id for r in rows} == {"sid-b"}

    def test_subdirectory_cwd_is_contained(self, monkeypatch):
        snap = {
            "sid-a": _record("claude-klabauter-57", "/sock/a.sock", cwd="/repo/claude-klabauter/sub/dir"),
        }
        monkeypatch.setattr(hr, "snapshot", lambda: snap)
        monkeypatch.setattr(hr, "self_record", lambda: None)

        rows = peer_roster.build_roster("/repo/claude-klabauter")
        assert {r.session_id for r in rows} == {"sid-a"}

    def test_sibling_prefix_that_is_not_a_real_subdirectory_is_excluded(self, monkeypatch):
        # "/repo/claude-klabauter-other" shares the string prefix "/repo/claude-klabauter" but is
        # NOT a subdirectory of "/repo/claude-klabauter" -- containment must be
        # path-separator-aware, not a bare string startswith.
        snap = {
            "sid-a": _record("other", "/sock/a.sock", cwd="/repo/claude-klabauter-other"),
        }
        monkeypatch.setattr(hr, "snapshot", lambda: snap)
        monkeypatch.setattr(hr, "self_record", lambda: None)

        rows = peer_roster.build_roster("/repo/claude-klabauter")
        assert rows == []

    def test_symlinked_cwd_resolves_to_the_same_repo_as_a_real_root(
        self, monkeypatch, tmp_path
    ):
        # P2 (Review: code-reviewer): a harness-reported `cwd` reached
        # through a symlink (e.g. macOS `/tmp` -> `/private/tmp`) must still
        # match a `repo_root` given as the resolved real path -- realpath
        # must run on BOTH sides, or a live peer silently drops off the
        # roster.
        real_root = tmp_path / "real_repo"
        real_root.mkdir()
        link_root = tmp_path / "link_repo"
        link_root.symlink_to(real_root)

        snap = {
            "sid-a": _record("claude-klabauter-57", "/sock/a.sock", cwd=str(link_root)),
        }
        monkeypatch.setattr(hr, "snapshot", lambda: snap)
        monkeypatch.setattr(hr, "self_record", lambda: None)

        rows = peer_roster.build_roster(str(real_root))
        assert {r.session_id for r in rows} == {"sid-a"}

    def test_missing_cwd_is_excluded_never_raises(self, monkeypatch):
        snap = {
            "sid-a": _record(None, None, cwd=None),
        }
        monkeypatch.setattr(hr, "snapshot", lambda: snap)
        monkeypatch.setattr(hr, "self_record", lambda: None)

        rows = peer_roster.build_roster("/repo/claude-klabauter")
        assert rows == []


class TestRefAddressStabilityUnderFiltering:
    def test_filtered_out_session_still_widens_the_kept_sessions_ref(self, monkeypatch):
        # AC "Critical": ref/name-collision must be computed over the WHOLE
        # snapshot, never a filtered subset. Two sessions named identically
        # ("claude-klabauter-89") live in DIFFERENT repos -- filtering must not change
        # the surviving row's ref/address relative to what an unfiltered
        # caller would see for that same session id.
        snap = {
            "sid-in": _record("claude-klabauter-89", "/sock/in.sock", cwd="/repo/claude-klabauter"),
            "sid-out": _record("claude-klabauter-89", "/sock/out.sock", cwd="/repo/other"),
        }
        monkeypatch.setattr(hr, "snapshot", lambda: snap)
        monkeypatch.setattr(hr, "self_record", lambda: None)

        from coordinator_core.session import reachability

        unfiltered_candidates = {
            c.session_id: c for c in reachability.resolve_candidates(snap)
        }

        rows = peer_roster.build_roster("/repo/claude-klabauter")
        assert {r.session_id for r in rows} == {"sid-in"}
        row = rows[0]

        # Filtering must not have changed the address relative to the
        # unfiltered resolution -- both must show the widened, ref-qualified
        # form (the bare name collides in the WHOLE snapshot).
        assert row.address == unfiltered_candidates["sid-in"].address
        assert row.address.startswith("claude-klabauter-89 [")

    def test_ref_widening_survives_the_filtered_out_peer_being_absent_from_output(
        self, monkeypatch
    ):
        snap = {
            "sid-in": _record("claude-klabauter-89", "/sock/in.sock", cwd="/repo/claude-klabauter"),
            "sid-out": _record("claude-klabauter-89", "/sock/out.sock", cwd="/repo/other"),
        }
        monkeypatch.setattr(hr, "snapshot", lambda: snap)
        monkeypatch.setattr(hr, "self_record", lambda: None)

        rows = peer_roster.build_roster("/repo/claude-klabauter")
        assert len(rows) == 1
        # The filtered-out session never appears as a row, but the surviving
        # row still carries the qualifier its (invisible) collision forced.
        assert rows[0].session_id == "sid-in"
        assert "[" in rows[0].address


class TestSelfRow:
    def test_self_row_marked_explicitly(self, monkeypatch):
        snap = {
            "self-sid": _record("claude-klabauter-84", "/sock/self.sock", cwd="/repo/claude-klabauter"),
            "peer-sid": _record("claude-klabauter-99", "/sock/peer.sock", cwd="/repo/claude-klabauter"),
        }
        monkeypatch.setattr(hr, "snapshot", lambda: snap)
        monkeypatch.setattr(hr, "self_record", lambda: ("self-sid", snap["self-sid"]))

        rows = peer_roster.build_roster("/repo/claude-klabauter")
        by_id = {r.session_id: r for r in rows}
        assert by_id["self-sid"].is_self is True
        assert by_id["peer-sid"].is_self is False

    def test_no_self_record_marks_nothing_self(self, monkeypatch):
        snap = {
            "sid-a": _record("claude-klabauter-57", "/sock/a.sock", cwd="/repo/claude-klabauter"),
        }
        monkeypatch.setattr(hr, "snapshot", lambda: snap)
        monkeypatch.setattr(hr, "self_record", lambda: None)
        monkeypatch.delenv("CLAUDE_CODE_MESSAGING_SOCKET", raising=False)

        rows = peer_roster.build_roster("/repo/claude-klabauter")
        assert rows[0].is_self is False
        assert rows[0].self_determination == "unresolved"

    def test_self_record_success_marks_resolved(self, monkeypatch):
        snap = {
            "self-sid": _record("claude-klabauter-84", "/sock/self.sock", cwd="/repo/claude-klabauter"),
            "peer-sid": _record("claude-klabauter-99", "/sock/peer.sock", cwd="/repo/claude-klabauter"),
        }
        monkeypatch.setattr(hr, "snapshot", lambda: snap)
        monkeypatch.setattr(hr, "self_record", lambda: ("self-sid", snap["self-sid"]))

        rows = peer_roster.build_roster("/repo/claude-klabauter")
        by_id = {r.session_id: r for r in rows}
        assert by_id["self-sid"].is_self is True
        assert all(r.self_determination == "resolved" for r in rows)

    def test_self_record_none_falls_back_to_messaging_socket_env_match(
        self, monkeypatch
    ):
        # The reproduced live defect: `self_record()` declines (e.g. the
        # pid resolver's `env-miss:name-mismatch` leg) even though
        # `CLAUDE_PID` was correct -- the roster must still find `self` via
        # the second, independent signal `reachability._socket_env_self_match`
        # uses, exactly mirroring `reachability.resolve_address`'s own
        # fallback.
        snap = {
            "self-sid": _record("claude-klabauter-84", "/sock/self.sock", cwd="/repo/claude-klabauter"),
            "peer-sid": _record("claude-klabauter-99", "/sock/peer.sock", cwd="/repo/claude-klabauter"),
        }
        monkeypatch.setattr(hr, "snapshot", lambda: snap)
        monkeypatch.setattr(hr, "self_record", lambda: None)
        monkeypatch.setenv("CLAUDE_CODE_MESSAGING_SOCKET", "/sock/self.sock")

        rows = peer_roster.build_roster("/repo/claude-klabauter")
        by_id = {r.session_id: r for r in rows}
        assert by_id["self-sid"].is_self is True
        assert by_id["peer-sid"].is_self is False
        assert all(r.self_determination == "resolved" for r in rows)

    def test_both_self_signals_absent_marks_unresolved_not_silently_no_self(
        self, monkeypatch
    ):
        snap = {
            "sid-a": _record("claude-klabauter-57", "/sock/a.sock", cwd="/repo/claude-klabauter"),
        }
        monkeypatch.setattr(hr, "snapshot", lambda: snap)
        monkeypatch.setattr(hr, "self_record", lambda: None)
        monkeypatch.delenv("CLAUDE_CODE_MESSAGING_SOCKET", raising=False)

        rows = peer_roster.build_roster("/repo/claude-klabauter")
        assert rows[0].is_self is False
        assert rows[0].self_determination == "unresolved"

    def test_self_socket_env_empty_string_never_matches_none_socket(
        self, monkeypatch
    ):
        # Negative-spec parity with `reachability._socket_env_self_match`:
        # an empty/missing env var must never coincidentally match a record
        # whose `messaging_socket_path` is also falsy/None.
        snap = {
            "sid-a": _record(None, None, cwd="/repo/claude-klabauter"),
        }
        monkeypatch.setattr(hr, "snapshot", lambda: snap)
        monkeypatch.setattr(hr, "self_record", lambda: None)
        monkeypatch.delenv("CLAUDE_CODE_MESSAGING_SOCKET", raising=False)

        rows = peer_roster.build_roster("/repo/claude-klabauter")
        assert rows[0].is_self is False
        assert rows[0].self_determination == "unresolved"


class TestDegradedAddress:
    def test_missing_name_or_socket_degrades_to_none_address_never_bare_uuid(
        self, monkeypatch
    ):
        snap = {
            "sid-a": _record(None, None, cwd="/repo/claude-klabauter"),
        }
        monkeypatch.setattr(hr, "snapshot", lambda: snap)
        monkeypatch.setattr(hr, "self_record", lambda: None)

        rows = peer_roster.build_roster("/repo/claude-klabauter")
        assert len(rows) == 1
        assert rows[0].address is None
        assert rows[0].address != "sid-a"


class TestEmptyOrAbsentRegistry:
    def test_empty_snapshot_yields_empty_roster_no_raise(self, monkeypatch):
        monkeypatch.setattr(hr, "snapshot", lambda: {})
        monkeypatch.setattr(hr, "self_record", lambda: None)

        assert peer_roster.build_roster("/repo/claude-klabauter") == []

    def test_snapshot_exception_degrades_to_empty_roster(self, monkeypatch):
        def _raise():
            raise RuntimeError("boom")

        monkeypatch.setattr(hr, "snapshot", _raise)
        monkeypatch.setattr(hr, "self_record", lambda: None)

        assert peer_roster.build_roster("/repo/claude-klabauter") == []

    def test_self_record_exception_still_returns_rows(self, monkeypatch):
        snap = {
            "sid-a": _record("claude-klabauter-57", "/sock/a.sock", cwd="/repo/claude-klabauter"),
        }

        def _raise():
            raise RuntimeError("boom")

        monkeypatch.setattr(hr, "snapshot", lambda: snap)
        monkeypatch.setattr(hr, "self_record", _raise)
        monkeypatch.delenv("CLAUDE_CODE_MESSAGING_SOCKET", raising=False)

        rows = peer_roster.build_roster("/repo/claude-klabauter")
        assert len(rows) == 1
        assert rows[0].is_self is False
        assert rows[0].self_determination == "unresolved"


class TestStatusAndRunningSeconds:
    def test_status_parsed_display_only(self, monkeypatch):
        snap = {
            "sid-a": _record("claude-klabauter-57", "/sock/a.sock", cwd="/repo/claude-klabauter", status="busy"),
        }
        monkeypatch.setattr(hr, "snapshot", lambda: snap)
        monkeypatch.setattr(hr, "self_record", lambda: None)

        rows = peer_roster.build_roster("/repo/claude-klabauter")
        assert rows[0].status == "busy"

    def test_missing_status_is_none(self, monkeypatch):
        snap = {
            "sid-a": _record("claude-klabauter-57", "/sock/a.sock", cwd="/repo/claude-klabauter"),
        }
        monkeypatch.setattr(hr, "snapshot", lambda: snap)
        monkeypatch.setattr(hr, "self_record", lambda: None)

        rows = peer_roster.build_roster("/repo/claude-klabauter")
        assert rows[0].status is None

    def test_running_seconds_is_a_plain_number(self, monkeypatch):
        now = 2000.0
        snap = {
            "sid-a": _record(
                "claude-klabauter-57", "/sock/a.sock", cwd="/repo/claude-klabauter", start_epoch=now - 120
            ),
        }
        monkeypatch.setattr(hr, "snapshot", lambda: snap)
        monkeypatch.setattr(hr, "self_record", lambda: None)
        monkeypatch.setattr(peer_roster.time, "time", lambda: now)

        rows = peer_roster.build_roster("/repo/claude-klabauter")
        assert rows[0].running_seconds == 120.0

"""
coordinator_core.session.tests.test_reachability — resolver test suite.

Spec backlink: `state/handoffs/2026-08-13-session-owner-reachability-registry.md` § 1/§ 4

Every fixture is built via `monkeypatch.setattr(harness_registry, ...)` --
this suite never reads the operator's real `~/.claude/sessions` (mirrors
`session/tests/test_harness_registry.py`'s own discipline).
"""

from __future__ import annotations

import hashlib

from coordinator_core.session import harness_registry as hr
from coordinator_core.session import reachability


def _record(name, socket, cwd="/repo", pid=1, start_epoch=1000.0):
    return hr.RegistryRecord(
        pid=pid,
        start_epoch=start_epoch,
        cwd=cwd,
        name=name,
        messaging_socket_path=socket,
    )


def _full12(socket: str) -> str:
    return hashlib.sha256(("session:" + socket).encode("utf-8")).hexdigest()[:12]


class TestReachableOutcome:
    def test_unique_name_still_resolves_with_ref_qualified_address(self, monkeypatch):
        # The harness refuses a bare name for a cross-session SendMessage
        # target even when it is uniquely named among live peers -- measured
        # live 2026-08-13 (ListAgents showed one row named
        # "claude-klabauter-87" among 40 peers, no collision, and the harness
        # still demanded the ref-qualified form). Ref-qualification is
        # therefore unconditional, not collision-gated.
        socket = "/tmp/cc-socks/57557.sock"
        snap = {
            "sid-a": _record("claude-klabauter-57", socket),
        }
        monkeypatch.setattr(hr, "snapshot", lambda: snap)
        monkeypatch.setattr(hr, "self_record", lambda: None)

        result = reachability.resolve_address("sid-a")
        assert result.outcome == "reachable"
        assert result.session_id == "sid-a"
        expected_ref = _full12(socket)[:6]
        assert result.address == f"claude-klabauter-57 [{expected_ref}]"

    def test_shared_name_appends_widened_ref(self, monkeypatch):
        snap = {
            "sid-a": _record("claude-klabauter-89", "/tmp/cc-socks/aaa.sock"),
            "sid-b": _record("claude-klabauter-89", "/tmp/cc-socks/bbb.sock"),
        }
        monkeypatch.setattr(hr, "snapshot", lambda: snap)
        monkeypatch.setattr(hr, "self_record", lambda: None)

        result_a = reachability.resolve_address("sid-a")
        result_b = reachability.resolve_address("sid-b")
        assert result_a.outcome == "reachable"
        assert result_b.outcome == "reachable"
        assert result_a.address.startswith("claude-klabauter-89 [")
        assert result_b.address.startswith("claude-klabauter-89 [")
        assert result_a.address != result_b.address

        ref_a = result_a.address.split("[")[1].rstrip("]")
        ref_b = result_b.address.split("[")[1].rstrip("]")
        full_a = _full12("/tmp/cc-socks/aaa.sock")
        full_b = _full12("/tmp/cc-socks/bbb.sock")
        assert full_a.startswith(ref_a)
        assert full_b.startswith(ref_b)


class TestRefWideningLoop:
    def test_widens_past_six_hex_chars_on_prefix_collision(self, monkeypatch):
        # Review: code-reviewer -- P2, replaces a probe-and-`pytest.skip`
        # search for a real sha256 collision with a monkeypatched
        # `_full_hash12` carrying a HARDCODED 6-hex-prefix collision, so the
        # widening branch is deterministically exercised on every run and
        # this test can never silently skip.
        full_a = "aaaaaa000000"
        full_b = "aaaaaa111111"
        monkeypatch.setattr(
            reachability,
            "_full_hash12",
            lambda socket: {"socket-a": full_a, "socket-b": full_b}[socket],
        )
        assert reachability._full_hash12("socket-a") == full_a
        assert reachability._full_hash12("socket-b") == full_b

        ref_a = reachability._widen_ref(full_a, [full_b])
        ref_b = reachability._widen_ref(full_b, [full_a])
        assert len(ref_a) > 6 or len(ref_b) > 6 or ref_a != ref_b
        assert full_a.startswith(ref_a)
        assert full_b.startswith(ref_b)
        assert ref_a != ref_b or full_a[: len(ref_a)] != full_b[: len(ref_a)]


class TestNotReachable:
    def test_no_matching_record_is_not_reachable(self, monkeypatch):
        monkeypatch.setattr(hr, "snapshot", lambda: {})
        monkeypatch.setattr(hr, "self_record", lambda: None)

        result = reachability.resolve_address("no-such-session")
        assert result.outcome == "not_reachable"
        assert result.address is None

    def test_empty_owner_id_is_not_reachable(self, monkeypatch):
        monkeypatch.setattr(hr, "snapshot", lambda: {})
        monkeypatch.setattr(hr, "self_record", lambda: None)

        assert reachability.resolve_address("").outcome == "not_reachable"

    def test_record_missing_name_or_socket_degrades_not_reachable(self, monkeypatch):
        snap = {"sid-a": _record(None, None)}
        monkeypatch.setattr(hr, "snapshot", lambda: snap)
        monkeypatch.setattr(hr, "self_record", lambda: None)

        result = reachability.resolve_address("sid-a")
        assert result.outcome == "not_reachable"


class TestAmbiguousContractShape:
    """Prefix matching was removed 2026-08-13 (never in spec; § 1's
    governing criterion is "accepts owner ids in every recording
    convention already in the tree", all of which record full UUIDs).
    `harness_registry.snapshot()` is `sessionId`-keyed and de-duplicates
    same-`sessionId` files at parse time (its own docstring: "the later
    one in `Path.glob`'s OS-dependent iteration order wins"), so an
    exact-match lookup can never yield more than one candidate through
    this seam today. These tests construct the `ambiguous` shape directly
    against `_resolve_one`/`Candidate`, pinning the outcome's documented
    contract rather than an input that can no longer produce it live --
    see `resolve_address`'s own docstring negative-spec.
    """

    def test_unresolvable_candidate_address_is_none_not_the_raw_uuid(self, monkeypatch):
        # Review: code-reviewer -- P3, a candidate lacking a usable
        # name/socket must never surface its raw session id as `.address`,
        # which would print as though it were a real SendMessage address.
        snap = {
            "5d3d5763-aaaa": _record("claude-klabauter-a1", "/sock/a.sock"),
            "5d3d5763-bbbb": _record(None, None),
        }
        matches = sorted(snap)
        candidates = [
            reachability._resolve_one(sid, snap)
            or reachability.Candidate(sid, "", "", None)
            for sid in matches
        ]
        result = reachability.ResolveResult(outcome="ambiguous", candidates=candidates)
        by_id = {c.session_id: c for c in result.candidates}
        assert by_id["5d3d5763-bbbb"].address is None
        expected_ref = _full12("/sock/a.sock")[:6]
        assert by_id["5d3d5763-aaaa"].address == f"claude-klabauter-a1 [{expected_ref}]"

    def test_unresolvable_candidate_name_and_ref_are_empty_string_not_none(
        self, monkeypatch
    ):
        # Review: review-integrator -- P3, pins Candidate's stated contract:
        # `name`/`ref` are "" (not None) for the same unresolvable slot whose
        # `.address` is None -- the two fields signal differently on purpose.
        snap = {
            "5d3d5763-aaaa": _record("claude-klabauter-a1", "/sock/a.sock"),
            "5d3d5763-bbbb": _record(None, None),
        }
        matches = sorted(snap)
        candidates = [
            reachability._resolve_one(sid, snap)
            or reachability.Candidate(sid, "", "", None)
            for sid in matches
        ]
        by_id = {c.session_id: c for c in candidates}
        unresolvable = by_id["5d3d5763-bbbb"]
        assert unresolvable.name == ""
        assert unresolvable.ref == ""
        assert unresolvable.address is None


class TestPrefixRemoved:
    def test_short_prefix_no_longer_matches_is_not_reachable(self, monkeypatch):
        snap = {
            "5d3d5763-aaaa": _record("claude-klabauter-a1", "/sock/a.sock"),
        }
        monkeypatch.setattr(hr, "snapshot", lambda: snap)
        monkeypatch.setattr(hr, "self_record", lambda: None)

        result = reachability.resolve_address("5d3d5763")
        assert result.outcome == "not_reachable"
        assert result.address is None

    def test_exact_full_uuid_match_still_resolves(self, monkeypatch):
        snap = {
            "abc123": _record("claude-klabauter-e1", "/sock/e.sock"),
            "abc123-longer-id": _record("claude-klabauter-e2", "/sock/f.sock"),
        }
        monkeypatch.setattr(hr, "snapshot", lambda: snap)
        monkeypatch.setattr(hr, "self_record", lambda: None)

        result = reachability.resolve_address("abc123")
        assert result.outcome == "reachable"
        assert result.session_id == "abc123"


class TestOwnSession:
    def test_own_session_id_is_distinguished_from_not_reachable(self, monkeypatch):
        snap = {
            "self-sid": _record("claude-klabauter-84", "/sock/self.sock"),
        }
        monkeypatch.setattr(hr, "snapshot", lambda: snap)
        monkeypatch.setattr(hr, "self_record", lambda: ("self-sid", snap["self-sid"]))

        result = reachability.resolve_address("self-sid")
        assert result.outcome == "own_session"
        assert result.session_id == "self-sid"
        assert result.address is None

    def test_socket_env_match_classifies_own_session_when_self_record_declines(
        self, monkeypatch
    ):
        # Regression test for the measured defect: self_record() declines
        # (e.g. CLAUDE_PID env-miss:name-mismatch on a correct pid) but the
        # socket env var matches the resolved record's own socket -- must
        # still classify own_session, not silently degrade to reachable.
        snap = {
            "self-sid": _record("claude-klabauter-84", "/sock/self.sock"),
        }
        monkeypatch.setattr(hr, "snapshot", lambda: snap)
        monkeypatch.setattr(hr, "self_record", lambda: None)
        monkeypatch.setenv("CLAUDE_CODE_MESSAGING_SOCKET", "/sock/self.sock")

        result = reachability.resolve_address("self-sid")
        assert result.outcome == "own_session"
        assert result.session_id == "self-sid"
        assert result.address is None

    def test_socket_env_unset_and_self_record_none_stays_reachable(self, monkeypatch):
        snap = {
            "sid-a": _record("claude-klabauter-57", "/sock/a.sock"),
        }
        monkeypatch.setattr(hr, "snapshot", lambda: snap)
        monkeypatch.setattr(hr, "self_record", lambda: None)
        monkeypatch.delenv("CLAUDE_CODE_MESSAGING_SOCKET", raising=False)

        result = reachability.resolve_address("sid-a")
        assert result.outcome == "reachable"
        assert result.session_id == "sid-a"

    def test_none_socket_record_and_unset_env_never_match(self, monkeypatch):
        # The None == None trap: a record with no messaging_socket_path and
        # an unset env var must NOT be classified own_session.
        snap = {
            "sid-a": _record("claude-klabauter-57", None),
        }
        monkeypatch.setattr(hr, "snapshot", lambda: snap)
        monkeypatch.setattr(hr, "self_record", lambda: None)
        monkeypatch.delenv("CLAUDE_CODE_MESSAGING_SOCKET", raising=False)

        result = reachability.resolve_address("sid-a")
        assert result.outcome == "not_reachable"

    def test_socket_env_set_but_differs_stays_reachable(self, monkeypatch):
        snap = {
            "sid-a": _record("claude-klabauter-57", "/sock/a.sock"),
        }
        monkeypatch.setattr(hr, "snapshot", lambda: snap)
        monkeypatch.setattr(hr, "self_record", lambda: None)
        monkeypatch.setenv("CLAUDE_CODE_MESSAGING_SOCKET", "/sock/other.sock")

        result = reachability.resolve_address("sid-a")
        assert result.outcome == "reachable"
        assert result.session_id == "sid-a"


class TestDegradedRegistrySources:
    def test_missing_sessions_dir_degrades_not_reachable(self, monkeypatch, tmp_path):
        missing = tmp_path / "does-not-exist"
        monkeypatch.setattr(hr, "registry_dir", lambda: missing)
        monkeypatch.setattr(
            "coordinator_core.session.core._resolve_claude_pid_from_env",
            lambda: (None, "env-miss:absent"),
        )

        result = reachability.resolve_address("anything")
        assert result.outcome == "not_reachable"

    def test_unreadable_and_malformed_records_are_skipped_not_fatal(self, monkeypatch, tmp_path):
        import json
        import time

        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()
        (sessions_dir / "broken.json").write_text("{not valid json", encoding="utf-8")
        good_epoch_ticks = int((time.time() - 60 + 11644473600.0) * 1e7)
        payload = {
            "sessionId": "good-sid",
            "pid": 42,
            "procStart": good_epoch_ticks,
            "name": "claude-klabauter-99",
            "messagingSocketPath": "/sock/good.sock",
        }
        (sessions_dir / "42.json").write_text(json.dumps(payload), encoding="utf-8")
        monkeypatch.setattr(hr, "registry_dir", lambda: sessions_dir)
        monkeypatch.setattr(
            "coordinator_core.session.core._resolve_claude_pid_from_env",
            lambda: (None, "env-miss:absent"),
        )

        result = reachability.resolve_address("good-sid")
        assert result.outcome == "reachable"
        expected_ref = _full12("/sock/good.sock")[:6]
        assert result.address == f"claude-klabauter-99 [{expected_ref}]"


class TestResolveCandidates:
    """2026-08-13 live-peer-roster § 2: the public seam
    `resolve_candidates()`, built for `coordinator_core.session.peer_roster`
    -- must not change `resolve_address`'s own behavior (covered by every
    other class in this file, unmodified)."""

    def test_resolves_every_live_session_in_the_snapshot(self, monkeypatch):
        snap = {
            "sid-a": _record("claude-klabauter-57", "/sock/a.sock"),
            "sid-b": _record("claude-klabauter-89", "/sock/b.sock"),
        }
        monkeypatch.setattr(hr, "snapshot", lambda: snap)

        candidates = reachability.resolve_candidates(snap)
        assert {c.session_id for c in candidates} == {"sid-a", "sid-b"}

    def test_omits_a_session_lacking_usable_name_or_socket(self, monkeypatch):
        snap = {
            "sid-a": _record("claude-klabauter-57", "/sock/a.sock"),
            "sid-b": _record(None, None),
        }
        monkeypatch.setattr(hr, "snapshot", lambda: snap)

        candidates = reachability.resolve_candidates(snap)
        assert {c.session_id for c in candidates} == {"sid-a"}

    def test_matches_resolve_address_for_the_same_session(self, monkeypatch):
        snap = {
            "sid-a": _record("claude-klabauter-89", "/sock/aaa.sock"),
            "sid-b": _record("claude-klabauter-89", "/sock/bbb.sock"),
        }
        monkeypatch.setattr(hr, "snapshot", lambda: snap)
        monkeypatch.setattr(hr, "self_record", lambda: None)

        candidates = {c.session_id: c for c in reachability.resolve_candidates(snap)}
        resolved = reachability.resolve_address("sid-a")

        assert resolved.outcome == "reachable"
        assert candidates["sid-a"].address == resolved.address


class TestReplayTodayCase:
    """§ Acceptance criteria: "the today-case is replayed end to end: given
    the claim-release baton's `claimed_by`, the resolver returns an address
    with no human disambiguation"."""

    def test_claimed_by_uuid_resolves_to_one_address(self, monkeypatch):
        claimed_by = "5739c815-7df8-4798-baab-5caa9c19a2d5"
        snap = {
            claimed_by: _record("claude-klabauter-d8", "/sock/d8.sock"),
        }
        monkeypatch.setattr(hr, "snapshot", lambda: snap)
        monkeypatch.setattr(hr, "self_record", lambda: None)

        result = reachability.resolve_address(claimed_by)
        assert result.outcome == "reachable"
        expected_ref = _full12("/sock/d8.sock")[:6]
        assert result.address == f"claude-klabauter-d8 [{expected_ref}]"
        assert result.candidates == []

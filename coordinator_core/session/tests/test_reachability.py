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


class TestResolveAdvisoryAddress:
    """`resolve_advisory_address` — the shared bare-string resolution core
    both `baton_assemble` and `pickup_assemble` format on top of."""

    def test_reachable_returns_bare_address(self, monkeypatch):
        snap = {"sid-a": _record("claude-klabauter-57", "/sock/a.sock")}
        monkeypatch.setattr(hr, "snapshot", lambda: snap)
        monkeypatch.setattr(hr, "self_record", lambda: None)

        expected_ref = _full12("/sock/a.sock")[:6]
        assert reachability.resolve_advisory_address("sid-a") == (
            f"claude-klabauter-57 [{expected_ref}]"
        )

    def test_own_session_returns_marker_string(self, monkeypatch):
        snap = {"self-sid": _record("claude-klabauter-84", "/sock/self.sock")}
        monkeypatch.setattr(hr, "snapshot", lambda: snap)
        monkeypatch.setattr(hr, "self_record", lambda: ("self-sid", snap["self-sid"]))

        assert reachability.resolve_advisory_address("self-sid") == "<this session>"

    def test_not_reachable_returns_empty_string(self, monkeypatch):
        monkeypatch.setattr(hr, "snapshot", lambda: {})
        monkeypatch.setattr(hr, "self_record", lambda: None)

        assert reachability.resolve_advisory_address("no-such-session") == ""

    def test_falsy_session_id_returns_empty_string_without_a_lookup(self, monkeypatch):
        def _boom():
            raise AssertionError("must not query the registry for a falsy id")

        monkeypatch.setattr(hr, "snapshot", _boom)
        assert reachability.resolve_advisory_address(None) == ""
        assert reachability.resolve_advisory_address("") == ""


class TestResolveAddressesBulk:
    """`resolve_addresses_bulk` — one snapshot for the whole roster, per
    `pickup_assemble.compute_competing_claim`'s performance requirement."""

    def test_resolves_every_id_off_one_snapshot_call(self, monkeypatch):
        snap = {
            "sid-a": _record("claude-klabauter-57", "/sock/a.sock"),
            "sid-b": _record("claude-klabauter-89", "/sock/b.sock"),
        }
        calls = {"n": 0}

        def _snapshot():
            calls["n"] += 1
            return snap

        monkeypatch.setattr(hr, "snapshot", _snapshot)
        monkeypatch.setattr(hr, "self_record", lambda: None)

        result = reachability.resolve_addresses_bulk(["sid-a", "sid-b", "sid-a"])
        assert calls["n"] == 1
        expected_a = f"claude-klabauter-57 [{_full12('/sock/a.sock')[:6]}]"
        expected_b = f"claude-klabauter-89 [{_full12('/sock/b.sock')[:6]}]"
        assert result == {"sid-a": expected_a, "sid-b": expected_b}

    def test_unresolvable_and_absent_ids_map_to_empty_string(self, monkeypatch):
        snap = {"sid-a": _record("claude-klabauter-57", "/sock/a.sock")}
        monkeypatch.setattr(hr, "snapshot", lambda: snap)
        monkeypatch.setattr(hr, "self_record", lambda: None)

        result = reachability.resolve_addresses_bulk(["sid-a", "sid-missing"])
        assert result["sid-missing"] == ""

    def test_self_session_id_maps_to_marker_string(self, monkeypatch):
        snap = {"self-sid": _record("claude-klabauter-84", "/sock/self.sock")}
        monkeypatch.setattr(hr, "snapshot", lambda: snap)
        monkeypatch.setattr(hr, "self_record", lambda: ("self-sid", snap["self-sid"]))

        result = reachability.resolve_addresses_bulk(["self-sid"])
        assert result["self-sid"] == "<this session>"

    def test_empty_input_list_returns_empty_dict(self, monkeypatch):
        monkeypatch.setattr(hr, "snapshot", lambda: {})
        monkeypatch.setattr(hr, "self_record", lambda: None)

        assert reachability.resolve_addresses_bulk([]) == {}


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


class TestNotReachableReasonIsNamed:
    """The `not_reachable` arm must not collapse "no such live session"
    into "this harness cannot address anyone".

    Measured live 2026-08-14 (Claude Code 2.1.232, Windows): 44/44
    `<claude-config>/sessions/*.json` records omit `messagingSocketPath`
    because the harness's cross-session-inbox gate is off, so every peer
    resolved to `not_reachable` with no way for a caller to tell that
    apart from a dead/absent session. These pin the distinction, not the
    gate's current state -- each fixture builds the registry shape it
    asserts about.
    """

    def test_live_record_without_socket_reports_messaging_unavailable(
        self, monkeypatch
    ):
        # The fleet-wide shape: the target IS live and named, and NOTHING
        # in the registry carries a socket. "No such session" would be a
        # false statement about a live, busy peer.
        snap = {
            "sid-live": _record("claude-klabauter-11", None),
            "sid-other": _record("claude-klabauter-22", None),
        }
        monkeypatch.setattr(hr, "snapshot", lambda: snap)
        monkeypatch.setattr(hr, "self_record", lambda: None)

        result = reachability.resolve_address("sid-live")
        assert result.outcome == "not_reachable"
        assert result.address is None
        assert result.reason == reachability.NotReachableReason.MESSAGING_UNAVAILABLE

    def test_absent_record_reports_no_live_record(self, monkeypatch):
        snap = {"sid-live": _record("claude-klabauter-11", "/sock/a.sock")}
        monkeypatch.setattr(hr, "snapshot", lambda: snap)
        monkeypatch.setattr(hr, "self_record", lambda: None)

        result = reachability.resolve_address("sid-gone")
        assert result.outcome == "not_reachable"
        assert result.reason == reachability.NotReachableReason.NO_LIVE_RECORD

    def test_socketless_peer_among_socketed_peers_is_a_peer_fact(self, monkeypatch):
        # Messaging IS available here -- one peer simply never registered
        # an inbox. Reporting the harness-wide reason would send the reader
        # after a capability that is already working.
        snap = {
            "sid-bound": _record("claude-klabauter-11", "/sock/a.sock"),
            "sid-unbound": _record("claude-klabauter-22", None),
        }
        monkeypatch.setattr(hr, "snapshot", lambda: snap)
        monkeypatch.setattr(hr, "self_record", lambda: None)

        result = reachability.resolve_address("sid-unbound")
        assert result.outcome == "not_reachable"
        assert result.reason == reachability.NotReachableReason.PEER_INBOX_ABSENT

    def test_named_record_with_socket_but_no_name_reports_no_peer_name(
        self, monkeypatch
    ):
        snap = {"sid-nameless": _record(None, "/sock/a.sock")}
        monkeypatch.setattr(hr, "snapshot", lambda: snap)
        monkeypatch.setattr(hr, "self_record", lambda: None)

        result = reachability.resolve_address("sid-nameless")
        assert result.outcome == "not_reachable"
        assert result.reason == reachability.NotReachableReason.NO_PEER_NAME

    def test_empty_owner_id_reason_says_nothing_about_the_registry(self, monkeypatch):
        def _explode():
            raise AssertionError("snapshot() must not be read for a falsy owner id")

        monkeypatch.setattr(hr, "snapshot", _explode)
        monkeypatch.setattr(hr, "self_record", lambda: None)

        result = reachability.resolve_address("")
        assert result.outcome == "not_reachable"
        assert result.reason == reachability.NotReachableReason.NO_OWNER_ID

    def test_reachable_and_own_session_carry_no_reason(self, monkeypatch):
        snap = {
            "sid-self": _record("claude-klabauter-11", "/sock/self.sock"),
            "sid-peer": _record("claude-klabauter-22", "/sock/peer.sock"),
        }
        monkeypatch.setattr(hr, "snapshot", lambda: snap)
        monkeypatch.setattr(hr, "self_record", lambda: ("sid-self", snap["sid-self"]))

        assert reachability.resolve_address("sid-peer").reason is None
        own = reachability.resolve_address("sid-self")
        assert own.outcome == "own_session"
        assert own.reason is None
        assert own.address is None


class TestMessagingAvailablePredicate:
    def test_false_when_no_record_carries_a_socket(self):
        snap = {
            "sid-a": _record("claude-klabauter-11", None),
            "sid-b": _record("claude-klabauter-22", None),
        }
        assert reachability.messaging_available(snap) is False

    def test_true_when_any_record_carries_a_socket(self):
        snap = {
            "sid-a": _record("claude-klabauter-11", None),
            "sid-b": _record("claude-klabauter-22", "/sock/b.sock"),
        }
        assert reachability.messaging_available(snap) is True

    def test_empty_snapshot_is_unavailable(self):
        assert reachability.messaging_available({}) is False


class TestNoSubstituteRefWhenSocketAbsent:
    """Anti-scope: a socketless record must never acquire a manufactured
    address. The harness hashes its own live socket path and nothing else,
    so any stand-in (`sessionId`, `pid`, `cwd`) yields an address the
    harness refuses -- "a confident wrong address is worse than no
    address"."""

    def test_socketless_record_is_omitted_from_resolve_candidates(self):
        snap = {
            "sid-bound": _record("claude-klabauter-11", "/sock/a.sock"),
            "sid-unbound": _record("claude-klabauter-22", None),
        }
        resolved = {c.session_id for c in reachability.resolve_candidates(snap)}
        assert resolved == {"sid-bound"}

    def test_no_address_string_embeds_the_session_id(self, monkeypatch):
        snap = {"sid-unbound": _record("claude-klabauter-22", None)}
        monkeypatch.setattr(hr, "snapshot", lambda: snap)
        monkeypatch.setattr(hr, "self_record", lambda: None)

        result = reachability.resolve_address("sid-unbound")
        assert result.address is None
        assert reachability.resolve_advisory_address("sid-unbound") == ""
        assert reachability.resolve_addresses_bulk(["sid-unbound"]) == {
            "sid-unbound": ""
        }

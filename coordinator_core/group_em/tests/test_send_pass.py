"""Tests for the ported send-digest selection/throttle module.

Covers the negative specs named in the C2 dispatch brief for
`docs/plans/2026-08-30-group-em-entry-fires-one-warm-op.md`: cooldown arming
on emit, `unrecorded` on a failed cooldown write, `away` excluded by name
ahead of any bookkeeping cause, `None`-obligations ranking without excluding,
and the `max_entries` ceiling reporting `truncated` rather than silently
cutting.
"""

from __future__ import annotations

import json
import pytest
import os


from coordinator_core.group_em import send_pass


def _verdict(session_id, reason="turn-ended", candidate=True, state="paused", source="reader", cwd=None):
    return {
        "session_id": session_id,
        "candidate": candidate,
        "reason": reason,
        "state": state,
        "source": source,
        "cwd": cwd,
    }


def test_emitting_entry_arms_cooldown_same_call(tmp_path):
    repo_root = str(tmp_path)
    roster = [_verdict("peer-one")]

    digest = send_pass.build_send_digest(repo_root, roster, "caller-one", now=1000.0)

    assert [e["session_id"] for e in digest["entries"]] == ["peer-one"]
    log = send_pass.read_send_log(repo_root, "caller-one")
    assert len(log) == 1
    assert log[0]["offer_key"] == send_pass.offer_key("caller-one", "peer-one")

    # A second call immediately after must suppress peer-one under cooldown.
    digest2 = send_pass.build_send_digest(repo_root, roster, "caller-one", now=1001.0)
    assert digest2["entries"] == []
    reasons = {s["session_id"]: s["why"] for s in digest2["suppressed"]}
    assert reasons["peer-one"] == "cooldown"


def test_unrecorded_on_failed_cooldown_write(tmp_path, monkeypatch):
    repo_root = str(tmp_path)
    roster = [_verdict("peer-two")]

    monkeypatch.setattr(send_pass, "_record_offer", lambda *a, **k: False)

    digest = send_pass.build_send_digest(repo_root, roster, "caller-two", now=1000.0)

    assert [e["session_id"] for e in digest["entries"]] == ["peer-two"]
    assert digest["unrecorded"] == ["peer-two"]


def test_away_excluded_by_name_ahead_of_bookkeeping(tmp_path):
    repo_root = str(tmp_path)
    peer_dir = os.path.join(repo_root, "state", "subagent-share", "peer-away")
    os.makedirs(peer_dir, exist_ok=True)
    with open(os.path.join(peer_dir, "next-move-ledger.jsonl"), "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"discharged_at": None, "fired": False}) + "\n")

    roster = [_verdict("peer-away", reason="away")]

    digest = send_pass.build_send_digest(repo_root, roster, "caller-three", now=1000.0)

    assert digest["entries"] == []
    assert len(digest["suppressed"]) == 1
    assert digest["suppressed"][0]["why"] == "never-send-reason"
    assert digest["suppressed"][0]["session_id"] == "peer-away"


def test_none_obligations_ranks_without_excluding(tmp_path):
    repo_root = str(tmp_path)
    # peer-with-ledger has one open obligation; peer-no-ledger has none (None).
    ledger_dir = os.path.join(repo_root, "state", "subagent-share", "peer-with-ledger")
    os.makedirs(ledger_dir, exist_ok=True)
    with open(os.path.join(ledger_dir, "next-move-ledger.jsonl"), "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"discharged_at": None, "fired": False}) + "\n")

    roster = [_verdict("peer-no-ledger"), _verdict("peer-with-ledger")]

    digest = send_pass.build_send_digest(repo_root, roster, "caller-four", now=1000.0)

    session_ids = [e["session_id"] for e in digest["entries"]]
    assert session_ids == ["peer-with-ledger", "peer-no-ledger"]
    no_ledger_entry = next(e for e in digest["entries"] if e["session_id"] == "peer-no-ledger")
    assert no_ledger_entry["undischarged_obligations"] is None


def test_max_entries_ceiling_reports_truncated(tmp_path):
    repo_root = str(tmp_path)
    roster = [_verdict(f"peer-{i}") for i in range(7)]

    digest = send_pass.build_send_digest(
        repo_root, roster, "caller-five", now=1000.0, max_entries=3
    )

    assert len(digest["entries"]) == 3
    assert digest["truncated"] is True
    assert digest["eligible_before_ceiling"] == 7
    rate_ceiling_ids = {
        s["session_id"] for s in digest["suppressed"] if s["why"] == "rate-ceiling"
    }
    assert len(rate_ceiling_ids) == 4


def test_no_per_peer_public_entry_point():
    """The module must expose no function offering a single peer directly --
    `build_send_digest` is the sole route to an entry (negative spec)."""
    public_names = [n for n in dir(send_pass) if not n.startswith("_")]
    forbidden_substrings = ("send_one", "send_peer", "offer_peer", "nudge_peer")
    for name in public_names:
        lowered = name.lower()
        assert not any(f in lowered for f in forbidden_substrings), name


class _FakeRow:
    def __init__(self, session_id, name):
        self.session_id = session_id
        self.name = name


def test_resolve_addressee_returns_live_name(tmp_path):
    repo_root = str(tmp_path)
    rows = [_FakeRow("peer-sid", "claude-klabauter-e0")]

    name = send_pass.resolve_addressee(
        repo_root, "peer-sid", build_roster=lambda repo_root: rows
    )

    assert name == "claude-klabauter-e0"


def test_resolve_addressee_refuses_on_repoint(tmp_path):
    """A name that no longer maps to the queried session id must refuse,
    not fall back to the last-known name or the bare session id -- this is
    the exact re-point failure C9 exists to close."""
    repo_root = str(tmp_path)
    # The live roster now shows a DIFFERENT session id under that peer's old
    # slot -- the queried (now-stale) session id is absent entirely.
    rows = [_FakeRow("peer-sid-NEW", "claude-klabauter-e0")]

    name = send_pass.resolve_addressee(
        repo_root, "peer-sid-OLD", build_roster=lambda repo_root: rows
    )

    assert name is None


def test_resolve_addressee_refuses_when_session_absent(tmp_path):
    repo_root = str(tmp_path)

    name = send_pass.resolve_addressee(
        repo_root, "gone-sid", build_roster=lambda repo_root: []
    )

    assert name is None


def test_resolve_addressee_refuses_when_row_has_no_name(tmp_path):
    repo_root = str(tmp_path)
    rows = [_FakeRow("peer-sid", None)]

    name = send_pass.resolve_addressee(
        repo_root, "peer-sid", build_roster=lambda repo_root: rows
    )

    assert name is None


def test_resolve_addressee_refuses_on_unsafe_session_id(tmp_path):
    repo_root = str(tmp_path)

    name = send_pass.resolve_addressee(
        repo_root, "../escape", build_roster=lambda repo_root: []
    )

    assert name is None


def test_resolve_addressee_refuses_on_roster_read_failure(tmp_path):
    repo_root = str(tmp_path)

    def _raise(repo_root):
        raise RuntimeError("registry unavailable")

    name = send_pass.resolve_addressee(repo_root, "peer-sid", build_roster=_raise)

    assert name is None


def test_resolve_addressee_never_caches_across_calls(tmp_path):
    """Every call re-reads the roster -- a name resolved once must not be
    reused once the live roster no longer confirms it."""
    repo_root = str(tmp_path)
    calls = {"rows": [_FakeRow("peer-sid", "claude-klabauter-e0")]}

    def _roster(repo_root):
        return calls["rows"]

    first = send_pass.resolve_addressee(repo_root, "peer-sid", build_roster=_roster)
    assert first == "claude-klabauter-e0"

    # The peer re-pointed away between calls -- the resolver must not have
    # memoized the earlier answer.
    calls["rows"] = []
    second = send_pass.resolve_addressee(repo_root, "peer-sid", build_roster=_roster)
    assert second is None


def test_offer_row_carries_outcome_discriminator(tmp_path):
    repo_root = str(tmp_path)
    roster = [_verdict("peer-disc")]

    send_pass.build_send_digest(repo_root, roster, "caller-disc", now=1000.0)

    log = send_pass.read_send_log(repo_root, "caller-disc")
    assert len(log) == 1
    assert log[0]["outcome"] == "offer"


def test_decline_writes_declination_row_same_log(tmp_path):
    repo_root = str(tmp_path)

    ok = send_pass.decline(
        repo_root, "caller-six", "peer-six", "gate1", "not ready this tick", now=2000.0
    )

    assert ok is True
    log = send_pass.read_send_log(repo_root, "caller-six")
    assert len(log) == 1
    row = log[0]
    assert row["outcome"] == "declination"
    assert row["gate"] == "gate1"
    assert row["reason"] == "not ready this tick"
    assert row["offer_key"] == send_pass.offer_key("caller-six", "peer-six")


def test_decline_refuses_bad_gate(tmp_path):
    repo_root = str(tmp_path)

    ok = send_pass.decline(repo_root, "caller-seven", "peer-seven", "gate3", "reason", now=1.0)

    assert ok is False
    assert send_pass.read_send_log(repo_root, "caller-seven") == []


def test_decline_refuses_empty_reason(tmp_path):
    repo_root = str(tmp_path)

    ok = send_pass.decline(repo_root, "caller-eight", "peer-eight", "gate2", "  ", now=1.0)

    assert ok is False


def test_decline_never_arms_cooldown(tmp_path):
    """Declining is not offering -- a declined peer must still be eligible
    on the very next digest, never held under cooldown from the decline."""
    repo_root = str(tmp_path)
    roster = [_verdict("peer-nine")]

    send_pass.decline(repo_root, "caller-nine", "peer-nine", "gate2", "waiting", now=1000.0)
    digest = send_pass.build_send_digest(repo_root, roster, "caller-nine", now=1001.0)

    assert [e["session_id"] for e in digest["entries"]] == ["peer-nine"]


def test_entries_carry_dwell_seconds_key(tmp_path):
    repo_root = str(tmp_path)
    roster = [_verdict("peer-dwell")]

    digest = send_pass.build_send_digest(repo_root, roster, "caller-dwell", now=1000.0)

    assert digest["entries"][0]["dwell_seconds"] is None


def test_dwell_seconds_derived_from_receiver_state_stamp(tmp_path, monkeypatch):
    repo_root = str(tmp_path)
    monkeypatch.setattr(
        send_pass.read_pass,
        "read_receiver_state",
        lambda sid, cwd: {"stamped_at": "2026-08-31T00:00:00Z"},
    )
    monkeypatch.setattr(
        send_pass.read_pass, "_transcript_mtime_epoch", lambda sid, cwd: None
    )
    from datetime import datetime, timezone

    stamp_epoch = datetime(2026, 8, 31, 0, 0, 0, tzinfo=timezone.utc).timestamp()
    roster = [_verdict("peer-stamped")]

    digest = send_pass.build_send_digest(
        repo_root, roster, "caller-stamped", now=stamp_epoch + 500.0
    )

    assert digest["entries"][0]["dwell_seconds"] == 500.0


def test_dwell_seconds_uses_peer_cwd_not_repo_root(tmp_path, monkeypatch):
    """Finding 1 (coordinator:code-reviewer, P1): a peer whose `cwd` is a
    subdirectory of `repo_root` (permitted by `build_roster`'s "within
    repo_root" filter) must have ITS OWN cwd threaded to
    `_transcript_mtime_epoch`, matching `read_pass.classify_peer`'s own
    `peer.get("cwd") or repo_root` pattern -- else `_transcript_path_for`
    looks up the wrong encoded path and dwell silently degrades to `None`
    forever for exactly this population."""
    repo_root = str(tmp_path)
    peer_cwd = str(tmp_path / "nested-worktree")

    monkeypatch.setattr(
        send_pass.read_pass, "read_receiver_state", lambda sid, root: None
    )

    seen_cwds: list = []

    def fake_transcript_mtime(sid, cwd):
        seen_cwds.append(cwd)
        return 500.0 if cwd == peer_cwd else None

    monkeypatch.setattr(
        send_pass.read_pass, "_transcript_mtime_epoch", fake_transcript_mtime
    )

    roster = [_verdict("peer-nested", cwd=peer_cwd)]
    digest = send_pass.build_send_digest(repo_root, roster, "caller-nested", now=1000.0)

    assert digest["entries"][0]["dwell_seconds"] == 500.0
    assert peer_cwd in seen_cwds
    assert repo_root not in seen_cwds


def test_dwell_seconds_prefers_more_recent_of_stamp_and_transcript(tmp_path, monkeypatch):
    """Finding 2 (coordinator:code-reviewer, P2): the module docstring's
    entire justification for reading both sources is that the more RECENT
    of the two wins. Pin both directions -- a rewrite that always preferred
    `stamped_at` (or always preferred the transcript) would fail one of
    these."""
    from datetime import datetime, timezone

    repo_root = str(tmp_path)
    stamp_dt = datetime(2026, 8, 31, 0, 0, 0, tzinfo=timezone.utc)
    stamp_epoch = stamp_dt.timestamp()

    monkeypatch.setattr(
        send_pass.read_pass,
        "read_receiver_state",
        lambda sid, root: {"stamped_at": "2026-08-31T00:00:00Z"},
    )

    # Transcript is NEWER than the stamp -- dwell must be measured from the
    # transcript, not the stale stamp.
    monkeypatch.setattr(
        send_pass.read_pass,
        "_transcript_mtime_epoch",
        lambda sid, cwd: stamp_epoch + 200.0,
    )
    roster = [_verdict("peer-newer-transcript")]
    digest = send_pass.build_send_digest(
        repo_root, roster, "caller-max-1", now=stamp_epoch + 500.0
    )
    assert digest["entries"][0]["dwell_seconds"] == 300.0

    # Transcript is OLDER than the stamp -- dwell must be measured from the
    # (more recent) stamp, not the stale transcript.
    monkeypatch.setattr(
        send_pass.read_pass,
        "_transcript_mtime_epoch",
        lambda sid, cwd: stamp_epoch - 200.0,
    )
    roster2 = [_verdict("peer-newer-stamp")]
    digest2 = send_pass.build_send_digest(
        repo_root, roster2, "caller-max-2", now=stamp_epoch + 500.0
    )
    assert digest2["entries"][0]["dwell_seconds"] == 500.0


def test_open_obligations_includes_freshly_emitted_entries(tmp_path):
    repo_root = str(tmp_path)
    roster = [_verdict("peer-open")]

    digest = send_pass.build_send_digest(repo_root, roster, "caller-open", now=1000.0)

    assert digest["open_obligations"] == ["peer-open"]


def test_open_obligations_survive_cooldown_suppression_until_declined(tmp_path):
    """The belt to C2's suspenders: a peer offered on an earlier tick and
    now held by cooldown still names an open obligation -- until declined."""
    repo_root = str(tmp_path)
    roster = [_verdict("peer-ten")]

    first = send_pass.build_send_digest(repo_root, roster, "caller-ten", now=1000.0)
    assert first["open_obligations"] == ["peer-ten"]

    second = send_pass.build_send_digest(repo_root, roster, "caller-ten", now=1001.0)
    assert second["entries"] == []
    assert second["open_obligations"] == ["peer-ten"]

    send_pass.decline(repo_root, "caller-ten", "peer-ten", "gate1", "still mid-turn", now=1002.0)
    third = send_pass.build_send_digest(repo_root, roster, "caller-ten", now=1003.0)
    assert third["open_obligations"] == []


# ---------------------------------------------------------------------------
# DECLINATIONS -- "a tick that sends nothing records which obligation it
# declined and why, and cannot close on an empty result". The empty-roster leg
# is also the plan's acceptance oracle; these cover the paths it does not.
# ---------------------------------------------------------------------------


def test_empty_roster_declines_the_obligation_to_look(tmp_path):
    """A tick that considered nobody must say so, not return four empty fields.

    This is the shape the criterion forbids: a digest of empty lists is
    indistinguishable from a tick that never ran.
    """
    digest = send_pass.build_send_digest(str(tmp_path), [], "caller-empty", now=1000.0)
    assert digest["declined"], "an empty-roster tick closed with no declination"
    assert any(row["reason"].startswith("roster-empty") for row in digest["declined"])
    assert all(row.get("obligation") and row.get("reason") for row in digest["declined"])


def test_every_suppressed_peer_gets_its_own_declination(tmp_path):
    """DoE's wording: a declination for every roster entry it does not message,
    naming which gate failed. One suppressed peer, one row, carrying its reason.

    Review: overengineering-reviewer (finding #4, EM-ratified partial) -- the
    per-peer declination is a projection of `suppressed`, folded in there
    (`obligation`/`dwell_seconds` on the row itself) rather than round-tripped
    through `declined`, which now names only tick-level declinations.
    """
    roster = [_verdict("peer-away", reason="away", state="away")]
    digest = send_pass.build_send_digest(str(tmp_path), roster, "caller-sup", now=1000.0)
    assert not digest["entries"]
    by_peer = {row["session_id"]: row for row in digest["suppressed"] if row["session_id"]}
    assert "peer-away" in by_peer
    assert by_peer["peer-away"]["why"]
    assert by_peer["peer-away"]["obligation"] == "message peer peer-away"


def test_full_roster_none_eligible_still_declines_the_tick(tmp_path):
    """A non-empty roster where nothing survives is still a tick that sent nothing.

    The per-peer rows alone would let the tick close without ever saying it sent
    to no one -- the reader would have to infer it from an empty `entries`, which
    is precisely the inference the criterion refuses to rely on.
    """
    roster = [_verdict("peer-a", reason="away", state="away"),
              _verdict("peer-b", reason="away", state="away")]
    digest = send_pass.build_send_digest(str(tmp_path), roster, "caller-none", now=1000.0)
    assert not digest["entries"]
    tick_rows = [row for row in digest["declined"] if row["session_id"] is None]
    assert tick_rows, "no tick-level declination on a roster where nothing was eligible"
    assert "2" in tick_rows[0]["reason"], "the declination should name how many were considered"


def test_a_tick_that_sends_declines_only_what_it_held_back(tmp_path):
    """The converse, so `declined` is not just always-non-empty theatre: a tick
    that actually emits carries no tick-level declination."""
    roster = [_verdict("peer-live")]
    digest = send_pass.build_send_digest(str(tmp_path), roster, "caller-live", now=1000.0)
    assert digest["entries"]
    assert [row for row in digest["declined"] if row["session_id"] is None] == []


def test_cooldown_declination_carries_dwell_so_the_hold_is_weighable(tmp_path, monkeypatch):
    """Observed live 2026-08-31: the roster correctly identified a peer parked
    10.4m and the digest suppressed it on cooldown, with nothing saying how long
    it had been parked. Cooldown outranking dwell is deliberate -- re-offering to
    a peer you just messaged is nagging -- but the EM cannot weigh a hold it
    cannot see, so the declination names both.
    """
    repo_root = str(tmp_path)
    roster = [_verdict("peer-held")]
    send_pass.build_send_digest(repo_root, roster, "caller-dw", now=1000.0)

    monkeypatch.setattr(send_pass, "_dwell_seconds", lambda r, p, n, cwd=None: 624.0)
    digest = send_pass.build_send_digest(repo_root, roster, "caller-dw", now=1060.0)

    held = [row for row in digest["suppressed"] if row["why"] == "cooldown"]
    assert held, "expected the second tick to hold the peer on cooldown"
    assert held[0]["dwell_seconds"] == 624.0


def test_non_cooldown_declinations_do_not_pay_for_dwell(tmp_path, monkeypatch):
    """`away` is not a hold anyone would overturn on dwell, and each computation
    is two reads per peer. Every row still carries the key, `None` where
    inapplicable, so a consumer never key-checks by variant."""
    monkeypatch.setattr(
        send_pass, "_dwell_seconds", lambda *a: pytest.fail("dwell computed for a non-cooldown hold")
    )
    roster = [_verdict("peer-away", reason="away", state="away")]
    digest = send_pass.build_send_digest(str(tmp_path), roster, "caller-away", now=1000.0)
    row = next(r for r in digest["suppressed"] if r["session_id"] == "peer-away")
    assert row["dwell_seconds"] is None
    assert all("dwell_seconds" in r for r in digest["suppressed"])

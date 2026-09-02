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


def _verdict(
    session_id,
    reason="turn-ended",
    candidate=True,
    state="paused",
    source="reader",
    cwd=None,
    contradicted=False,
):
    return {
        "session_id": session_id,
        "candidate": candidate,
        "reason": reason,
        "state": state,
        "source": source,
        "cwd": cwd,
        "contradicted": contradicted,
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


def test_digest_carries_as_of_struck_from_the_same_now(tmp_path):
    repo_root = str(tmp_path)
    roster = [_verdict("peer-as-of")]

    digest = send_pass.build_send_digest(repo_root, roster, "caller-as-of", now=1_700_000_000.0)

    assert digest["as_of"] == "2023-11-14T22:13:20Z"


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
        send_pass.read_pass, "transcript_activity_epoch", lambda sid, cwd: (None, False)
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
    `transcript_activity_epoch`, matching `read_pass.classify_peer`'s own
    `peer.get("cwd") or repo_root` pattern -- else `_transcript_path_for`
    looks up the wrong encoded path and dwell silently degrades to `None`
    forever for exactly this population."""
    repo_root = str(tmp_path)
    peer_cwd = str(tmp_path / "nested-worktree")

    monkeypatch.setattr(
        send_pass.read_pass, "read_receiver_state", lambda sid, root: None
    )

    seen_cwds: list = []

    def fake_transcript_activity(sid, cwd):
        seen_cwds.append(cwd)
        return (500.0, True) if cwd == peer_cwd else (None, False)

    monkeypatch.setattr(
        send_pass.read_pass, "transcript_activity_epoch", fake_transcript_activity
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

    # Transcript is NEWER than the stamp AND trusted (read off a record's own
    # `timestamp`) -- dwell must be measured from the transcript, not the
    # stale stamp.
    monkeypatch.setattr(
        send_pass.read_pass,
        "transcript_activity_epoch",
        lambda sid, cwd: (stamp_epoch + 200.0, True),
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
        "transcript_activity_epoch",
        lambda sid, cwd: (stamp_epoch - 200.0, True),
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


def test_an_untrusted_transcript_clock_never_wins_the_dwell_max(tmp_path, monkeypatch):
    """The 2026-08-31 defect, pinned. `max(stamp, mtime)` let file mtime --
    which the harness moves forward with untimestamped bookkeeping rows on
    STOPPED peers -- win exactly when the reading matters, reporting a stalled
    peer as freshly active. An untrusted epoch may not make a peer look
    busier, however recent it looks."""
    from datetime import datetime, timezone

    repo_root = str(tmp_path)
    stamp_epoch = datetime(2026, 8, 31, 0, 0, 0, tzinfo=timezone.utc).timestamp()

    monkeypatch.setattr(
        send_pass.read_pass,
        "read_receiver_state",
        lambda sid, root: {"stamped_at": "2026-08-31T00:00:00Z"},
    )
    monkeypatch.setattr(
        send_pass.read_pass,
        "transcript_activity_epoch",
        lambda sid, cwd: (stamp_epoch + 420.0, False),
    )

    digest = send_pass.build_send_digest(
        repo_root, [_verdict("peer-skewed-mtime")], "caller-untrusted", now=stamp_epoch + 500.0
    )

    # 500s of real dwell, not the 80s the skewed mtime would have reported.
    assert digest["entries"][0]["dwell_seconds"] == 500.0


def test_an_untrusted_transcript_clock_is_still_used_when_it_is_the_only_source(
    tmp_path, monkeypatch
):
    """Refusing it outright would report `None` (unknown) for every peer with
    no receiver-state record -- strictly less information than the upper bound
    this function has always given. It loses the comparison; it is not
    discarded."""
    repo_root = str(tmp_path)
    monkeypatch.setattr(send_pass.read_pass, "read_receiver_state", lambda sid, root: None)
    monkeypatch.setattr(
        send_pass.read_pass, "transcript_activity_epoch", lambda sid, cwd: (500.0, False)
    )

    digest = send_pass.build_send_digest(
        repo_root, [_verdict("peer-mtime-only")], "caller-only-source", now=1000.0
    )

    assert digest["entries"][0]["dwell_seconds"] == 500.0


def test_the_share_paths_are_one_owners_answer_not_three_copies(tmp_path):
    """`send_pass`, `group_em.obligations` and the undischarged-next-move
    watchdog each carried their own `state/subagent-share/<sid>/` join and
    their own `"next-move-ledger.jsonl"` literal, with `obligations` reaching
    into this module's private namespace for one of them. One typo apart, a
    producer and its reader would have been on different files with nothing
    to catch it -- all three now call `subagent_share`'s helpers directly (no
    module-private alias left to drift), which this exercises end to end: a
    ledger written at `subagent_share.ledger_path` is readable through both
    `send_pass.undischarged_obligations` and `obligations.for_peer`.

    Review: overengineering-reviewer (finding #2, minor, accepted) -- this
    used to compare three separately-bound private aliases for equality;
    the aliases are gone, so the meaningful check is that a producer and a
    reader land on the same file, not that two names for one function match.
    """
    import json

    from coordinator_core.group_em import obligations
    from coordinator_core.session import subagent_share

    repo_root, session_id = str(tmp_path), "sess-share"
    assert send_pass.send_log_path(repo_root, session_id) == subagent_share.send_log_path(
        repo_root, session_id
    )

    ledger_path = subagent_share.ledger_path(repo_root, session_id)
    os.makedirs(os.path.dirname(ledger_path), exist_ok=True)
    with open(ledger_path, "w", encoding="utf-8") as fh:
        fh.write(
            json.dumps(
                {"obligation_id": "ob-1", "discharged_at": None, "fired": False}
            )
            + "\n"
        )

    assert send_pass.undischarged_obligations(repo_root, session_id) == 1
    assert obligations.for_peer(repo_root, session_id) == [
        {"obligation_id": "ob-1", "discharged_at": None, "fired": False}
    ]


def test_an_unsafe_session_id_is_still_refused_a_path(tmp_path):
    """The predicate moved modules; it did not relax. A bare `.`/`..` passes
    the character class alone, which is why the check is not just a regex."""
    from coordinator_core.session import subagent_share

    assert subagent_share.safe_session_id("sess-1") is True
    for bad in ("..", ".", "", None, "a/b", "a\b", "a:b"):
        assert subagent_share.safe_session_id(bad) is False


# C4 -- state/dispatch-briefs/2026-09-01-the-crowns-standing-surfaces-report-
# themselves/C4.md: a contradicted peer reaches `suppressed` with the gate
# that excluded it named, instead of vanishing with no trace at either
# `read_pass`'s filter or this module's own.


def test_contradicted_peer_reaches_suppressed_with_gate_named_live_busy(tmp_path):
    repo_root = str(tmp_path)
    roster = [
        _verdict(
            "peer-live-busy",
            reason="live-busy-contradicts-paused",
            candidate=False,
            contradicted=True,
        )
    ]

    digest = send_pass.build_send_digest(repo_root, roster, "caller-c4a", now=1000.0)

    assert digest["entries"] == []
    assert len(digest["suppressed"]) == 1
    row = digest["suppressed"][0]
    assert row["session_id"] == "peer-live-busy"
    assert row["why"] == "contradicted"
    assert row["reason"] == "live-busy-contradicts-paused"


def test_contradicted_peer_reaches_suppressed_with_gate_named_stale_snapshot(tmp_path):
    repo_root = str(tmp_path)
    roster = [
        _verdict(
            "peer-stale-snapshot",
            reason="stale-snapshot-contradicts-paused",
            candidate=False,
            contradicted=True,
        )
    ]

    digest = send_pass.build_send_digest(repo_root, roster, "caller-c4b", now=1000.0)

    assert digest["entries"] == []
    assert len(digest["suppressed"]) == 1
    row = digest["suppressed"][0]
    assert row["session_id"] == "peer-stale-snapshot"
    assert row["why"] == "contradicted"
    assert row["reason"] == "stale-snapshot-contradicts-paused"


def test_digest_counts_sum_to_population_classified_including_contradicted(tmp_path):
    """`entries` + `suppressed` must account for every verdict this tick
    classified -- a contradicted peer that reached neither would be an
    upstream exclusion with no trace anywhere (the defect C4 closes)."""
    repo_root = str(tmp_path)
    roster = [
        _verdict("peer-normal"),
        _verdict(
            "peer-live-busy",
            reason="live-busy-contradicts-paused",
            candidate=False,
            contradicted=True,
        ),
        _verdict(
            "peer-stale-unresolved",
            reason="stale-snapshot-unresolved",
            candidate=False,
            contradicted=True,
        ),
    ]

    digest = send_pass.build_send_digest(repo_root, roster, "caller-c4c", now=1000.0)

    assert len(digest["entries"]) + len(digest["suppressed"]) == len(roster)
    contradicted_ids = {
        row["session_id"] for row in digest["suppressed"] if row["why"] == "contradicted"
    }
    assert contradicted_ids == {"peer-live-busy", "peer-stale-unresolved"}


class _Row:
    def __init__(self, session_id, name):
        self.session_id = session_id
        self.name = name


def test_resolve_addressee_refuses_a_name_two_live_sessions_answer_to(tmp_path):
    """`SendMessage` addresses BY NAME, so returning a name two sessions share
    hands the caller an address that can land on the wrong one. Stable key in,
    volatile address out -- only when the address is unambiguous.
    """
    rows = [_Row("peer-sid", "twin"), _Row("other-sid", "twin")]
    got = send_pass.resolve_addressee(
        str(tmp_path), "peer-sid", build_roster=lambda repo_root=None: rows
    )
    assert got is None


def test_resolve_addressee_returns_the_name_when_it_is_unique(tmp_path):
    rows = [_Row("peer-sid", "alpha"), _Row("other-sid", "beta")]
    got = send_pass.resolve_addressee(
        str(tmp_path), "peer-sid", build_roster=lambda repo_root=None: rows
    )
    assert got == "alpha"


def test_resolve_addressee_raises_on_the_wrong_build_roster(tmp_path):
    """Two same-named `build_roster`s live in one package with incompatible row
    shapes. Injecting the dict-yielding one made every `getattr` return None --
    an unaddressable fleet reported as a clean refusal, raising nothing.
    """
    dict_rows = [{"session_id": "peer-sid", "name": "alpha"}]
    import pytest

    with pytest.raises(TypeError, match="same name, different shape"):
        send_pass.resolve_addressee(
            str(tmp_path), "peer-sid", build_roster=lambda repo_root=None: dict_rows
        )

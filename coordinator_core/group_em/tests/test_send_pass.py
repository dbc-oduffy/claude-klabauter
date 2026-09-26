
from __future__ import annotations

import json
import pytest
import os


from coordinator_core.group_em import send_pass
from coordinator_core.session.machinery_paths import share_dir as _share_dir


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
    peer_dir = _share_dir(repo_root, "peer-away")
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
    ledger_dir = _share_dir(repo_root, "peer-with-ledger")
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


def test_record_offers_batches_under_holder_key_with_nudger_attribution(tmp_path):
    repo_root = str(tmp_path)

    unrecorded = send_pass.record_offers(
        repo_root, "holder-one", ["peer-a", "peer-b"], "nudger-one", now=1000.0
    )

    assert unrecorded == []
    log = send_pass.read_send_log(repo_root, "holder-one")
    assert len(log) == 2
    for row, peer in zip(log, ["peer-a", "peer-b"]):
        assert row["outcome"] == "offer"
        assert row["offered_by"] == "nudger-one"
        assert row["offer_key"] == send_pass.offer_key("holder-one", peer)

    remaining = send_pass._cooldown_remaining(
        log, send_pass.offer_key("holder-one", "peer-a"), 1001.0, send_pass.DEFAULT_COOLDOWN_SECONDS
    )
    assert remaining > 0

    assert send_pass.read_send_log(repo_root, "nudger-one") == []


def test_record_offers_offer_key_derives_from_holder_not_nudger(tmp_path):
    repo_root = str(tmp_path)

    send_pass.record_offers(repo_root, "holder-two", ["peer-x"], "nudger-two", now=1000.0)

    log = send_pass.read_send_log(repo_root, "holder-two")
    assert log[0]["offer_key"] == send_pass.offer_key("holder-two", "peer-x")
    assert log[0]["offer_key"] != send_pass.offer_key("nudger-two", "peer-x")


def test_record_offers_refuses_malformed_id_in_any_position(tmp_path):
    repo_root = str(tmp_path)

    unrecorded = send_pass.record_offers(
        repo_root, "../escape", ["peer-y"], "nudger-three", now=1000.0
    )
    assert unrecorded == ["peer-y"]
    assert send_pass.read_send_log(repo_root, "../escape") == []

    unrecorded = send_pass.record_offers(
        repo_root, "holder-three", ["peer-y"], "../escape", now=1000.0
    )
    assert unrecorded == ["peer-y"]

    unrecorded = send_pass.record_offers(
        repo_root, "holder-four", ["peer-good", "../escape"], "nudger-four", now=1000.0
    )
    assert unrecorded == ["../escape"]
    log = send_pass.read_send_log(repo_root, "holder-four")
    assert len(log) == 1
    assert log[0]["offer_key"] == send_pass.offer_key("holder-four", "peer-good")


def test_record_offers_single_write_call(tmp_path, monkeypatch):
    repo_root = str(tmp_path)
    calls: list[bytes] = []
    real_append_claimed_line = send_pass.append_claimed_line

    def _counting_append(path, encoded):
        calls.append(encoded)
        return real_append_claimed_line(path, encoded)

    monkeypatch.setattr(send_pass, "append_claimed_line", _counting_append)

    send_pass.record_offers(
        repo_root, "holder-five", ["peer-1", "peer-2", "peer-3"], "nudger-five", now=1000.0
    )

    assert len(calls) == 1
    assert calls[0].decode("utf-8").count("offer_key") == 3


def test_record_offer_row_without_attribution_still_suppresses(tmp_path):
    repo_root = str(tmp_path)
    roster = [_verdict("peer-plain")]

    digest = send_pass.build_send_digest(repo_root, roster, "caller-plain", now=1000.0)
    assert [e["session_id"] for e in digest["entries"]] == ["peer-plain"]

    log = send_pass.read_send_log(repo_root, "caller-plain")
    assert "offered_by" not in log[0]

    digest2 = send_pass.build_send_digest(repo_root, roster, "caller-plain", now=1001.0)
    assert digest2["entries"] == []
    reasons = {s["session_id"]: s["why"] for s in digest2["suppressed"]}
    assert reasons["peer-plain"] == "cooldown"


def test_log_key_is_open_excludes_attributed_rows(tmp_path):
    key = send_pass.offer_key("holder-six", "peer-six")
    attributed_log = [
        {"outcome": "offer", "offer_key": key, "offered_at": 1000.0, "offered_by": "nudger-six"},
    ]
    assert send_pass._log_key_is_open(attributed_log, key) is False

    plain_log = [
        {"outcome": "offer", "offer_key": key, "offered_at": 1000.0},
    ]
    assert send_pass._log_key_is_open(plain_log, key) is True


def test_log_key_is_open_ac8b_end_to_end_via_build_send_digest(tmp_path):
    repo_root = str(tmp_path)
    send_pass.record_offers(
        repo_root, "holder-seven", ["peer-seven"], "nudger-seven", now=1000.0
    )
    roster = [_verdict("peer-seven")]
    digest = send_pass.build_send_digest(repo_root, roster, "holder-seven", now=1001.0)
    assert digest["entries"] == []
    assert "peer-seven" not in digest["open_obligations"]


def test_no_per_peer_public_entry_point():
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
    repo_root = str(tmp_path)
    # The live roster now shows a DIFFERENT session id under that peer's old
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
    repo_root = str(tmp_path)
    calls = {"rows": [_FakeRow("peer-sid", "claude-klabauter-e0")]}

    def _roster(repo_root):
        return calls["rows"]

    first = send_pass.resolve_addressee(repo_root, "peer-sid", build_roster=_roster)
    assert first == "claude-klabauter-e0"

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
    from datetime import datetime, timezone

    repo_root = str(tmp_path)
    stamp_dt = datetime(2026, 8, 31, 0, 0, 0, tzinfo=timezone.utc)
    stamp_epoch = stamp_dt.timestamp()

    monkeypatch.setattr(
        send_pass.read_pass,
        "read_receiver_state",
        lambda sid, root: {"stamped_at": "2026-08-31T00:00:00Z"},
    )

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


# DECLINATIONS -- "a tick that sends nothing records which obligation it


def test_empty_roster_declines_the_obligation_to_look(tmp_path):
    digest = send_pass.build_send_digest(str(tmp_path), [], "caller-empty", now=1000.0)
    assert digest["declined"], "an empty-roster tick closed with no declination"
    assert any(row["reason"].startswith("roster-empty") for row in digest["declined"])
    assert all(row.get("obligation") and row.get("reason") for row in digest["declined"])


def test_every_suppressed_peer_gets_its_own_declination(tmp_path):
    roster = [_verdict("peer-away", reason="away", state="away")]
    digest = send_pass.build_send_digest(str(tmp_path), roster, "caller-sup", now=1000.0)
    assert not digest["entries"]
    by_peer = {row["session_id"]: row for row in digest["suppressed"] if row["session_id"]}
    assert "peer-away" in by_peer
    assert by_peer["peer-away"]["why"]
    assert by_peer["peer-away"]["obligation"] == "message peer peer-away"


def test_full_roster_none_eligible_still_declines_the_tick(tmp_path):
    roster = [_verdict("peer-a", reason="away", state="away"),
              _verdict("peer-b", reason="away", state="away")]
    digest = send_pass.build_send_digest(str(tmp_path), roster, "caller-none", now=1000.0)
    assert not digest["entries"]
    tick_rows = [row for row in digest["declined"] if row["session_id"] is None]
    assert tick_rows, "no tick-level declination on a roster where nothing was eligible"
    assert "2" in tick_rows[0]["reason"], "the declination should name how many were considered"


def test_a_tick_that_sends_declines_only_what_it_held_back(tmp_path):
    roster = [_verdict("peer-live")]
    digest = send_pass.build_send_digest(str(tmp_path), roster, "caller-live", now=1000.0)
    assert digest["entries"]
    assert [row for row in digest["declined"] if row["session_id"] is None] == []


def test_cooldown_declination_carries_dwell_so_the_hold_is_weighable(tmp_path, monkeypatch):
    repo_root = str(tmp_path)
    roster = [_verdict("peer-held")]
    send_pass.build_send_digest(repo_root, roster, "caller-dw", now=1000.0)

    monkeypatch.setattr(send_pass, "_dwell_seconds", lambda r, p, n, cwd=None: 624.0)
    digest = send_pass.build_send_digest(repo_root, roster, "caller-dw", now=1060.0)

    held = [row for row in digest["suppressed"] if row["why"] == "cooldown"]
    assert held, "expected the second tick to hold the peer on cooldown"
    assert held[0]["dwell_seconds"] == 624.0


def test_non_cooldown_declinations_do_not_pay_for_dwell(tmp_path, monkeypatch):
    monkeypatch.setattr(
        send_pass, "_dwell_seconds", lambda *a: pytest.fail("dwell computed for a non-cooldown hold")
    )
    roster = [_verdict("peer-away", reason="away", state="away")]
    digest = send_pass.build_send_digest(str(tmp_path), roster, "caller-away", now=1000.0)
    row = next(r for r in digest["suppressed"] if r["session_id"] == "peer-away")
    assert row["dwell_seconds"] is None
    assert all("dwell_seconds" in r for r in digest["suppressed"])


def test_an_untrusted_transcript_clock_never_wins_the_dwell_max(tmp_path, monkeypatch):
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

    assert digest["entries"][0]["dwell_seconds"] == 500.0


def test_an_untrusted_transcript_clock_is_still_used_when_it_is_the_only_source(
    tmp_path, monkeypatch
):
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
    import json

    from coordinator_core.group_em import obligations
    from coordinator_core.session import machinery_paths

    repo_root, session_id = str(tmp_path), "sess-share"
    assert send_pass.send_log_path(repo_root, session_id) == machinery_paths.send_log_path(
        repo_root, session_id
    )

    ledger_path = machinery_paths.ledger_path(repo_root, session_id)
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
    from coordinator_core.session import machinery_paths

    assert machinery_paths.safe_session_id("sess-1") is True
    for bad in ("..", ".", "", None, "a/b", "a\b", "a:b"):
        assert machinery_paths.safe_session_id(bad) is False


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
    dict_rows = [{"session_id": "peer-sid", "name": "alpha"}]
    import pytest

    with pytest.raises(TypeError, match="same name, different shape"):
        send_pass.resolve_addressee(
            str(tmp_path), "peer-sid", build_roster=lambda repo_root=None: dict_rows
        )

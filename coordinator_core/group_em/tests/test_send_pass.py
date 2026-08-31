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
import os


from coordinator_core.group_em import send_pass


def _verdict(session_id, reason="turn-ended", candidate=True, state="paused", source="reader"):
    return {
        "session_id": session_id,
        "candidate": candidate,
        "reason": reason,
        "state": state,
        "source": source,
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

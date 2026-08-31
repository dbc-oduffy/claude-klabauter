"""
coordinator_core.tests.test_tracker_transitions — C9b: AC4, AC5, AC9 (plus
the AC4/AC5 precedence cell, AC7, the closed-axis-enum guard, and the
`applied_at`/`tier: suggest` invisibility contract).

Purpose: exercises `coordinator_core.tracker_transitions`' per-axis dedup
scoping (C3) and the pure reopen-cascade constructor (C4) against the
module as it landed in C2-C5. Does not touch `tracker_store.py`'s own
suite (`test_tracker_store.py`, C9a) or the projection suite
(`test_tracker_projection.py`, C9c) — those are concurrent peers' scope.

Spec backlink: pln-sat-03-event-sourced-completio-c270a1
§ Acceptance Criteria AC4, AC5, AC7, AC9; § Tasks C9.
"""

from __future__ import annotations

import subprocess

import pytest

from coordinator_core import tracker_entities
from coordinator_core import tracker_store
from coordinator_core import tracker_transitions as tt

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def _make_git_repo(root):
    """Init a minimal git repository under *root* — mirrors
    `test_tracker_entities.py`'s `_make_git_repo` (`append_event`'s
    `locked_rmw` resolves its lock directory via `git rev-parse
    --git-common-dir`, so a bare non-git `tmp_path` fails there first).
    """
    root.mkdir(parents=True, exist_ok=True)

    def _git(*args):
        subprocess.run(
            ["git"] + list(args),
            cwd=str(root),
            capture_output=True,
            check=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )

    _git("init", "-b", "main")
    _git("config", "user.email", "tracker-transitions-test@claude-klabauter.test")
    _git("config", "user.name", "Tracker Transitions Test")
    _git("config", "commit.gpgsign", "false")
    keeper = root / ".gitkeep"
    keeper.write_text("", encoding="utf-8")
    _git("add", ".gitkeep")
    _git("commit", "-m", "chore: init")
    return root


@pytest.fixture
def repo_root(tmp_path, monkeypatch):
    monkeypatch.setattr(tracker_store, "machine_slug", lambda *a, **kw: "this-machine")
    return _make_git_repo(tmp_path / "repo")


# ---------------------------------------------------------------------------
# AC4 — dedup fires on a replayed reconcile event.
# ---------------------------------------------------------------------------


def test_ac4_code_complete_dedup_on_replayed_evidence_sha(repo_root):
    first = tt.emit_transition(
        "item-1",
        "code_complete",
        "asserted",
        actor="reconciler",
        evidence={"sha": "abc123"},
        tier="auto",
        source_observation_id="obs-1",
        repo_root=repo_root,
    )
    second = tt.emit_transition(
        "item-1",
        "code_complete",
        "asserted",
        actor="reconciler",
        evidence={"sha": "abc123"},
        tier="auto",
        source_observation_id="obs-1",
        repo_root=repo_root,
    )

    assert second["id"] == first["id"]
    events = tracker_store.read_events(repo_root=repo_root)
    assert len(events) == 1


def test_ac4_null_sha_axis_dedup_on_source_observation_id(repo_root):
    first = tt.emit_transition(
        "item-1",
        "qa_verified",
        "verified",
        actor="reconciler",
        evidence=None,
        tier="auto",
        source_observation_id="obs-9",
        repo_root=repo_root,
    )
    second = tt.emit_transition(
        "item-1",
        "qa_verified",
        "verified",
        actor="reconciler",
        evidence=None,
        tier="auto",
        source_observation_id="obs-9",
        repo_root=repo_root,
    )

    assert second["id"] == first["id"]
    events = tracker_store.read_events(repo_root=repo_root)
    assert len(events) == 1


def test_ac4_manual_close_dedup_on_source_observation_id(repo_root):
    first = tt.emit_transition(
        "item-1",
        "manual_close",
        "closed",
        actor="reconciler",
        evidence=None,
        tier="auto",
        source_observation_id="obs-close-1",
        repo_root=repo_root,
    )
    second = tt.emit_transition(
        "item-1",
        "manual_close",
        "closed",
        actor="reconciler",
        evidence=None,
        tier="auto",
        source_observation_id="obs-close-1",
        repo_root=repo_root,
    )

    assert second["id"] == first["id"]
    events = tracker_store.read_events(repo_root=repo_root)
    assert len(events) == 1


# ---------------------------------------------------------------------------
# AC5 — dedup does NOT fire without a source_observation_id.
# ---------------------------------------------------------------------------


def test_ac5_direct_human_event_never_deduped_replayed_three_times(repo_root):
    stored = [
        tt.emit_transition(
            "item-2",
            "manual_close",
            "closed",
            actor="human",
            evidence=None,
            tier="direct",
            source_observation_id=None,
            repo_root=repo_root,
        )
        for _ in range(3)
    ]

    ids = {event["id"] for event in stored}
    assert len(ids) == 3
    events = tracker_store.read_events(repo_root=repo_root)
    assert len(events) == 3


def test_ac5_verify_fail_reverify_records_three_real_events(repo_root):
    verify = tt.emit_transition(
        "item-3",
        "qa_verified",
        "verified",
        actor="human",
        evidence=None,
        tier="direct",
        source_observation_id=None,
        repo_root=repo_root,
    )
    fail = tt.emit_transition(
        "item-3",
        "qa_verified",
        "failed",
        actor="human",
        evidence=None,
        tier="direct",
        source_observation_id=None,
        repo_root=repo_root,
    )
    reverify = tt.emit_transition(
        "item-3",
        "qa_verified",
        "verified",
        actor="human",
        evidence=None,
        tier="direct",
        source_observation_id=None,
        repo_root=repo_root,
    )

    ids = {verify["id"], fail["id"], reverify["id"]}
    assert len(ids) == 3
    events = tracker_store.read_events(repo_root=repo_root)
    assert len(events) == 3


# ---------------------------------------------------------------------------
# AC4/AC5 precedence — the named cell: direct-human code_complete carrying
# evidence.sha and NO source_observation_id is never deduped.
# ---------------------------------------------------------------------------


def test_ac4_ac5_precedence_direct_human_code_complete_with_sha_never_deduped(
    repo_root,
):
    first = tt.emit_transition(
        "item-4",
        "code_complete",
        "asserted",
        actor="human",
        evidence={"sha": "deadbeef"},
        tier="direct",
        source_observation_id=None,
        repo_root=repo_root,
    )
    second = tt.emit_transition(
        "item-4",
        "code_complete",
        "asserted",
        actor="human",
        evidence={"sha": "deadbeef"},
        tier="direct",
        source_observation_id=None,
        repo_root=repo_root,
    )

    assert first["id"] != second["id"]
    events = tracker_store.read_events(repo_root=repo_root)
    assert len(events) == 2


# ---------------------------------------------------------------------------
# AC9 — _build_reopen_cascade is a pure constructor.
# ---------------------------------------------------------------------------


def test_ac9_cascade_neither_axis_asserted_yields_only_manual_close(monkeypatch):
    monkeypatch.setattr(tracker_store, "machine_slug", lambda *a, **kw: "this-machine")
    current_states = {"code_complete": None, "qa_verified": None}

    payloads = tt._build_reopen_cascade("item-x", current_states, actor="human")

    assert len(payloads) == 1
    assert payloads[0]["axis"] == "manual_close"
    assert payloads[0]["to_state"] == "reopened"
    assert payloads[0]["from_state"] is None


def test_ac9_cascade_only_code_complete_asserted(monkeypatch):
    monkeypatch.setattr(tracker_store, "machine_slug", lambda *a, **kw: "this-machine")
    current_states = {"code_complete": "asserted", "qa_verified": None}

    payloads = tt._build_reopen_cascade("item-x", current_states, actor="human")

    assert [p["axis"] for p in payloads] == ["manual_close", "code_complete"]
    assert payloads[1]["to_state"] == "retracted"
    assert payloads[1]["from_state"] == "asserted"


def test_ac9_cascade_only_qa_verified_asserted(monkeypatch):
    monkeypatch.setattr(tracker_store, "machine_slug", lambda *a, **kw: "this-machine")
    current_states = {"code_complete": None, "qa_verified": "verified"}

    payloads = tt._build_reopen_cascade("item-x", current_states, actor="human")

    assert [p["axis"] for p in payloads] == ["manual_close", "qa_verified"]
    assert payloads[1]["to_state"] == "retracted"
    assert payloads[1]["from_state"] == "verified"


def test_ac9_cascade_both_axes_asserted_ordering(monkeypatch):
    monkeypatch.setattr(tracker_store, "machine_slug", lambda *a, **kw: "this-machine")
    current_states = {"code_complete": "asserted", "qa_verified": "verified"}

    payloads = tt._build_reopen_cascade("item-x", current_states, actor="human")

    assert [p["axis"] for p in payloads] == [
        "manual_close",
        "code_complete",
        "qa_verified",
    ]
    assert payloads[0]["to_state"] == "reopened"
    assert payloads[1]["to_state"] == "retracted"
    assert payloads[2]["to_state"] == "retracted"


def test_ac9_cascade_emitted_through_exactly_one_append_events_call(
    repo_root, monkeypatch
):
    calls = []
    orig_append_events = tracker_store.append_events

    def _spy_append_events(events, *, repo_root):
        calls.append(list(events))
        return orig_append_events(events, repo_root=repo_root)

    append_event_calls = []
    orig_append_event = tracker_store.append_event

    def _spy_append_event(event, *, repo_root):
        append_event_calls.append(event)
        return orig_append_event(event, repo_root=repo_root)

    monkeypatch.setattr(tracker_store, "append_events", _spy_append_events)
    monkeypatch.setattr(tracker_store, "append_event", _spy_append_event)

    # Prime the item so both retractable axes read as currently asserted.
    tt.emit_transition(
        "item-cascade",
        "code_complete",
        "asserted",
        actor="human",
        evidence={"sha": "sha-1"},
        tier="direct",
        source_observation_id=None,
        repo_root=repo_root,
    )
    tt.emit_transition(
        "item-cascade",
        "qa_verified",
        "verified",
        actor="human",
        evidence=None,
        tier="direct",
        source_observation_id=None,
        repo_root=repo_root,
    )
    # Reset spies: only the cascade call itself is under test below.
    calls.clear()
    append_event_calls.clear()

    result = tt.reopen_cascade("item-cascade", actor="human", repo_root=repo_root)

    assert len(calls) == 1
    assert len(calls[0]) == 3
    assert append_event_calls == []
    assert len(result) == 3


# ---------------------------------------------------------------------------
# AC7 — manual_close with to_state='reopened' and from_state=None is legal.
# ---------------------------------------------------------------------------


def test_ac7_manual_close_reopened_null_from_state_is_accepted(repo_root):
    stored = tt.emit_transition(
        "item-5",
        "manual_close",
        "reopened",
        from_state=None,
        actor="human",
        evidence=None,
        tier="direct",
        source_observation_id=None,
        repo_root=repo_root,
    )

    assert stored["to_state"] == "reopened"
    assert stored["from_state"] is None


# ---------------------------------------------------------------------------
# Closed axis enum, applied_at semantics, and suggest-tier invisibility.
# ---------------------------------------------------------------------------


def test_unknown_axis_rejected_with_typed_error():
    with pytest.raises(tt.TrackerTransitionError):
        tt.transition_event(
            "item-6",
            "not_a_real_axis",
            "asserted",
            actor="human",
            evidence=None,
            tier="direct",
            source_observation_id=None,
        )


def test_applied_at_null_only_for_suggest_tier(repo_root):
    suggest = tt.emit_transition(
        "item-7",
        "code_complete",
        "asserted",
        actor="auto",
        evidence={"sha": "sha-suggest"},
        tier="suggest",
        source_observation_id="obs-suggest",
        repo_root=repo_root,
    )
    direct = tt.emit_transition(
        "item-7",
        "qa_verified",
        "verified",
        actor="human",
        evidence=None,
        tier="direct",
        source_observation_id=None,
        repo_root=repo_root,
    )

    assert suggest["applied_at"] is None
    assert direct["applied_at"] == direct["observed_at"]


# ---------------------------------------------------------------------------
# C1 — closed tier vocabulary (TRANSITION_TIERS), and the queued (`deferred`)
# value's applied_at/AC3 four-row table.
# ---------------------------------------------------------------------------


def test_unknown_tier_rejected_with_typed_error():
    with pytest.raises(tt.TrackerTransitionError):
        tt.transition_event(
            "item-tier-1",
            "manual_close",
            "closed",
            actor="human",
            evidence=None,
            tier="not_a_real_tier",
            source_observation_id=None,
        )


def test_ac3_four_row_tier_table_through_emit_transition(repo_root):
    """AC3's regression lock (director review, F8): each tier's event is
    emitted through `emit_transition`, not `_emit` directly, so the row
    proves the constructor's tier gate actually runs on this path."""
    rows = {}
    for index, tier in enumerate(sorted(tt.TRANSITION_TIERS)):
        rows[tier] = tt.emit_transition(
            f"item-tier-row-{index}",
            "manual_close",
            "closed",
            actor="human" if tier in ("direct", "deferred") else "auto",
            evidence=None,
            tier=tier,
            source_observation_id=None if tier == "direct" else f"obs-{tier}",
            repo_root=repo_root,
        )

    assert set(rows) == {"auto", "suggest", "direct", "deferred"}
    assert rows["auto"]["applied_at"] == rows["auto"]["observed_at"]
    assert rows["direct"]["applied_at"] == rows["direct"]["observed_at"]
    assert rows["suggest"]["applied_at"] is None
    assert rows["deferred"]["applied_at"] is None


def test_ac2_direct_tier_reopen_cascade_still_stamps_applied_at(repo_root):
    """AC2 regression this chunk exists to avoid introducing: a `direct`-tier
    reopen-cascade event (`_build_reopen_cascade`/`_REOPEN_TIER`) must still
    stamp `applied_at`, not fall into the newly widened null set."""
    payloads = tt._build_reopen_cascade(
        "item-tier-reopen",
        {"code_complete": "asserted", "qa_verified": "verified"},
        actor="human",
    )
    assert all(payload["tier"] == "direct" for payload in payloads)

    stored = tt._emit_batch(payloads, repo_root=repo_root)
    assert stored
    for event in stored:
        assert event["tier"] == "direct"
        assert event["applied_at"] == event["observed_at"]
        assert event["applied_at"] is not None


# ---------------------------------------------------------------------------
# Review: coordinator:code-reviewer, P1 — `_find_existing_by_address` scans
# the SAME shared shard `tracker_entities.py` writes into (and this
# module's own `kind: "snapshot"` events land in), and neither shape
# carries `item_id`/`axis`/`to_state`. This regression fixture writes one
# of each onto the shard BEFORE emitting a transition — the mixed-shard
# scan the pre-fix `_dedup_check_address` unguarded bracket access would
# have crashed on with `KeyError: 'item_id'` (or `'axis'`/`'to_state'`).
# ---------------------------------------------------------------------------


def test_mixed_shard_entity_and_snapshot_events_do_not_crash_transition_dedup(
    repo_root,
):
    tracker_entities.emit_project_created(
        "proj-1", name="Project One", repo_root=repo_root
    )

    snapshot_payload = tt.build_snapshot_event(
        "item-snap",
        "code_complete",
        folded_event_ids=["evt-fake-folded"],
        as_of_sequence=1,
        as_of_applied_at=None,
        folded_to_state="asserted",
    )
    tt.emit_snapshot_event(snapshot_payload, repo_root=repo_root)

    first = tt.emit_transition(
        "item-9",
        "code_complete",
        "asserted",
        actor="reconciler",
        evidence={"sha": "sha-mixed"},
        tier="auto",
        source_observation_id="obs-mixed",
        repo_root=repo_root,
    )
    second = tt.emit_transition(
        "item-9",
        "code_complete",
        "asserted",
        actor="reconciler",
        evidence={"sha": "sha-mixed"},
        tier="auto",
        source_observation_id="obs-mixed",
        repo_root=repo_root,
    )

    assert second["id"] == first["id"]
    events = [
        e
        for e in tracker_store.read_events(repo_root=repo_root)
        if e.get("item_id") == "item-9"
    ]
    assert len(events) == 1


# ---------------------------------------------------------------------------
# Review: coordinator:code-reviewer, P3 — `_emit_batch`'s partial-dedup
# branch (an existing-match resolved alongside a genuinely-new payload in
# the SAME batch) has no current production caller (`reopen_cascade` always
# passes `source_observation_id=None`, which never dedups), so it is
# exercised directly here rather than through `reopen_cascade`.
# ---------------------------------------------------------------------------


def test_emit_batch_partial_dedup_keeps_only_new_payloads_in_append_call(
    repo_root, monkeypatch
):
    calls = []
    orig_append_events = tracker_store.append_events

    def _spy_append_events(events, *, repo_root):
        calls.append(list(events))
        return orig_append_events(events, repo_root=repo_root)

    monkeypatch.setattr(tracker_store, "append_events", _spy_append_events)

    existing = tt.emit_transition(
        "item-batch",
        "code_complete",
        "asserted",
        actor="reconciler",
        evidence={"sha": "sha-batch"},
        tier="auto",
        source_observation_id="obs-batch",
        repo_root=repo_root,
    )

    dup_payload = tt.transition_event(
        "item-batch",
        "code_complete",
        "asserted",
        actor="reconciler",
        evidence={"sha": "sha-batch"},
        tier="auto",
        source_observation_id="obs-batch",
    )
    new_payload = tt.transition_event(
        "item-batch",
        "qa_verified",
        "verified",
        actor="reconciler",
        evidence=None,
        tier="auto",
        source_observation_id="obs-batch-2",
    )

    result = tt._emit_batch([dup_payload, new_payload], repo_root=repo_root)

    assert result[0]["id"] == existing["id"]
    assert result[1]["axis"] == "qa_verified"
    assert len(calls) == 1
    assert len(calls[0]) == 1
    assert calls[0][0]["axis"] == "qa_verified"


# ---------------------------------------------------------------------------
# C3 (sat-04) — retract generation closes the revert-of-revert collision.
# See docs/plans/2026-08-18-sat-04-completion-axis-policy.md § D8, AC5,
# AC5b, AC5c, AC5d, AC6, AC7a, AC11.
# ---------------------------------------------------------------------------


def test_ac5_revert_of_revert_re_assert_is_a_distinct_event(repo_root):
    assert_a = tt.emit_transition(
        "item-gen-1",
        "code_complete",
        "asserted",
        actor="reconciler",
        evidence={"sha": "sha-a"},
        tier="auto",
        source_observation_id="obs-a",
        repo_root=repo_root,
    )
    tt.emit_transition(
        "item-gen-1",
        "code_complete",
        "retracted",
        from_state="asserted",
        actor="reconciler",
        evidence={"sha": "revert-sha-1"},
        tier="auto",
        source_observation_id="obs-r1",
        repo_root=repo_root,
    )
    re_assert_a = tt.emit_transition(
        "item-gen-1",
        "code_complete",
        "asserted",
        actor="reconciler",
        evidence={"sha": "sha-a"},
        tier="auto",
        source_observation_id="obs-a-2",
        repo_root=repo_root,
    )

    assert re_assert_a["id"] != assert_a["id"]
    from coordinator_core import tracker_projection

    assert (
        tracker_projection.current_state("item-gen-1", "code_complete", repo_root=repo_root)
        == "asserted"
    )


def test_ac5b_two_full_revert_cycles_yield_five_distinct_events(repo_root):
    events_out = []
    events_out.append(
        tt.emit_transition(
            "item-gen-2",
            "code_complete",
            "asserted",
            actor="reconciler",
            evidence={"sha": "sha-a"},
            tier="auto",
            source_observation_id="obs-a0",
            repo_root=repo_root,
        )
    )
    events_out.append(
        tt.emit_transition(
            "item-gen-2",
            "code_complete",
            "retracted",
            from_state="asserted",
            actor="reconciler",
            evidence={"sha": "revert-sha-1"},
            tier="auto",
            source_observation_id="obs-r1",
            repo_root=repo_root,
        )
    )
    events_out.append(
        tt.emit_transition(
            "item-gen-2",
            "code_complete",
            "asserted",
            actor="reconciler",
            evidence={"sha": "sha-a"},
            tier="auto",
            source_observation_id="obs-a1",
            repo_root=repo_root,
        )
    )
    events_out.append(
        tt.emit_transition(
            "item-gen-2",
            "code_complete",
            "retracted",
            from_state="asserted",
            actor="reconciler",
            evidence={"sha": "revert-sha-2"},
            tier="auto",
            source_observation_id="obs-r2",
            repo_root=repo_root,
        )
    )
    events_out.append(
        tt.emit_transition(
            "item-gen-2",
            "code_complete",
            "asserted",
            actor="reconciler",
            evidence={"sha": "sha-a"},
            tier="auto",
            source_observation_id="obs-a2",
            repo_root=repo_root,
        )
    )

    ids = {event["id"] for event in events_out}
    assert len(ids) == 5

    stored = [
        e
        for e in tracker_store.read_events(repo_root=repo_root)
        if e.get("item_id") == "item-gen-2"
    ]
    assert len(stored) == 5

    from coordinator_core import tracker_projection

    assert (
        tracker_projection.current_state("item-gen-2", "code_complete", repo_root=repo_root)
        == "asserted"
    )


def test_ac5c_generation_is_not_a_caller_supplied_field(repo_root):
    with pytest.raises(TypeError):
        tt.transition_event(
            "item-gen-3",
            "code_complete",
            "asserted",
            actor="human",
            evidence={"sha": "sha-gen"},
            tier="direct",
            source_observation_id="obs-gen",
            generation=5,
        )
    with pytest.raises(TypeError):
        tt.emit_transition(
            "item-gen-3",
            "code_complete",
            "asserted",
            actor="human",
            evidence={"sha": "sha-gen"},
            tier="direct",
            source_observation_id="obs-gen",
            generation=5,
            repo_root=repo_root,
        )


def test_ac5d_emit_batch_advances_generation_within_batch(repo_root):
    payload_1 = tt.transition_event(
        "item-gen-4",
        "code_complete",
        "retracted",
        from_state="asserted",
        actor="reconciler",
        evidence={"sha": "revert-sha-shared"},
        tier="auto",
        source_observation_id="obs-r1",
    )
    payload_2 = tt.transition_event(
        "item-gen-4",
        "code_complete",
        "retracted",
        from_state="asserted",
        actor="reconciler",
        evidence={"sha": "revert-sha-shared"},
        tier="auto",
        source_observation_id="obs-r2",
    )

    result = tt._emit_batch([payload_1, payload_2], repo_root=repo_root)

    assert result[0]["id"] != result[1]["id"]
    assert result[0]["generation"] == 0
    assert result[1]["generation"] == 1
    stored = [
        e
        for e in tracker_store.read_events(repo_root=repo_root)
        if e.get("item_id") == "item-gen-4"
    ]
    assert len(stored) == 2


def test_ac7a_pre_c3_event_missing_generation_field_reads_as_zero(repo_root):
    pre_c3_event = {
        "id": "evt-this-machine-precthreeabc",
        "item_id": "item-gen-5",
        "axis": "code_complete",
        "from_state": None,
        "to_state": "asserted",
        "actor": "reconciler",
        "evidence": {"sha": "sha-pre"},
        "tier": "auto",
        "source_observation_id": "obs-pre",
        "observed_at": "2026-01-01T00:00:00.000000+00:00",
        "applied_at": "2026-01-01T00:00:00.000000+00:00",
        "schema_version": 1,
        # deliberately no "generation" field — pre-C3 shape.
    }
    tracker_store.append_event(pre_c3_event, repo_root=repo_root)

    # Re-observed at generation 0 (no retracts stored yet): dedups to the
    # pre-C3 event, whose absent generation reads as 0.
    reobserved = tt.emit_transition(
        "item-gen-5",
        "code_complete",
        "asserted",
        actor="reconciler",
        evidence={"sha": "sha-pre"},
        tier="auto",
        source_observation_id="obs-pre",
        repo_root=repo_root,
    )
    assert reobserved["id"] == pre_c3_event["id"]
    stored = [
        e
        for e in tracker_store.read_events(repo_root=repo_root)
        if e.get("item_id") == "item-gen-5"
    ]
    assert len(stored) == 1

    tt.emit_transition(
        "item-gen-5",
        "code_complete",
        "retracted",
        from_state="asserted",
        actor="reconciler",
        evidence={"sha": "revert-sha-pre"},
        tier="auto",
        source_observation_id="obs-pre-retract",
        repo_root=repo_root,
    )

    # Post-retract re-assert lands at generation 1 — appends rather than
    # matching the generation-0 pre-C3 event (the fix this chunk exists
    # to admit).
    post_retract_reassert = tt.emit_transition(
        "item-gen-5",
        "code_complete",
        "asserted",
        actor="reconciler",
        evidence={"sha": "sha-pre"},
        tier="auto",
        source_observation_id="obs-pre",
        repo_root=repo_root,
    )
    assert post_retract_reassert["id"] != pre_c3_event["id"]
    assert post_retract_reassert["generation"] == 1
    stored = [
        e
        for e in tracker_store.read_events(repo_root=repo_root)
        if e.get("item_id") == "item-gen-5"
    ]
    assert len(stored) == 3


def test_ac6_mint_and_dedup_addresses_change_together_no_duplicate_id_error(repo_root):
    tt.emit_transition(
        "item-gen-6",
        "code_complete",
        "asserted",
        actor="reconciler",
        evidence={"sha": "sha-a"},
        tier="auto",
        source_observation_id="obs-a0",
        repo_root=repo_root,
    )
    tt.emit_transition(
        "item-gen-6",
        "code_complete",
        "retracted",
        from_state="asserted",
        actor="reconciler",
        evidence={"sha": "revert-sha-1"},
        tier="auto",
        source_observation_id="obs-r1",
        repo_root=repo_root,
    )
    # Must not raise TrackerStoreDuplicateIdError: the mint address and the
    # dedup-check address both carry the same generation (AC6), so this
    # re-assert mints a distinct id rather than colliding with the
    # cycle-0 assert's id at append time.
    re_assert = tt.emit_transition(
        "item-gen-6",
        "code_complete",
        "asserted",
        actor="reconciler",
        evidence={"sha": "sha-a"},
        tier="auto",
        source_observation_id="obs-a1",
        repo_root=repo_root,
    )
    stored = [
        e
        for e in tracker_store.read_events(repo_root=repo_root)
        if e.get("item_id") == "item-gen-6"
    ]
    assert len(stored) == 3
    assert re_assert in stored


def test_ac11_replay_inside_one_generation_still_dedups_to_a_single_event(repo_root):
    tt.emit_transition(
        "item-gen-7",
        "code_complete",
        "asserted",
        actor="reconciler",
        evidence={"sha": "sha-a"},
        tier="auto",
        source_observation_id="obs-a0",
        repo_root=repo_root,
    )
    tt.emit_transition(
        "item-gen-7",
        "code_complete",
        "retracted",
        from_state="asserted",
        actor="reconciler",
        evidence={"sha": "revert-sha-1"},
        tier="auto",
        source_observation_id="obs-r1",
        repo_root=repo_root,
    )
    # Generation is now 1. Two racing appends of the SAME re-assert
    # observation (same evidence.sha, same generation) must still collide
    # to a single stored event — DR-241 bound (i)'s collision-as-guard,
    # unweakened by the generation addition (D7).
    first = tt.emit_transition(
        "item-gen-7",
        "code_complete",
        "asserted",
        actor="reconciler",
        evidence={"sha": "sha-a"},
        tier="auto",
        source_observation_id="obs-a1",
        repo_root=repo_root,
    )
    second = tt.emit_transition(
        "item-gen-7",
        "code_complete",
        "asserted",
        actor="reconciler",
        evidence={"sha": "sha-a"},
        tier="auto",
        source_observation_id="obs-a1",
        repo_root=repo_root,
    )
    assert first["id"] == second["id"]
    stored = [
        e
        for e in tracker_store.read_events(repo_root=repo_root)
        if e.get("item_id") == "item-gen-7"
    ]
    assert len(stored) == 3


def test_suggest_tier_event_invisible_to_read_events(repo_root):
    tt.emit_transition(
        "item-8",
        "code_complete",
        "asserted",
        actor="auto",
        evidence={"sha": "sha-hidden"},
        tier="suggest",
        source_observation_id="obs-hidden",
        repo_root=repo_root,
    )

    events = tracker_store.read_events(repo_root=repo_root)
    assert events == []


# ---------------------------------------------------------------------------
# Post-review fix (BLOCKED finding, state/subagent-share/1c3928ee-abbf-49ce-
# b35c-d6727a4b903c/review-s1-transitions.md): a retract counts itself in
# `_code_complete_retract_generation`'s sum, so addressing a retract on its
# own generation makes the address unstable across re-observation. The
# retract arm of `_code_complete_dedup_key` now addresses on
# `(source_observation_id, evidence.sha)` instead, leaving the generation
# load-bearing for the assert arm only.
# ---------------------------------------------------------------------------


def test_retract_re_observed_dedups_to_the_same_stored_event(repo_root):
    """The regression this fix exists for. Re-emitting the SAME retract
    observation (same item_id/axis/to_state/evidence.sha/
    source_observation_id) must resolve to the already-stored event, not
    mint a second one — this is exactly AC14's "exactly one retract event
    is ever stored" contract, which the pre-fix generation-keyed address
    broke (the retract counted itself the second time round, so its
    address recomputed to a different generation and never matched).
    """
    first = tt.emit_transition(
        "item-retract-replay",
        "code_complete",
        "retracted",
        from_state="asserted",
        actor="reconciler",
        evidence={"sha": "revert-1"},
        tier="auto",
        source_observation_id="obs-r1",
        repo_root=repo_root,
    )
    second = tt.emit_transition(
        "item-retract-replay",
        "code_complete",
        "retracted",
        from_state="asserted",
        actor="reconciler",
        evidence={"sha": "revert-1"},
        tier="auto",
        source_observation_id="obs-r1",
        repo_root=repo_root,
    )

    assert second["id"] == first["id"]
    stored = [
        e
        for e in tracker_store.read_events(repo_root=repo_root)
        if e.get("item_id") == "item-retract-replay" and e.get("to_state") == "retracted"
    ]
    assert len(stored) == 1


def test_out_of_order_retract_replay_still_dedups_to_its_own_original(repo_root):
    """Two distinct retracts (different sha/observation) land as two
    events; replaying the FIRST one again — after the second has already
    been stored — must still dedup to R1's own original event, not mint a
    duplicate or accidentally match R2's.
    """
    r1 = tt.emit_transition(
        "item-retract-out-of-order",
        "code_complete",
        "retracted",
        from_state="asserted",
        actor="reconciler",
        evidence={"sha": "revert-r1"},
        tier="auto",
        source_observation_id="obs-r1",
        repo_root=repo_root,
    )
    tt.emit_transition(
        "item-retract-out-of-order",
        "code_complete",
        "retracted",
        from_state="asserted",
        actor="reconciler",
        evidence={"sha": "revert-r2"},
        tier="auto",
        source_observation_id="obs-r2",
        repo_root=repo_root,
    )
    r1_replay = tt.emit_transition(
        "item-retract-out-of-order",
        "code_complete",
        "retracted",
        from_state="asserted",
        actor="reconciler",
        evidence={"sha": "revert-r1"},
        tier="auto",
        source_observation_id="obs-r1",
        repo_root=repo_root,
    )

    assert r1_replay["id"] == r1["id"]
    stored = [
        e
        for e in tracker_store.read_events(repo_root=repo_root)
        if e.get("item_id") == "item-retract-out-of-order"
        and e.get("to_state") == "retracted"
    ]
    assert len(stored) == 2


def test_emit_batch_duplicate_retract_does_not_advance_counter_for_later_payloads(
    repo_root,
):
    """A batch containing a retract that matches an already-stored event
    must not inflate the running generation counter for later payloads in
    the same batch (the secondary, dormant defect from the same review
    finding) — exercised directly against `_emit_batch` since
    `reopen_cascade` is the only production caller today and always passes
    `source_observation_id=None`, which never dedups.
    """
    existing_retract = tt.emit_transition(
        "item-batch-retract-gen",
        "code_complete",
        "retracted",
        from_state="asserted",
        actor="reconciler",
        evidence={"sha": "revert-existing"},
        tier="auto",
        source_observation_id="obs-existing",
        repo_root=repo_root,
    )

    dup_payload = tt.transition_event(
        "item-batch-retract-gen",
        "code_complete",
        "retracted",
        from_state="asserted",
        actor="reconciler",
        evidence={"sha": "revert-existing"},
        tier="auto",
        source_observation_id="obs-existing",
    )
    assert_payload = tt.transition_event(
        "item-batch-retract-gen",
        "code_complete",
        "asserted",
        actor="reconciler",
        evidence={"sha": "sha-fresh"},
        tier="auto",
        source_observation_id="obs-fresh",
    )

    result = tt._emit_batch([dup_payload, assert_payload], repo_root=repo_root)

    assert result[0]["id"] == existing_retract["id"]
    # The batch had exactly one PRIOR stored retract (from the setup call
    # above) before either payload in this batch was processed; since the
    # duplicate retract in this batch was not newly appended, the running
    # counter must not advance past that prior count, so the assert
    # payload stamps generation 1 (count of retracts stored BEFORE this
    # batch), not 2.
    assert result[1]["generation"] == 1


# ---------------------------------------------------------------------------
# C3 — withdrawal_event: a kind-discriminated event naming a queued event's
# id, same-shard prefix-validated at construction.
# ---------------------------------------------------------------------------


def test_withdrawal_event_appends_and_names_the_withdrawn_id(repo_root):
    queued = tt.emit_transition(
        "item-withdraw-1",
        "qa_verified",
        "verified",
        actor="human",
        evidence=None,
        tier="deferred",
        source_observation_id="obs-withdraw-1",
        repo_root=repo_root,
    )

    payload = tt.withdrawal_event(queued["id"], actor="human")
    assert payload == {
        "kind": "withdrawal",
        "withdraws": queued["id"],
        "actor": "human",
    }

    withdrawal = tt.emit_withdrawal_event(payload, repo_root=repo_root)

    assert withdrawal["kind"] == "withdrawal"
    assert withdrawal["withdraws"] == queued["id"]
    assert withdrawal["applied_at"] == withdrawal["observed_at"]
    assert withdrawal["id"] != queued["id"]

    stored_ids = {event["id"] for event in tracker_store.read_events(repo_root=repo_root)}
    # `read_events` filters to `applied_at`-populated events; the queued
    # (deferred-tier) row is invisible there by construction, but the
    # withdrawal row (a direct, always-applied append) is visible.
    assert withdrawal["id"] in stored_ids


def test_withdrawal_event_rejects_a_foreign_shard_id():
    with pytest.raises(tt.TrackerTransitionError):
        tt.withdrawal_event("evt-other-machine-abc123", actor="human")


def test_withdrawal_event_rejects_a_fold_marker_shaped_id():
    # Fold-marker ids are shaped `<slug>-fold-<digest>`, with NO `evt-`
    # prefix — a generic "split on `-` and take the second segment" would
    # misparse this as same-shard; the prefix test must not.
    with pytest.raises(tt.TrackerTransitionError):
        tt.withdrawal_event("this-machine-fold-abc123", actor="human")


def test_withdrawal_event_accepts_a_same_shard_id(monkeypatch):
    monkeypatch.setattr(tracker_store, "machine_slug", lambda *a, **kw: "this-machine")
    payload = tt.withdrawal_event("evt-this-machine-abc123", actor="human")
    assert payload["withdraws"] == "evt-this-machine-abc123"


# ---------------------------------------------------------------------------
# C5 — evidence.probe per DR-closure-fidelity-tier-axis D2/D3, plus the
# additivity proof for a widen that adds no new schema key.
# ---------------------------------------------------------------------------


def test_probe_result_error_is_refused_with_typed_error():
    with pytest.raises(tt.TrackerTransitionError):
        tt.transition_event(
            "item-probe-1",
            "code_complete",
            "asserted",
            actor="reconciler",
            evidence={"sha": "abc123", "probe": {"probe_result": "error"}},
            tier="suggest",
            source_observation_id="obs-probe-1",
        )


def test_probe_present_but_not_error_is_accepted():
    payload = tt.transition_event(
        "item-probe-2",
        "code_complete",
        "asserted",
        actor="reconciler",
        evidence={"sha": "abc123", "probe": {"probe_result": "ok"}},
        tier="suggest",
        source_observation_id="obs-probe-2",
    )
    assert payload["evidence"]["probe"]["probe_result"] == "ok"


def test_evidence_with_no_probe_key_is_unaffected_by_the_guard():
    payload = tt.transition_event(
        "item-probe-3",
        "code_complete",
        "asserted",
        actor="reconciler",
        evidence={"sha": "abc123"},
        tier="suggest",
        source_observation_id="obs-probe-3",
    )
    assert "probe" not in payload["evidence"]


def test_null_evidence_round_trips_byte_identical_and_idempotency_unchanged(
    repo_root,
):
    """Additivity proof, arm 1: `evidence: null`. Round-trips through
    `emit_transition`/`_emit` and reads back identical, and the
    (source_observation_id, evidence_sha) null-SHA dedup arm still keys on
    `source_observation_id` alone — replaying the same payload dedups to
    the SAME stored event, exactly as it did before `probe` existed as a
    concept anywhere in this module.
    """
    first = tt.emit_transition(
        "item-probe-null",
        "qa_verified",
        "verified",
        actor="reconciler",
        evidence=None,
        tier="auto",
        source_observation_id="obs-probe-null",
        repo_root=repo_root,
    )
    assert first["evidence"] is None

    events = tracker_store.read_events(repo_root=repo_root)
    assert len(events) == 1
    stored = events[0]
    assert stored["evidence"] is None
    assert stored == first

    second = tt.emit_transition(
        "item-probe-null",
        "qa_verified",
        "verified",
        actor="reconciler",
        evidence=None,
        tier="auto",
        source_observation_id="obs-probe-null",
        repo_root=repo_root,
    )
    assert second["id"] == first["id"]
    assert len(tracker_store.read_events(repo_root=repo_root)) == 1


def test_full_six_key_evidence_round_trips_byte_identical_and_idempotency_unchanged(
    repo_root,
):
    """Additivity proof, arm 2: the full section 4.3 six-key `evidence`
    object, with no `probe` key present. Round-trips through
    `emit_transition`/`_emit` and reads back identical, and the
    (generation, evidence.sha) `code_complete` assert-arm dedup key is
    unchanged — it is not keyed on `probe`, so replaying the SAME six-key
    evidence object still dedups to the SAME stored event.
    """
    six_key_evidence = {
        "kind": "commit",
        "sha": "deadbeef",
        "reachable_default_branch": True,
        "reverts_sha": None,
        "citation": "some/path.py:42",
        "confidence": 0.9,
    }

    first = tt.emit_transition(
        "item-probe-six-key",
        "code_complete",
        "asserted",
        actor="reconciler",
        evidence=six_key_evidence,
        tier="auto",
        source_observation_id="obs-probe-six-key",
        repo_root=repo_root,
    )
    assert first["evidence"] == six_key_evidence
    assert "probe" not in first["evidence"]

    events = tracker_store.read_events(repo_root=repo_root)
    assert len(events) == 1
    stored = events[0]
    assert stored["evidence"] == six_key_evidence
    assert stored == first

    second = tt.emit_transition(
        "item-probe-six-key",
        "code_complete",
        "asserted",
        actor="reconciler",
        evidence=dict(six_key_evidence),
        tier="auto",
        source_observation_id="obs-probe-six-key",
        repo_root=repo_root,
    )
    assert second["id"] == first["id"]
    assert len(tracker_store.read_events(repo_root=repo_root)) == 1


def test_withdrawal_and_withdrawn_row_both_present_in_shard(repo_root):
    queued = tt.emit_transition(
        "item-withdraw-2",
        "manual_close",
        "closed",
        actor="human",
        evidence=None,
        tier="deferred",
        source_observation_id="obs-withdraw-2",
        repo_root=repo_root,
    )
    withdrawal = tt.emit_withdrawal_event(
        tt.withdrawal_event(queued["id"], actor="human"), repo_root=repo_root
    )

    shard_lines = tracker_store.shard_path(repo_root).read_text(
        encoding="utf-8"
    ).splitlines()
    assert any(f'"id": "{queued["id"]}"' in line for line in shard_lines if line.strip())
    assert any(
        f'"id": "{withdrawal["id"]}"' in line for line in shard_lines if line.strip()
    )

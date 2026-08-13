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

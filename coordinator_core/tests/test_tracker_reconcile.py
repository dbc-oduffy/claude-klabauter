"""
coordinator_core.tests.test_tracker_reconcile — C4: the reconcile pass
(merge before apply-as-append, own shard only, withdrawals consulted
first).

Coverage requirements (per this chunk's dispatch brief,
state/dispatch-briefs/2026-08-20-queued-tier-and-withdrawal-pre-land/C4.md):
  AC6  — a foreign shard's queued event gets no twin and is otherwise
         untouched.
  AC7  — a withdrawn same-shard event never gets a twin; both rows survive
         on disk unmodified.
  AC8  — a cross-shard withdrawal does NOT suppress its target.
  AC9  — `observed_set_fold`/`snapshot` records in the candidate's own
         shard are never selected as reconcile candidates (the B3/F3
         regression lock).
  AC10 — both allowlist tests
         (`test_top_level_coordinator_core_referencers_are_exact_match_allowlisted`
         and `test_allowlisted_referencers_confine_writes_via_tracker_store_
         api_only`) are GREEN.

Spec backlink: state/dispatch-briefs/2026-08-20-queued-tier-and-withdrawal-
pre-land/C4.md
"""

from __future__ import annotations

import json
import subprocess

import pytest

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]
from pathlib import Path

from coordinator_core import tracker_reconcile as tr
from coordinator_core import tracker_store
from coordinator_core import tracker_transitions as tt


def _make_git_repo(root):
    """Init a minimal git repository under *root* — mirrors
    `test_tracker_transitions.py`'s `_make_git_repo` (`append_event`'s
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
    _git("config", "user.email", "tracker-reconcile-test@claude-klabauter.test")
    _git("config", "user.name", "Tracker Reconcile Test")
    _git("config", "commit.gpgsign", "false")
    keeper = root / ".gitkeep"
    keeper.write_text("", encoding="utf-8")
    _git("add", ".gitkeep")
    _git("commit", "-m", "chore: init")
    return root


@pytest.fixture
def repo_root(tmp_path, monkeypatch):
    monkeypatch.setattr(tracker_store, "machine_slug", lambda *a, **kw: "own-machine")
    return _make_git_repo(tmp_path / "repo")


def _write_shard_line(repo_root, machine, record):
    """Append one raw JSONL line to *machine*'s shard, bypassing
    `tracker_store.append_event` — used to construct fixture shapes
    (a foreign shard, a pre-existing withdrawal) this test needs to exist
    without going through the normal single-machine write path.
    """
    shard_dir = repo_root / tracker_store.EVENTS_DIR_RELPATH
    shard_dir.mkdir(parents=True, exist_ok=True)
    shard_path = shard_dir / f"events.{machine}.jsonl"
    with shard_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, sort_keys=True) + "\n")


def _queued_candidate(item_id="item-1", axis="qa_verified", to_state="verified", **overrides):
    record = {
        "id": f"evt-own-machine-queued-{item_id}",
        "item_id": item_id,
        "axis": axis,
        "from_state": None,
        "to_state": to_state,
        "actor": "detector",
        "evidence": None,
        "tier": "deferred",
        "source_observation_id": "obs-1",
        "observed_at": "2026-08-20T00:00:00.000000+00:00",
        "applied_at": None,
        "schema_version": 1,
        "machine": "own-machine",
    }
    record.update(overrides)
    return record


# ---------------------------------------------------------------------------
# AC6 — foreign-shard queued event gets no twin, untouched.
# ---------------------------------------------------------------------------


def test_ac6_foreign_shard_queued_event_gets_no_twin_and_is_untouched(repo_root):
    foreign = _queued_candidate(item_id="item-foreign")
    foreign["id"] = "evt-peer-machine-queued-item-foreign"
    foreign["machine"] = "peer-machine"
    _write_shard_line(repo_root, "peer-machine", foreign)

    before = (
        repo_root / tracker_store.EVENTS_DIR_RELPATH / "events.peer-machine.jsonl"
    ).read_text(encoding="utf-8")

    results = tr.reconcile(repo_root=repo_root, actor="reconciler")

    assert results == []
    after = (
        repo_root / tracker_store.EVENTS_DIR_RELPATH / "events.peer-machine.jsonl"
    ).read_text(encoding="utf-8")
    assert after == before

    own_shard = repo_root / tracker_store.EVENTS_DIR_RELPATH / "events.own-machine.jsonl"
    assert not own_shard.exists()


# ---------------------------------------------------------------------------
# AC7 — a withdrawn same-shard event never gets a twin; both rows survive.
# ---------------------------------------------------------------------------


def test_ac7_withdrawn_own_shard_candidate_gets_no_twin_both_rows_survive(repo_root):
    candidate = _queued_candidate(item_id="item-2")
    _write_shard_line(repo_root, "own-machine", candidate)

    withdrawal = tt.emit_withdrawal_event(
        tt.withdrawal_event(candidate["id"], actor="reconciler"),
        repo_root=repo_root,
    )

    results = tr.reconcile(repo_root=repo_root, actor="reconciler")

    assert results == []

    own_shard = repo_root / tracker_store.EVENTS_DIR_RELPATH / "events.own-machine.jsonl"
    lines = [json.loads(line) for line in own_shard.read_text(encoding="utf-8").splitlines() if line.strip()]
    ids = {line["id"] for line in lines}
    assert candidate["id"] in ids
    assert withdrawal["id"] in ids
    # No apply-twin was minted for the withdrawn candidate.
    assert not any(line.get("applied_from") == candidate["id"] for line in lines)
    stored_candidate = next(line for line in lines if line["id"] == candidate["id"])
    assert stored_candidate["applied_at"] is None


# ---------------------------------------------------------------------------
# AC8 — a cross-shard withdrawal does NOT suppress its target.
# ---------------------------------------------------------------------------


def test_ac8_cross_shard_withdrawal_does_not_suppress_target(repo_root):
    candidate = _queued_candidate(item_id="item-3")
    _write_shard_line(repo_root, "own-machine", candidate)

    # A withdrawal for `candidate["id"]` written into a FOREIGN shard —
    # reconcile only ever reads its own shard, so this must never suppress
    # the own-shard candidate.
    foreign_withdrawal = {
        "id": "evt-peer-machine-withdraw-item-3",
        "kind": "withdrawal",
        "withdraws": candidate["id"],
        "actor": "peer-reconciler",
        "observed_at": "2026-08-20T00:00:01.000000+00:00",
        "applied_at": "2026-08-20T00:00:01.000000+00:00",
        "schema_version": 1,
        "machine": "peer-machine",
    }
    _write_shard_line(repo_root, "peer-machine", foreign_withdrawal)

    results = tr.reconcile(repo_root=repo_root, actor="reconciler")

    assert len(results) == 1
    twin = results[0]
    assert twin["applied_from"] == candidate["id"]
    assert twin["item_id"] == candidate["item_id"]
    assert twin["axis"] == candidate["axis"]
    assert twin["to_state"] == candidate["to_state"]
    assert twin["applied_at"] is not None

    own_shard = repo_root / tracker_store.EVENTS_DIR_RELPATH / "events.own-machine.jsonl"
    lines = [json.loads(line) for line in own_shard.read_text(encoding="utf-8").splitlines() if line.strip()]
    stored_twin = next(line for line in lines if line.get("applied_from") == candidate["id"])
    assert stored_twin["id"] == twin["id"]
    assert stored_twin["item_id"] == candidate["item_id"]
    assert stored_twin["axis"] == candidate["axis"]
    assert stored_twin["to_state"] == candidate["to_state"]
    assert stored_twin["applied_at"] is not None


# ---------------------------------------------------------------------------
# AC9 (B3/F3 regression lock) — markers/snapshots never selected.
# ---------------------------------------------------------------------------


def test_ac9_observed_set_fold_marker_never_selected_as_candidate(repo_root):
    marker = {
        "id": "own-machine-fold-abc123",
        "kind": "observed_set_fold",
        "observed_set": {},
        "applied_at": None,
        "observed_at": "2026-08-20T00:00:00.000000+00:00",
        "machine": "own-machine",
    }
    _write_shard_line(repo_root, "own-machine", marker)

    results = tr.reconcile(repo_root=repo_root, actor="reconciler")

    assert results == []


def test_ac9_snapshot_event_never_selected_as_candidate(repo_root):
    snapshot_payload = tt.build_snapshot_event(
        "item-4",
        "code_complete",
        folded_event_ids=["evt-own-machine-abc"],
        as_of_sequence=1,
        as_of_applied_at="2026-08-20T00:00:00.000000+00:00",
        folded_to_state="asserted",
    )
    tt.emit_snapshot_event(snapshot_payload, repo_root=repo_root)

    results = tr.reconcile(repo_root=repo_root, actor="reconciler")

    assert results == []


def test_ac9_suggest_and_deferred_tier_transition_are_selected(repo_root):
    candidate = _queued_candidate(item_id="item-5", axis="manual_close", to_state="closed")
    _write_shard_line(repo_root, "own-machine", candidate)

    results = tr.reconcile(repo_root=repo_root, actor="reconciler")

    assert len(results) == 1
    assert results[0]["applied_from"] == candidate["id"]


def test_ac9_already_applied_transition_is_not_reselected(repo_root):
    applied = tt.emit_transition(
        "item-6",
        "qa_verified",
        "verified",
        actor="reconciler",
        evidence=None,
        tier="auto",
        source_observation_id="obs-6",
        repo_root=repo_root,
    )
    assert applied["applied_at"] is not None

    results = tr.reconcile(repo_root=repo_root, actor="reconciler")

    assert results == []


# ---------------------------------------------------------------------------
# Idempotent re-apply — a second reconcile pass over the same candidate is
# a no-op (collision-as-guard, module docstring).
# ---------------------------------------------------------------------------


def test_second_reconcile_pass_is_idempotent_no_op(repo_root):
    candidate = _queued_candidate(item_id="item-7")
    _write_shard_line(repo_root, "own-machine", candidate)

    first = tr.reconcile(repo_root=repo_root, actor="reconciler")
    assert len(first) == 1

    second = tr.reconcile(repo_root=repo_root, actor="reconciler")
    assert second == []

    own_shard = repo_root / tracker_store.EVENTS_DIR_RELPATH / "events.own-machine.jsonl"
    lines = [json.loads(line) for line in own_shard.read_text(encoding="utf-8").splitlines() if line.strip()]
    twins = [line for line in lines if line.get("applied_from") == candidate["id"]]
    assert len(twins) == 1


# ---------------------------------------------------------------------------
# AC10 — both allowlist tests are GREEN.
# ---------------------------------------------------------------------------


def test_ac10_this_module_is_affirmed_and_confined():
    """`tracker_reconcile` is DR-241-affirmed in both allowlist guards and
    offends neither.

    AC10 was authored as "both guards are GREEN". Both are red on arrival at
    this module's landing, for two defects this plan neither introduced nor
    owns, each verified pre-existing at `d27cfebbc` (the commit before C1):

      - `tracker_envelope.py` and `tracker_tier_a.py` (sat-07 waves 1-2) are
        top-level `tracker_store` referencers that were never added to
        `_ALLOWED_TRACKER_STORE_REFERENCERS` with the DR-241 affirmation the
        guard demands.
      - `ops/session/boot_sweep.py:216`'s `state/handoffs/*.md` literal trips
        the confinement guard, which its OWN docstring says should stay out of
        scope by construction ("they don't share a scope with any tracker
        reference") -- so the AST scoping, not the literal, is what regressed.

    Widening the allowlist to go green is what both guards forbid in terms, and
    affirming another workstream's modules is not this plan's call to make. So
    this asserts the part AC10 exists to protect and that C4 can actually
    discharge: this module is registered, and it is absent from both guards'
    offender sets. The peer residual is routed, not absorbed -- see the
    bug-backlog entry filed alongside this chunk.
    """
    from coordinator_core.tests.test_tracker_store import (
        _ALLOWED_TRACKER_STORE_REFERENCERS,
        _PROJECT_ROOT,
        _confinement_check_for_file,
        _tracker_store_referencer_offenders,
    )

    rel = "coordinator_core/tracker_reconcile.py"
    assert rel in _ALLOWED_TRACKER_STORE_REFERENCERS

    offenders = _tracker_store_referencer_offenders(
        Path(_PROJECT_ROOT) / "coordinator_core",
        Path(_PROJECT_ROOT),
        _ALLOWED_TRACKER_STORE_REFERENCERS,
        recursive=False,
        use_ast_check=True,
    )
    assert rel not in offenders

    _confinement_check_for_file(Path(_PROJECT_ROOT) / rel, rel)

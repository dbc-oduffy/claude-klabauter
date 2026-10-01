"""test_close_ships_its_batons — C3 of
docs/plans/2026-08-30-the-close-ships-the-baton-it-closed.md.

Covers the seam C2 built: `directives_commit_tail.resolve_ship_stamp_candidates`/
`apply_ship_stamps`/`revert_ship_stamps`, and `apply.py::_run_close_commit_tail`'s
orchestration of them (ship-stamp BEFORE the commit call, folded into the SAME
`stage_paths` sequence, reverted rather than left standing when the commit that
was meant to carry the stamp fails or never lands).

Negative-spec (mirrors the plan's own Anti-scope): none of these tests drive a
real `git commit` or a real claim ledger — `_held_handoff_basenames` and
`run_close_commit_and_release_claims` are monkeypatched at the seam `apply.py`
itself calls through, so what is under test is the ORCHESTRATION (ordering,
fold, revert-on-failure, the empty-set signal), not `commit_paths`'/`handoff.
stamp`'s own already-tested internals.

Run: python -m pytest coordinator_core/workstream_complete/tests/test_close_ships_its_batons.py -q
"""

from __future__ import annotations

import time

import pytest

from coordinator_core.workstream_complete import apply as _apply
from coordinator_core.workstream_complete import directives_commit_tail


def _commit_tail_outcome(*, committed_sha, commit_failed):
    return directives_commit_tail.CommitTailOutcome(
        committed_sha=committed_sha,
        pushed=None,
        push_status="not-attempted",
        commit_failed=commit_failed,
        integrity_breach=False,
        sha_unverified=False,
        diagnostics=[],
    )


# ---------------------------------------------------------------------------
# resolve_ship_stamp_candidates — the positive-membership rule
# ---------------------------------------------------------------------------


def test_delivered_baton_is_a_candidate(tmp_path, monkeypatch):
    (tmp_path / "state" / "handoffs").mkdir(parents=True)
    (tmp_path / "state" / "handoffs" / "foo.md").write_text("x\n", encoding="utf-8")
    monkeypatch.setattr(
        directives_commit_tail, "_held_handoff_basenames", lambda *_a, **_k: ["foo.md"]
    )
    decisions = {"handoff_dispositions": {"foo.md": {"disposition": "shipped", "shipped_in": "deadbeef"}}}

    candidates = directives_commit_tail.resolve_ship_stamp_candidates(tmp_path, "sid", decisions)

    assert candidates == [("state/handoffs/foo.md", "deadbeef")]


@pytest.mark.parametrize("disposition", ["closed", "abandoned", "continued"])
def test_non_delivered_terminal_baton_is_not_a_candidate(tmp_path, monkeypatch, disposition):
    (tmp_path / "state" / "handoffs").mkdir(parents=True)
    (tmp_path / "state" / "handoffs" / "foo.md").write_text("x\n", encoding="utf-8")
    monkeypatch.setattr(
        directives_commit_tail, "_held_handoff_basenames", lambda *_a, **_k: ["foo.md"]
    )
    decisions = {"handoff_dispositions": {"foo.md": {"disposition": disposition, "shipped_in": "deadbeef"}}}

    candidates = directives_commit_tail.resolve_ship_stamp_candidates(tmp_path, "sid", decisions)

    assert candidates == []


def test_already_archived_baton_is_left_alone(tmp_path, monkeypatch):
    # No file under state/handoffs/ at all -- it is already under archive/handoffs/,
    # i.e. already consumed per the PM's folder-fact ruling. The claim ledger still
    # names the basename (a stale/late-release claim), but there is nothing active
    # here for this close to stamp.
    monkeypatch.setattr(
        directives_commit_tail, "_held_handoff_basenames", lambda *_a, **_k: ["foo.md"]
    )
    decisions = {"handoff_dispositions": {"foo.md": {"disposition": "shipped", "shipped_in": "deadbeef"}}}

    candidates = directives_commit_tail.resolve_ship_stamp_candidates(tmp_path, "sid", decisions)

    assert candidates == []


def test_held_claim_this_close_never_touched_is_not_a_candidate(tmp_path, monkeypatch):
    # The claim ledger says the session holds "bar.md", but this close's own
    # decisions never mention it -- a claim held merely to read (or on a baton
    # this close did not itself close) is excluded by the POSITIVE rule, not
    # because it matches a negative disposition.
    (tmp_path / "state" / "handoffs").mkdir(parents=True)
    (tmp_path / "state" / "handoffs" / "bar.md").write_text("x\n", encoding="utf-8")
    monkeypatch.setattr(
        directives_commit_tail, "_held_handoff_basenames", lambda *_a, **_k: ["bar.md"]
    )
    decisions = {"handoff_dispositions": {}}

    candidates = directives_commit_tail.resolve_ship_stamp_candidates(tmp_path, "sid", decisions)

    assert candidates == []


# ---------------------------------------------------------------------------
# apply.py::_run_close_commit_tail — orchestration: fold-in, no second
# commit object, empty-set signal, and the write-lands-then-commit-fails
# ordering.
# ---------------------------------------------------------------------------


def test_delivered_baton_folds_into_the_close_s_own_commit_with_no_second_commit(monkeypatch, tmp_path):
    commit_calls = []

    def _fake_run_close_commit_and_release_claims(worktree_root, **kwargs):
        commit_calls.append(kwargs)
        return _commit_tail_outcome(committed_sha="abc123", commit_failed=False)

    monkeypatch.setattr(
        directives_commit_tail, "resolve_ship_stamp_candidates",
        lambda *_a, **_k: [("state/handoffs/foo.md", "deadbeef")],
    )
    monkeypatch.setattr(
        directives_commit_tail, "apply_ship_stamps",
        lambda *_a, **_k: (
            directives_commit_tail.ShipStampOutcome(
                stamped_paths=("state/handoffs/foo.md",),
                skipped_paths=(),
                attempted=1,
                diagnostics=(),
            ),
            {"state/handoffs/foo.md": "original text\n"},
        ),
    )
    monkeypatch.setattr(
        directives_commit_tail,
        "run_close_commit_and_release_claims",
        _fake_run_close_commit_and_release_claims,
    )

    decisions = {"subject": "close it", "stage_paths": ["other.txt"]}
    report = _apply._run_close_commit_tail(tmp_path, decisions, "sid-1")

    assert len(commit_calls) == 1, "the stamp must fold into the SAME commit call, never a second one"
    assert sorted(commit_calls[0]["stage_paths"]) == sorted(["other.txt", "state/handoffs/foo.md"])
    assert report["ship_stamp"]["stamped"] == ["state/handoffs/foo.md"]
    assert report["ship_stamp"]["reverted"] == []
    assert report["committed_sha"] == "abc123"


def test_session_holding_no_batons_produces_a_read_signal(monkeypatch, tmp_path):
    monkeypatch.setattr(
        directives_commit_tail, "resolve_ship_stamp_candidates", lambda *_a, **_k: []
    )
    monkeypatch.setattr(
        directives_commit_tail,
        "run_close_commit_and_release_claims",
        lambda worktree_root, **kwargs: _commit_tail_outcome(committed_sha="abc123", commit_failed=False),
    )

    decisions = {"subject": "close it", "stage_paths": ["other.txt"]}
    report = _apply._run_close_commit_tail(tmp_path, decisions, "sid-2")

    # RAN, FOUND NOTHING -- distinguishable from "never ran" (the retired
    # design's empty_consumed_set had no reader; this key does).
    assert report["ship_stamp"]["attempted"] == 0
    assert report["ship_stamp"]["stamped"] == []


def test_write_lands_then_commit_raises_reverts_the_stamp(monkeypatch, tmp_path):
    revert_calls = []
    monkeypatch.setattr(
        directives_commit_tail, "resolve_ship_stamp_candidates",
        lambda *_a, **_k: [("state/handoffs/foo.md", "deadbeef")],
    )
    monkeypatch.setattr(
        directives_commit_tail, "apply_ship_stamps",
        lambda *_a, **_k: (
            directives_commit_tail.ShipStampOutcome(
                stamped_paths=("state/handoffs/foo.md",),
                skipped_paths=(),
                attempted=1,
                diagnostics=(),
            ),
            {"state/handoffs/foo.md": "original text\n"},
        ),
    )

    def _raise(worktree_root, **kwargs):
        raise RuntimeError("simulated commit failure")

    monkeypatch.setattr(directives_commit_tail, "run_close_commit_and_release_claims", _raise)
    monkeypatch.setattr(
        directives_commit_tail, "revert_ship_stamps",
        lambda root, relpaths, backups: revert_calls.append((tuple(relpaths), dict(backups))),
    )

    decisions = {"subject": "close it", "stage_paths": ["other.txt"]}
    report = _apply._run_close_commit_tail(tmp_path, decisions, "sid-3")

    assert revert_calls == [
        (("state/handoffs/foo.md",), {"state/handoffs/foo.md": "original text\n"})
    ], "a raised commit call must revert the already-landed stamp write, per DR-358's finally-release ordering"
    assert report["commit_failed"] is True


@pytest.mark.parametrize(
    "committed_sha,commit_failed",
    [(None, False), ("abc123", True)],
    ids=["no-op-commit-no-sha", "commit-refused"],
)
def test_stamp_reverted_when_commit_did_not_land(monkeypatch, tmp_path, committed_sha, commit_failed):
    revert_calls = []
    monkeypatch.setattr(
        directives_commit_tail, "resolve_ship_stamp_candidates",
        lambda *_a, **_k: [("state/handoffs/foo.md", "deadbeef")],
    )
    monkeypatch.setattr(
        directives_commit_tail, "apply_ship_stamps",
        lambda *_a, **_k: (
            directives_commit_tail.ShipStampOutcome(
                stamped_paths=("state/handoffs/foo.md",),
                skipped_paths=(),
                attempted=1,
                diagnostics=(),
            ),
            {"state/handoffs/foo.md": "original text\n"},
        ),
    )
    monkeypatch.setattr(
        directives_commit_tail, "run_close_commit_and_release_claims",
        lambda worktree_root, **kwargs: _commit_tail_outcome(
            committed_sha=committed_sha, commit_failed=commit_failed
        ),
    )
    monkeypatch.setattr(
        directives_commit_tail, "revert_ship_stamps",
        lambda root, relpaths, backups: revert_calls.append(tuple(relpaths)),
    )

    decisions = {"subject": "close it", "stage_paths": ["other.txt"]}
    report = _apply._run_close_commit_tail(tmp_path, decisions, "sid-4")

    assert revert_calls == [("state/handoffs/foo.md",)]
    assert report["ship_stamp"]["stamped"] == []
    assert report["ship_stamp"]["reverted"] == ["state/handoffs/foo.md"]


# ---------------------------------------------------------------------------
# Process-time budget for the candidate-resolution leg itself -- process
# time, never wall clock (a wall-clock read is dominated by peer load on a
# shared box, per this repo's own load-norm doctrine). Aggregated over
# N>=200 iterations since a single `time.process_time()` read sits below
# Windows' ~15.6ms clock granularity and cannot show a regression on its own.
# ---------------------------------------------------------------------------

_SHIP_CANDIDATE_RESOLUTION_CEILING_MS = 50.0
_ITERATIONS = 200


def test_resolve_ship_stamp_candidates_process_time_budget(tmp_path, monkeypatch):
    (tmp_path / "state" / "handoffs").mkdir(parents=True)
    (tmp_path / "state" / "handoffs" / "foo.md").write_text("x\n", encoding="utf-8")
    monkeypatch.setattr(
        directives_commit_tail, "_held_handoff_basenames", lambda *_a, **_k: ["foo.md"]
    )
    decisions = {"handoff_dispositions": {"foo.md": {"disposition": "shipped", "shipped_in": "deadbeef"}}}

    start = time.process_time()
    for _ in range(_ITERATIONS):
        directives_commit_tail.resolve_ship_stamp_candidates(tmp_path, "sid", decisions)
    elapsed_ms = (time.process_time() - start) * 1000.0

    assert elapsed_ms <= _SHIP_CANDIDATE_RESOLUTION_CEILING_MS, (
        f"resolve_ship_stamp_candidates: {elapsed_ms}ms process time over {_ITERATIONS} "
        f"iterations exceeds the {_SHIP_CANDIDATE_RESOLUTION_CEILING_MS}ms ceiling -- "
        "candidate resolution must stay bounded by claims held, never corpus size"
    )

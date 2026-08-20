"""
coordinator_core.ops.ceremony.tests.test_tail_ops

Tests for ceremony.tail_ops -- the C6 chunk of the `wsc_tail` rebuild
(docs/plans/2026-07-16-wsc-pure-python-tail-rebuild.md).

Coverage:
  cs_archive:
    (a) archive_idempotency        -- second call on an already-archived (or never-existed)
                                       session dir is a clean skip, never a failure
    (b) archive_moves_session_dir  -- happy path: session dir lands under .archive/<sid>-<date>

  cs_release_artifact:
    (c) release_non_holder_is_noop           -- claim held by a DIFFERENT session_id is
                                                 never deleted (non-deletion-of-live-peer)
    (d) release_self_holder_removes_claim    -- claim held by the resolving session IS removed
    (e) release_absent_claim_is_clean_skip   -- missing claim dir is idempotent no-op
    (f) release_legacy_pid_only_never_held   -- a claim dir with no session_id file is NEVER
                                                 treated as held-by-me (pid is not the key)
    (g) release_toctou_takeover_is_noop      -- a takeover between the two _claim_held_by_me
                                                 reads flips the second read; no deletion

  fleet two-phase wiring (generic two-phase helper -- (l)/(m)'s direct-wrapper tests were
  removed with archive_completed_plans/archive_completed_handoffs/sweep_actioned_memos
  themselves, C2 2026-07-23 -- see fire_archive_sweeps_detached below):
    (h) two_phase_no_candidates_short_circuits -- empty T1 preview -> no T3 call, empty result
    (i) two_phase_acts_on_preview_candidates   -- T1 candidates feed T3 candidate_ids verbatim
    (j) two_phase_preview_failure_short_circuits -- non-zero preview exit_code -> failed[], no T3
    (k) two_phase_handler_exception_is_caught  -- handler raising never propagates

  fire_archive_sweeps_detached (C2, 2026-07-23 wsc-tail-slim-down -- replaces the retired
  archive_completed_plans/archive_completed_handoffs/sweep_actioned_memos blocking wrappers):
    (l) fire_archive_sweeps_detached_spawns_four_expected_clis -- exactly the four per-class
        CLIs are spawned, each with worktree_root as its repo_root arg; sweep-boot.py is
        never among them
    (m) fire_archive_sweeps_detached_records_spawn_failure -- a spawn_detached() False return
        for one script lands in failed[], the rest still fire

  coverage.gate / review_trail.write wrappers:
    (n) coverage_gate_covered_verdict_is_acted
    (o) coverage_gate_indeterminate_is_failed
    (p) review_trail_incomplete_metadata_skips_cleanly
    (q) review_trail_b_adjudication_incomplete_is_critical
    (r) review_trail_complete_metadata_forwards_to_handler

  refresh_roadmap_callout (STEP_2_75, 2026-07-22 C9 wiring-gap fix -- docs/plans/
  2026-07-16-wsc-pure-python-tail-rebuild.md § C9; its former render_handoff_tracker
  sibling was retired 2026-08-14, see docs/plans/2026-08-14-retire-the-handoff-
  tracker-and-project-tracker-renders.md § C2):
    (u) refresh_roadmap_callout_no_consumed_handoffs_is_clean_skip -- [] input -> skip,
                                                                       no subprocess call
    (v) refresh_roadmap_callout_missing_roadmap_id_skips_per_handoff -- a consumed handoff
                                                                         with no roadmap_id
                                                                         frontmatter is skipped
    (w) refresh_roadmap_callout_acts_when_id_present -- happy path: rc=0 -> acted[]
    (x) refresh_roadmap_callout_handler_exception_is_failed -- refresh main() raising
                                                                 degrades to failed[]

  fire_tracker_and_roadmap_detached (C5, 2026-07-23 wsc-tail-slim-down -- detached
  replacement for the BLOCKING refresh_roadmap_callout call; mirrors
  fire_archive_sweeps_detached's C2 shape; its tracker-CLI leg was retired
  2026-08-14 along with `render-handoff-tracker.py`):
    (aa) fire_tracker_and_roadmap_detached_spawns_per_roadmap_callout -- the
         roadmap-callout CLI fires once per distinct allowlist-valid roadmap_id
         found in consumed_handoff_paths
    (bb) fire_tracker_and_roadmap_detached_no_roadmap_ids_is_clean_skip -- no
         consumed handoff carries a roadmap_id -> skipped[], zero callout spawns
    (cc) fire_tracker_and_roadmap_detached_dedupes_roadmap_ids -- two consumed
         handoffs sharing one roadmap_id fire the callout CLI only once
    (dd) fire_tracker_and_roadmap_detached_records_spawn_failure -- a spawn_detached()
         False return for the callout CLI lands in failed[]

  housekeeping-liveness wiring (C17b follow-up -- the missing `stamp_liveness`
  call sites; see housekeeping_liveness.py's own module docstring for the
  three-state contract these calls feed):
    (gg) refresh_roadmap_callout_success_stamps_roadmap_callout -- at least one
         acted[] entry stamps ROADMAP_CALLOUT
    (hh) refresh_roadmap_callout_all_skipped_does_not_stamp -- an all-skipped pass
         (no roadmap_id anywhere) leaves ROADMAP_CALLOUT unstamped

  (ii)/(jj) coverage_gate liveness tests, and the coverage.gate wrapper tests
  above them, were removed 2026-08-16 -- `tail_ops.run_coverage_gate`/
  `OP_COVERAGE_GATE` and `housekeeping_liveness.COVERAGE_GATE` no longer
  exist (state/kill-ledger.md K-001).

Spec backlink: pln-rebuild-the-wsc-commit-ceremon-f7c2a0 § C6, § C9.
Spec backlink: pln-wsc-tail-slim-down-op-scoped-c-e9a265 § C5.
Spec backlink: pln-wsc-tail-slim-down-op-scoped-c-e9a265 § C17b (liveness call sites).
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from coordinator_core.ops.ceremony import housekeeping_liveness as hl
from coordinator_core.ops.ceremony import tail_ops


def _run(coro) -> Any:
    """Run an async coroutine synchronously."""
    return asyncio.run(coro)


def _make_common_dir(tmp_path: Path) -> Path:
    """<tmp_path>/repo/.git -- mirrors the standard-layout common_dir convention."""
    common_dir = tmp_path / "repo" / ".git"
    common_dir.mkdir(parents=True)
    return common_dir


# ---------------------------------------------------------------------------
# cs_archive
# ---------------------------------------------------------------------------


def test_archive_idempotency(tmp_path):
    common_dir = _make_common_dir(tmp_path)

    # Never existed -> clean skip.
    result = tail_ops.cs_archive(common_dir, "sid-never-existed")
    assert result["acted"] == []
    assert result["failed"] == []
    assert result["skipped"] == [f"{tail_ops.OP_CS_ARCHIVE}:already-archived-or-absent"]

    # Archive once for real, then call again -- second call must also be a clean skip.
    sdir = common_dir / "coordinator-sessions" / "sid-1"
    sdir.mkdir(parents=True)
    (sdir / "meta.json").write_text("{}", encoding="utf-8")

    first = tail_ops.cs_archive(common_dir, "sid-1")
    assert first["failed"] == []
    assert first["acted"] == [f"{tail_ops.OP_CS_ARCHIVE}:sid-1"]
    assert not sdir.is_dir()

    second = tail_ops.cs_archive(common_dir, "sid-1")
    assert second["failed"] == []
    assert second["acted"] == []
    assert second["skipped"] == [f"{tail_ops.OP_CS_ARCHIVE}:already-archived-or-absent"]


def test_archive_moves_session_dir(tmp_path):
    common_dir = _make_common_dir(tmp_path)
    sdir = common_dir / "coordinator-sessions" / "sid-2"
    sdir.mkdir(parents=True)
    (sdir / "meta.json").write_text('{"session_id": "sid-2"}', encoding="utf-8")

    result = tail_ops.cs_archive(common_dir, "sid-2")

    assert result["failed"] == []
    assert result["acted"] == [f"{tail_ops.OP_CS_ARCHIVE}:sid-2"]
    assert not sdir.is_dir()

    archive_dirs = list((common_dir / "coordinator-sessions" / ".archive").glob("sid-2-*"))
    assert len(archive_dirs) == 1, f"expected exactly one archived dir; got {archive_dirs}"
    assert (archive_dirs[0] / "meta.json").is_file()


# ---------------------------------------------------------------------------
# cs_release_artifact
# ---------------------------------------------------------------------------


def _make_claim(common_dir: Path, artifact_class: str, basename: str, *, held_by: str | None) -> Path:
    claim_dir = common_dir / "coordinator-sessions" / f"{artifact_class}-claims" / basename
    claim_dir.mkdir(parents=True)
    if held_by is not None:
        (claim_dir / "session_id").write_text(held_by, encoding="utf-8")
    return claim_dir


def test_release_non_holder_is_noop(tmp_path, monkeypatch):
    """A claim held by a DIFFERENT session is never deleted (non-deletion-of-live-peer)."""
    common_dir = _make_common_dir(tmp_path)
    claim_dir = _make_claim(common_dir, "plan", "my-plan", held_by="OTHER-SESSION")

    monkeypatch.setattr(tail_ops, "resolve_current_session_id", lambda worktree_root: "MY-SESSION")

    result = tail_ops.cs_release_artifact(common_dir, "plan", "my-plan")

    assert claim_dir.is_dir(), "a peer's live claim must NOT be deleted"
    assert result["acted"] == []
    assert result["failed"] == []
    assert result["skipped"] == [f"{tail_ops.OP_CS_RELEASE_ARTIFACT}:not-holder"]


def test_release_self_holder_removes_claim(tmp_path, monkeypatch):
    common_dir = _make_common_dir(tmp_path)
    claim_dir = _make_claim(common_dir, "plan", "my-plan", held_by="MY-SESSION")

    monkeypatch.setattr(tail_ops, "resolve_current_session_id", lambda worktree_root: "MY-SESSION")

    result = tail_ops.cs_release_artifact(common_dir, "plan", "my-plan")

    assert not claim_dir.exists(), "the holder's own claim must be released"
    assert result["failed"] == []
    assert result["acted"] == [f"{tail_ops.OP_CS_RELEASE_ARTIFACT}:plan/my-plan"]


def test_release_absent_claim_is_clean_skip(tmp_path, monkeypatch):
    common_dir = _make_common_dir(tmp_path)
    monkeypatch.setattr(tail_ops, "resolve_current_session_id", lambda worktree_root: "MY-SESSION")

    result = tail_ops.cs_release_artifact(common_dir, "plan", "never-claimed")

    assert result["failed"] == []
    assert result["acted"] == []
    assert result["skipped"] == [f"{tail_ops.OP_CS_RELEASE_ARTIFACT}:already-absent"]


def test_release_legacy_pid_only_never_held(tmp_path, monkeypatch):
    """A claim dir with no session_id file (legacy pid-only claim) is NEVER released via
    cs_release_artifact -- the pid fallback the bash original carried is a permanent
    in-harness no-op and this native port keys exclusively on session_id (negative-spec)."""
    common_dir = _make_common_dir(tmp_path)
    claim_dir = _make_claim(common_dir, "plan", "legacy-plan", held_by=None)
    (claim_dir / "pid").write_text("12345", encoding="utf-8")

    monkeypatch.setattr(tail_ops, "resolve_current_session_id", lambda worktree_root: "MY-SESSION")

    result = tail_ops.cs_release_artifact(common_dir, "plan", "legacy-plan")

    assert claim_dir.is_dir(), "legacy pid-only claim must never be released by this port"
    assert result["skipped"] == [f"{tail_ops.OP_CS_RELEASE_ARTIFACT}:not-holder"]


def test_release_toctou_takeover_is_noop(tmp_path, monkeypatch):
    """A takeover between the two _claim_held_by_me reads flips the second read to False;
    the destructive rm must NOT run."""
    common_dir = _make_common_dir(tmp_path)
    claim_dir = _make_claim(common_dir, "plan", "raced-plan", held_by="MY-SESSION")

    monkeypatch.setattr(tail_ops, "resolve_current_session_id", lambda worktree_root: "MY-SESSION")

    calls = {"n": 0}
    real_check = tail_ops._claim_held_by_me

    def _flaky_check(cdir: Path, my_sid: str) -> bool:
        calls["n"] += 1
        if calls["n"] == 1:
            return real_check(cdir, my_sid)
        # Simulate a concurrent takeover landing between the two reads.
        (cdir / "session_id").write_text("NEW-HOLDER", encoding="utf-8")
        return real_check(cdir, my_sid)

    monkeypatch.setattr(tail_ops, "_claim_held_by_me", _flaky_check)

    result = tail_ops.cs_release_artifact(common_dir, "plan", "raced-plan")

    assert claim_dir.is_dir(), "a mid-flight takeover must abort the release, not delete it"
    assert result["skipped"] == [f"{tail_ops.OP_CS_RELEASE_ARTIFACT}:holder-changed-toctou"]
    assert calls["n"] == 2


# ---------------------------------------------------------------------------
# Fleet two-phase wiring (sweep parity + archive_plans/handoffs wiring)
# ---------------------------------------------------------------------------


def test_two_phase_no_candidates_short_circuits(tmp_path):
    common_dir = _make_common_dir(tmp_path)
    preview_calls = []
    act_calls = []

    async def _handler(params: dict, repo_root=None) -> dict:
        if params["dry_run"]:
            preview_calls.append(params)
            return {"exit_code": 0, "candidates": []}
        act_calls.append(params)
        return {"exit_code": 0, "acted": [], "skipped": [], "failed": []}

    result = _run(tail_ops.run_fleet_op_two_phase(_handler, "fleet.fake_op", common_dir))

    assert result == {"acted": [], "skipped": [], "failed": []}
    assert len(preview_calls) == 1
    assert not act_calls, "T3 act must NOT be called when T1 preview has no candidates"


def test_two_phase_acts_on_preview_candidates(tmp_path):
    common_dir = _make_common_dir(tmp_path)
    act_calls = []

    async def _handler(params: dict, repo_root=None) -> dict:
        if params["dry_run"]:
            return {"exit_code": 0, "candidates": [{"id": "c1"}, {"id": "c2"}]}
        act_calls.append(params)
        return {
            "exit_code": 0,
            "acted": [{"id": "c1"}],
            "skipped": [{"id": "c2"}],
            "failed": [],
        }

    result = _run(tail_ops.run_fleet_op_two_phase(_handler, "fleet.fake_op", common_dir))

    assert len(act_calls) == 1
    assert act_calls[0]["candidate_ids"] == ["c1", "c2"]
    assert act_calls[0]["mode"] == "already-terminal"
    assert result == {"acted": ["c1"], "skipped": ["c2"], "failed": []}


def test_two_phase_preview_failure_short_circuits(tmp_path):
    common_dir = _make_common_dir(tmp_path)
    act_calls = []

    async def _handler(params: dict, repo_root=None) -> dict:
        if params["dry_run"]:
            return {"exit_code": 1, "candidates": []}
        act_calls.append(params)
        return {"exit_code": 0}

    result = _run(tail_ops.run_fleet_op_two_phase(_handler, "fleet.fake_op", common_dir))

    assert not act_calls
    assert result["acted"] == []
    assert result["skipped"] == []
    assert result["failed"] == ["fleet.fake_op: preview exit_code=1"]


def test_two_phase_handler_exception_is_caught(tmp_path):
    common_dir = _make_common_dir(tmp_path)

    async def _handler(params: dict, repo_root=None) -> dict:
        raise RuntimeError("boom")

    result = _run(tail_ops.run_fleet_op_two_phase(_handler, "fleet.fake_op", common_dir))

    assert result["acted"] == []
    assert result["skipped"] == []
    assert len(result["failed"]) == 1
    assert "fleet.fake_op: RuntimeError" in result["failed"][0]


def test_fire_archive_sweeps_detached_spawns_four_expected_clis(tmp_path):
    """C2: the retired blocking archive_completed_plans/archive_completed_handoffs/
    sweep_actioned_memos calls are replaced by exactly four detached CLI fires -- never
    the composite sweep-boot.py (plan § C2 anti-scope: it also runs the unintegrated-
    findings reap, a tracked git rm, out of scope for a call fired on every WSC pass)."""
    worktree_root = tmp_path
    spawned: list[tuple] = []

    def _fake_spawn(repo_root, script_path, args):
        spawned.append((repo_root, script_path, tuple(args)))
        return True

    with patch.object(tail_ops, "spawn_detached", side_effect=_fake_spawn) as mock_spawn:
        result = tail_ops.fire_archive_sweeps_detached(worktree_root)

    assert mock_spawn.call_count == 4
    fired_scripts = {Path(script_path).name for _repo_root, script_path, _args in spawned}
    assert fired_scripts == {
        "sweep-terminal-plans.py",
        "sweep-shipped-handoffs.py",
        "sweep-consumed-handoffs.py",
        "sweep-actioned-memos.py",
    }
    assert "sweep-boot.py" not in fired_scripts

    for repo_root, script_path, args in spawned:
        assert repo_root == str(worktree_root)
        assert args == (str(worktree_root),)
        assert Path(script_path).parent == Path(worktree_root, "coordinator", "bin")

    assert result["failed"] == []
    assert len(result["acted"]) == 4


def test_fire_archive_sweeps_detached_records_spawn_failure(tmp_path):
    """A spawn_detached() False return for one script lands in failed[]; the other
    three scripts still fire (a single bad spawn must not short-circuit the rest)."""
    worktree_root = tmp_path

    def _fake_spawn(repo_root, script_path, args):
        return "sweep-actioned-memos.py" not in script_path

    with patch.object(tail_ops, "spawn_detached", side_effect=_fake_spawn) as mock_spawn:
        result = tail_ops.fire_archive_sweeps_detached(worktree_root)

    assert mock_spawn.call_count == 4
    assert len(result["acted"]) == 3
    assert len(result["failed"]) == 1
    assert "sweep-actioned-memos.py" in result["failed"][0]


def test_unregistered_op_key_is_clean_failure(tmp_path):
    common_dir = _make_common_dir(tmp_path)
    result = _run(tail_ops._run_fleet_op_by_key("fleet.does_not_exist", "fleet.does_not_exist", common_dir))
    assert result["acted"] == []
    assert result["skipped"] == []
    assert result["failed"] == ["fleet.does_not_exist: fleet.does_not_exist not registered"]


# ---------------------------------------------------------------------------
# review_trail.write wrapper
# ---------------------------------------------------------------------------


def test_review_trail_incomplete_metadata_skips_cleanly(tmp_path):
    common_dir = _make_common_dir(tmp_path)
    result = _run(tail_ops.write_review_trail(common_dir, review_trail=None))
    assert result["failed"] == []
    assert "failed_critical" not in result
    assert result["skipped"] == [f"{tail_ops.OP_REVIEW_TRAIL}:no-review-metadata"]


def test_review_trail_b_adjudication_incomplete_is_critical(tmp_path):
    common_dir = _make_common_dir(tmp_path)
    result = _run(
        tail_ops.write_review_trail(
            common_dir,
            review_trail={"sha_range": "abc..def"},  # missing 4 required fields
            b_adjudication_present=True,
        )
    )
    assert result["acted"] == []
    assert result["skipped"] == []
    assert result["failed"] == []
    assert len(result["failed_critical"]) == 1
    assert "b_adjudication present but review_trail missing" in result["failed_critical"][0]


def test_review_trail_partition_mandatory_incomplete_is_critical(tmp_path):
    """D, cross-repo/inbox/2026-08-15-example-retrieval-repo-em-wsc-review-trail-skips-silently.md:
    `partition_mandatory` is `b_adjudication_present`'s sibling gate — a resolved
    `decide_review_scale` row 4/6 with no complete review_trail metadata must also be
    `failed_critical`, not the ordinary `no-review-metadata` skip."""
    common_dir = _make_common_dir(tmp_path)
    result = _run(
        tail_ops.write_review_trail(
            common_dir,
            review_trail=None,
            partition_mandatory=True,
        )
    )
    assert result["acted"] == []
    assert result["skipped"] == []
    assert result["failed"] == []
    assert len(result["failed_critical"]) == 1
    assert "partition_mandatory resolved but review_trail missing" in result["failed_critical"][0]


def test_review_trail_partition_mandatory_false_and_no_adjudication_is_ordinary_skip(tmp_path):
    """Neither gate set → the pre-existing ordinary no-review-this-session skip,
    unchanged — D is additive, never a stricter default for the common close."""
    common_dir = _make_common_dir(tmp_path)
    result = _run(
        tail_ops.write_review_trail(common_dir, review_trail=None, partition_mandatory=False)
    )
    assert result["failed"] == []
    assert "failed_critical" not in result
    assert result["skipped"] == [f"{tail_ops.OP_REVIEW_TRAIL}:no-review-metadata"]


def test_review_trail_many_partition_mandatory_no_qualifying_entries_is_critical(tmp_path):
    """`write_review_trail_many`'s sibling of the single-record test above — an
    empty/all-incomplete list with `partition_mandatory=True` must also be critical."""
    common_dir = _make_common_dir(tmp_path)
    result = _run(
        tail_ops.write_review_trail_many(
            common_dir,
            review_trail_list=[{"sha_range": "abc..def"}],  # missing required fields
            partition_mandatory=True,
        )
    )
    assert result["acted"] == []
    assert result["skipped"] == []
    assert result["failed"] == []
    assert len(result["failed_critical"]) == 1
    assert "partition_mandatory resolved but review_trail (list) had no" in result["failed_critical"][0]


def test_review_trail_complete_metadata_forwards_to_handler(tmp_path):
    common_dir = _make_common_dir(tmp_path)
    forwarded = {}

    async def _handler(params: dict, repo_root=None) -> dict:
        forwarded.update(params)
        return {"out_path": "state/review-trail/2026-07-16-abc.json"}

    review_trail = {
        "sha_range": "abc..def",
        "reviewer": "code-reviewer",
        "scope": "chain",
        "verdict": "ok",
        "diff_loc": 42,
    }

    with patch.object(tail_ops, "get_op_handler", return_value=_handler):
        result = _run(tail_ops.write_review_trail(common_dir, review_trail=review_trail))

    assert result["failed"] == []
    assert "failed_critical" not in result
    assert result["acted"] == [
        f"{tail_ops.OP_REVIEW_TRAIL}:state/review-trail/2026-07-16-abc.json"
    ]
    assert forwarded == review_trail


# ---------------------------------------------------------------------------
# write_review_trail_many -- N-slice sibling for the commit-tail path
# (finishes the partitioned-review fix's second, previously-stopgapped half;
# see directives_commit_tail.py's _review_fields_present docstring)
# ---------------------------------------------------------------------------


def test_write_review_trail_many_empty_list_skips_cleanly(tmp_path):
    common_dir = _make_common_dir(tmp_path)
    result = _run(tail_ops.write_review_trail_many(common_dir, []))
    assert result["failed"] == []
    assert "failed_critical" not in result
    assert result["skipped"] == [f"{tail_ops.OP_REVIEW_TRAIL}:no-review-metadata"]


def test_write_review_trail_many_writes_one_record_per_qualifying_slice(tmp_path):
    common_dir = _make_common_dir(tmp_path)
    calls = []

    async def _handler(params: dict, repo_root=None) -> dict:
        calls.append(dict(params))
        return {"out_path": f"state/review-trail/{params['sha_range']}.json"}

    slices = [
        {
            "sha_range": "a1..a2",
            "reviewer": "code-reviewer",
            "scope": "chain",
            "verdict": "ok",
            "diff_loc": 10,
        },
        {"sha_range": "incomplete-slice-missing-fields"},  # dropped, no write attempted
        {
            "sha_range": "b1..b2",
            "reviewer": "staff-eng",
            "scope": "session",
            "verdict": "warn",
            "diff_loc": 20,
        },
    ]

    with patch.object(tail_ops, "get_op_handler", return_value=_handler):
        result = _run(tail_ops.write_review_trail_many(common_dir, slices))

    assert result["failed"] == []
    assert "failed_critical" not in result
    assert result["acted"] == [
        f"{tail_ops.OP_REVIEW_TRAIL}:state/review-trail/a1..a2.json",
        f"{tail_ops.OP_REVIEW_TRAIL}:state/review-trail/b1..b2.json",
    ]
    assert len(calls) == 2
    assert calls[0]["sha_range"] == "a1..a2"
    assert calls[1]["sha_range"] == "b1..b2"


def test_write_review_trail_many_17_slices_run_concurrently_not_n_times_serial(tmp_path):
    """KPI regression (2026-08-15, docs/plans/2026-07-22-wsc-tail-sub-2s-invoke-
    budget.md): measured on a real `/workstream-complete` pass, 17
    `--review-slice` records drove `write_review_trail_many`'s BLOCKING path
    to >30s (the global dispatch guard tripped twice) -- 17.8s of that was
    reproduced here in isolation, sequentially, on a tiny synthetic repo
    with no real contention, confirming the dominant cost was per-slice
    SUBPROCESS-SPAWN latency (measured against the then-live
    `review_trail_write._guard_foreign_session_range`, since removed by
    K-010, plus `_own_session_touched_paths_and_untrailered_flag`, each
    genuinely re-derived per slice since every slice names a DIFFERENT
    sha_range), not git's own walk cost over the tiny fixture data.

    Fix: `write_review_trail_many` now fires all qualifying slices'
    `write_review_trail` calls CONCURRENTLY (`asyncio.gather`) rather than
    sequentially -- each slice's own op call never raises (see that
    function's own docstring), so `asyncio.gather`'s default
    `return_exceptions=False` cannot let one slice suppress a sibling; the
    per-slice failure-isolation property `write_review_trail_many`'s own
    docstring requires is unaffected, only wall clock changes.

    This test pins the CONCURRENCY property STRUCTURALLY, by observing peak
    in-flight overlap rather than wall clock. An earlier version asserted a
    timing budget (``elapsed < 5 * _PER_SLICE_DELAY``) and was flaky exactly
    as predicted: it passed in isolation and failed in the full-file run on
    this box's stated load norm of 50-70 concurrent LLMs, because a wall-clock
    bound measures the scheduler as much as the code. A pin that fails under
    load teaches the next person to re-run until green, which is worse than no
    pin at all -- so the assertion is now on max concurrent in-flight calls,
    which is exactly the property under test and is unaffected by how slowly
    the box happens to be running.

    Sequential awaiting yields max_in_flight == 1; `asyncio.gather` yields
    max_in_flight == n_slices. There is no load condition under which those
    two are confusable.
    """
    common_dir = _make_common_dir(tmp_path)
    _PER_SLICE_DELAY = 0.05
    n_slices = 17

    slices = [
        {
            "sha_range": f"sha{i}~1..sha{i}",
            "reviewer": "staff-eng",
            "scope": "chain",
            "verdict": "ok",
            "diff_loc": 10,
        }
        for i in range(n_slices)
    ]

    in_flight = 0
    max_in_flight = 0

    async def _handler(params: dict, repo_root=None) -> dict:
        nonlocal in_flight, max_in_flight
        in_flight += 1
        max_in_flight = max(max_in_flight, in_flight)
        try:
            await asyncio.sleep(_PER_SLICE_DELAY)
        finally:
            in_flight -= 1
        return {"out_path": f"state/review-trail/{params['sha_range']}.json"}

    with patch.object(tail_ops, "get_op_handler", return_value=_handler):
        result = _run(tail_ops.write_review_trail_many(common_dir, slices))

    assert result["failed"] == []
    assert "failed_critical" not in result
    assert len(result["acted"]) == n_slices

    assert max_in_flight == n_slices, (
        f"peak in-flight was {max_in_flight}, expected {n_slices} -- "
        "the slices are not overlapping, which is a regression back to "
        "sequential per-slice awaiting"
    )


def test_write_review_trail_many_batches_attribution_context_once_not_n_times(tmp_path):
    """C1/C2 pin (docs/plans/2026-08-15-the-review-trail-write-stops-paying-
    n-wa.md): `write_review_trail_many` must call `review_trail_write.
    build_batch_attribution_context` exactly ONCE per batch, not once per
    slice -- the whole point of C1 is that the identical-or-batchable
    per-slice lookups (is-inside-work-tree, deliverable-id, P2 attribution
    window/grep) are computed once and reused, not re-derived per slice. A
    future edit that reintroduces per-slice re-derivation (e.g. moving the
    call inside the loop/gather) fails THIS test deterministically, rather
    than only showing up as a slow stopwatch under machine load.

    Also pins that every slice's own `write_review_trail` call receives the
    SAME batch_context object -- the whole point is reuse, not a
    per-slice-fresh-but-still-single-call context.
    """
    common_dir = _make_common_dir(tmp_path)
    n_slices = 17
    slices = [
        {
            "sha_range": f"sha{i}~1..sha{i}",
            "reviewer": "staff-eng",
            "scope": "chain",
            "verdict": "ok",
            "diff_loc": 10,
        }
        for i in range(n_slices)
    ]

    sentinel_context = {"is_work_tree_rc": 0, "marker": "batch-context-sentinel"}
    build_calls: list = []
    received_contexts: list = []

    def fake_build(caller_worktree, sha_ranges):
        build_calls.append((caller_worktree, tuple(sha_ranges)))
        return sentinel_context

    async def fake_write(common_dir, entry, *, b_adjudication_present=False, _batch_context=None):
        received_contexts.append(_batch_context)
        return {"acted": [entry["sha_range"]], "skipped": [], "failed": [], "failed_critical": []}

    with patch.object(
        tail_ops._review_trail_write_mod, "build_batch_attribution_context", fake_build,
    ), patch.object(tail_ops, "write_review_trail", fake_write):
        result = _run(tail_ops.write_review_trail_many(common_dir, slices))

    assert len(build_calls) == 1, (
        f"build_batch_attribution_context called {len(build_calls)} times for "
        f"{n_slices} slices -- expected exactly 1 (batched once), not once per slice"
    )
    assert build_calls[0][1] == tuple(entry["sha_range"] for entry in slices)
    assert len(received_contexts) == n_slices
    assert all(ctx is sentinel_context for ctx in received_contexts)
    assert result["failed"] == []
    assert "failed_critical" not in result


# ---------------------------------------------------------------------------
# refresh_roadmap_callout (STEP_2_75, C9 wiring-gap fix; its former
# render_handoff_tracker sibling was retired 2026-08-14, see
# docs/plans/2026-08-14-retire-the-handoff-tracker-and-project-tracker-renders.md C2)
# ---------------------------------------------------------------------------


def test_refresh_roadmap_callout_no_consumed_handoffs_is_clean_skip(tmp_path):
    worktree_root = tmp_path / "repo"
    worktree_root.mkdir()

    with patch(
        "coordinator_core.ops.refresh_roadmap_callout.main",
    ) as mock_main:
        result = tail_ops.refresh_roadmap_callout(worktree_root, [])

    mock_main.assert_not_called()
    assert result == {
        "acted": [], "skipped": [f"{tail_ops.OP_ROADMAP_CALLOUT}:no-consumed-handoff"], "failed": [],
    }


def test_refresh_roadmap_callout_missing_roadmap_id_skips_per_handoff(tmp_path):
    worktree_root = tmp_path / "repo"
    handoff_dir = worktree_root / "state" / "handoffs"
    handoff_dir.mkdir(parents=True)
    handoff_path = handoff_dir / "2026-07-22-example.md"
    handoff_path.write_text(
        "---\nclaimed_by: sid-1\n---\n\nbody, no roadmap_id here\n", encoding="utf-8",
    )

    with patch("coordinator_core.ops.refresh_roadmap_callout.main") as mock_main:
        result = tail_ops.refresh_roadmap_callout(
            worktree_root, ["state/handoffs/2026-07-22-example.md"]
        )

    mock_main.assert_not_called()
    assert result["acted"] == []
    assert result["failed"] == []
    assert result["skipped"] == [
        f"{tail_ops.OP_ROADMAP_CALLOUT}:no-roadmap-id:state/handoffs/2026-07-22-example.md"
    ]


def test_refresh_roadmap_callout_acts_when_id_present(tmp_path):
    worktree_root = tmp_path / "repo"
    handoff_dir = worktree_root / "state" / "handoffs"
    handoff_dir.mkdir(parents=True)
    handoff_path = handoff_dir / "2026-07-22-example.md"
    handoff_path.write_text(
        "---\nclaimed_by: sid-1\nroadmap_id: goal-example\n---\n\nbody\n", encoding="utf-8",
    )

    with patch(
        "coordinator_core.ops.refresh_roadmap_callout.main", return_value=0,
    ) as mock_main:
        result = tail_ops.refresh_roadmap_callout(
            worktree_root, ["state/handoffs/2026-07-22-example.md"]
        )

    mock_main.assert_called_once_with(["goal-example", "--root", str(worktree_root)])
    assert result == {
        "acted": [f"{tail_ops.OP_ROADMAP_CALLOUT}:goal-example"], "skipped": [], "failed": [],
        "roadmap_stub_index_paths": [],
    }


def test_refresh_roadmap_callout_stages_updated_stub_index(tmp_path):
    """When the roadmap's STUB-INDEX.md actually exists on disk post-refresh, its
    repo-relative path is surfaced under `roadmap_stub_index_paths` so the caller
    can stage it -- `refresh_roadmap_callout.main` rewrites a TRACKED file in-place,
    which would otherwise trip the dirty-tree gate's unattributable-path check."""
    worktree_root = tmp_path / "repo"
    handoff_dir = worktree_root / "state" / "handoffs"
    handoff_dir.mkdir(parents=True)
    handoff_path = handoff_dir / "2026-07-22-example.md"
    handoff_path.write_text(
        "---\nclaimed_by: sid-1\nroadmap_id: goal-example\n---\n\nbody\n", encoding="utf-8",
    )
    stub_index_dir = worktree_root / "state" / "roadmap" / "goal-example"
    stub_index_dir.mkdir(parents=True)
    (stub_index_dir / "STUB-INDEX.md").write_text("# Roadmap\n", encoding="utf-8")

    with patch("coordinator_core.ops.refresh_roadmap_callout.main", return_value=0):
        result = tail_ops.refresh_roadmap_callout(
            worktree_root, ["state/handoffs/2026-07-22-example.md"]
        )

    assert result["failed"] == []
    assert result["roadmap_stub_index_paths"] == ["state/roadmap/goal-example/STUB-INDEX.md"]


def test_refresh_roadmap_callout_handler_exception_is_failed(tmp_path):
    worktree_root = tmp_path / "repo"
    handoff_dir = worktree_root / "state" / "handoffs"
    handoff_dir.mkdir(parents=True)
    handoff_path = handoff_dir / "2026-07-22-example.md"
    handoff_path.write_text(
        "---\nclaimed_by: sid-1\nroadmap_id: goal-example\n---\n\nbody\n", encoding="utf-8",
    )

    with patch(
        "coordinator_core.ops.refresh_roadmap_callout.main",
        side_effect=RuntimeError("boom"),
    ):
        result = tail_ops.refresh_roadmap_callout(
            worktree_root, ["state/handoffs/2026-07-22-example.md"]
        )

    assert result["acted"] == []
    assert result["skipped"] == []
    assert len(result["failed"]) == 1
    assert tail_ops.OP_ROADMAP_CALLOUT in result["failed"][0]
    assert "RuntimeError" in result["failed"][0]


# ---------------------------------------------------------------------------
# fire_tracker_and_roadmap_detached (C5, 2026-07-23 wsc-tail-slim-down)
# ---------------------------------------------------------------------------


def _write_handoff(worktree_root: Path, name: str, roadmap_id: str | None) -> str:
    handoff_dir = worktree_root / "state" / "handoffs"
    handoff_dir.mkdir(parents=True, exist_ok=True)
    handoff_path = handoff_dir / name
    frontmatter = "---\nclaimed_by: sid-1\n"
    if roadmap_id is not None:
        frontmatter += f"roadmap_id: {roadmap_id}\n"
    frontmatter += "---\n\nbody\n"
    handoff_path.write_text(frontmatter, encoding="utf-8")
    return f"state/handoffs/{name}"


def test_fire_tracker_and_roadmap_detached_spawns_per_roadmap_callout(tmp_path):
    worktree_root = tmp_path
    rel_path = _write_handoff(worktree_root, "2026-07-23-a.md", "goal-example")

    spawned: list[tuple] = []

    def _fake_spawn(repo_root, script_path, args):
        spawned.append((repo_root, script_path, tuple(args)))
        return True

    with patch.object(tail_ops, "spawn_detached", side_effect=_fake_spawn) as mock_spawn:
        result = tail_ops.fire_tracker_and_roadmap_detached(worktree_root, [rel_path])

    assert mock_spawn.call_count == 1
    fired_scripts = [Path(script_path).name for _repo_root, script_path, _args in spawned]
    assert fired_scripts == ["refresh-roadmap-callout.py"]

    callout_call = spawned[0]
    assert callout_call[0] == str(worktree_root)
    assert callout_call[2] == ("--root", str(worktree_root), "goal-example")

    assert result["failed"] == []
    assert f"detached_fire:refresh-roadmap-callout.py:goal-example" in result["acted"]


def test_fire_tracker_and_roadmap_detached_no_roadmap_ids_is_clean_skip(tmp_path):
    worktree_root = tmp_path
    rel_path = _write_handoff(worktree_root, "2026-07-23-a.md", None)

    with patch.object(tail_ops, "spawn_detached", return_value=True) as mock_spawn:
        result = tail_ops.fire_tracker_and_roadmap_detached(worktree_root, [rel_path])

    # No roadmap id -- no spawn at all.
    assert mock_spawn.call_count == 0
    assert result["failed"] == []
    assert "detached_fire:refresh-roadmap-callout.py:no-roadmap-id" in result["skipped"]


def test_fire_tracker_and_roadmap_detached_dedupes_roadmap_ids(tmp_path):
    worktree_root = tmp_path
    rel_a = _write_handoff(worktree_root, "2026-07-23-a.md", "goal-example")
    rel_b = _write_handoff(worktree_root, "2026-07-23-b.md", "goal-example")

    with patch.object(tail_ops, "spawn_detached", return_value=True) as mock_spawn:
        result = tail_ops.fire_tracker_and_roadmap_detached(worktree_root, [rel_a, rel_b])

    # Exactly ONE roadmap-callout fire for the shared id (not two).
    assert mock_spawn.call_count == 1
    assert result["acted"].count("detached_fire:refresh-roadmap-callout.py:goal-example") == 1


def test_fire_tracker_and_roadmap_detached_records_spawn_failure(tmp_path):
    worktree_root = tmp_path
    rel_path = _write_handoff(worktree_root, "2026-07-23-a.md", "goal-example")

    with patch.object(tail_ops, "spawn_detached", return_value=False) as mock_spawn:
        result = tail_ops.fire_tracker_and_roadmap_detached(worktree_root, [rel_path])

    assert mock_spawn.call_count == 1
    assert len(result["failed"]) == 1
    assert "refresh-roadmap-callout.py" in result["failed"][0]
    assert result["acted"] == []


# ---------------------------------------------------------------------------
# housekeeping-liveness wiring (C17b follow-up)
# ---------------------------------------------------------------------------


def test_refresh_roadmap_callout_success_stamps_roadmap_callout(tmp_path):
    worktree_root = tmp_path / "repo"
    worktree_root.mkdir()
    (worktree_root / ".git").mkdir()
    handoff_dir = worktree_root / "state" / "handoffs"
    handoff_dir.mkdir(parents=True)
    handoff_path = handoff_dir / "2026-07-22-example.md"
    handoff_path.write_text(
        "---\nclaimed_by: sid-1\nroadmap_id: goal-example\n---\n\nbody\n", encoding="utf-8",
    )

    with patch("coordinator_core.ops.refresh_roadmap_callout.main", return_value=0):
        tail_ops.refresh_roadmap_callout(
            worktree_root, ["state/handoffs/2026-07-22-example.md"]
        )

    statuses = hl.liveness_status(str(worktree_root))
    assert statuses[hl.ROADMAP_CALLOUT] == hl.STATUS_FRESH


def test_refresh_roadmap_callout_all_skipped_does_not_stamp(tmp_path):
    worktree_root = tmp_path / "repo"
    worktree_root.mkdir()
    (worktree_root / ".git").mkdir()
    handoff_dir = worktree_root / "state" / "handoffs"
    handoff_dir.mkdir(parents=True)
    handoff_path = handoff_dir / "2026-07-22-example.md"
    handoff_path.write_text(
        "---\nclaimed_by: sid-1\n---\n\nbody, no roadmap_id here\n", encoding="utf-8",
    )

    with patch("coordinator_core.ops.refresh_roadmap_callout.main") as mock_main:
        tail_ops.refresh_roadmap_callout(
            worktree_root, ["state/handoffs/2026-07-22-example.md"]
        )

    mock_main.assert_not_called()
    statuses = hl.liveness_status(str(worktree_root))
    assert statuses[hl.ROADMAP_CALLOUT] == hl.STATUS_NEVER_STAMPED


# ---------------------------------------------------------------------------
# review_trail_metadata_complete -- the supply predicate `ceremony.wsc_tail`
# uses to decide the `b_adjudication_present` breach BEFORE it commits.
# ---------------------------------------------------------------------------


def test_metadata_complete_dict_all_fields():
    assert tail_ops.review_trail_metadata_complete(
        {"sha_range": "a..b", "reviewer": "code-reviewer", "scope": "session",
         "verdict": "ok", "diff_loc": "3"}
    ) is True


def test_metadata_complete_dict_missing_one_field():
    assert tail_ops.review_trail_metadata_complete(
        {"reviewer": "code-reviewer", "scope": "session", "verdict": "ok", "diff_loc": "3"}
    ) is False


def test_metadata_complete_dict_blank_field_counts_as_missing():
    assert tail_ops.review_trail_metadata_complete(
        {"sha_range": "", "reviewer": "code-reviewer", "scope": "session",
         "verdict": "ok", "diff_loc": "3"}
    ) is False


def test_metadata_complete_none_and_empty_shapes():
    assert tail_ops.review_trail_metadata_complete(None) is False
    assert tail_ops.review_trail_metadata_complete({}) is False
    assert tail_ops.review_trail_metadata_complete([]) is False


def test_metadata_complete_list_needs_only_one_qualifying_entry():
    """Mirrors `write_review_trail_many`'s own once-across-the-list gate: a
    caller supplying at least one complete slice has discharged the
    adjudication requirement, whatever the other entries look like."""
    assert tail_ops.review_trail_metadata_complete(
        [
            {"reviewer": "code-reviewer"},
            {"sha_range": "a..b", "reviewer": "code-reviewer", "scope": "session",
             "verdict": "ok", "diff_loc": "3"},
        ]
    ) is True


def test_metadata_complete_list_of_incomplete_entries_is_false():
    assert tail_ops.review_trail_metadata_complete(
        [{"reviewer": "code-reviewer"}, "not-a-dict", {}]
    ) is False


def test_metadata_complete_agrees_with_write_review_trail_gate(tmp_path):
    """The predicate and the writer must never disagree on the same input --
    the whole point of extracting it is that the pre-commit gate and the
    post-gate write decide identically."""
    common_dir = _make_common_dir(tmp_path)
    incomplete = {"reviewer": "code-reviewer", "scope": "session", "verdict": "ok"}

    assert tail_ops.review_trail_metadata_complete(incomplete) is False
    result = _run(
        tail_ops.write_review_trail(common_dir, incomplete, b_adjudication_present=True)
    )
    assert result["failed_critical"]
    assert result["metadata_supplied"] is False


def test_write_review_trail_many_isolates_a_raising_slice_from_its_siblings():
    """A slice that RAISES must not suppress its siblings' writes.

    Pins the isolation guarantee structurally rather than by contract. The
    sequential loop this function replaced was justified in its own docstring
    by exactly this property; `asyncio.gather`'s default
    (`return_exceptions=False`) would have propagated the first exception and
    cancelled the in-flight siblings, silently converting a documented
    guarantee into a claim that happens to hold only while
    `write_review_trail` keeps its never-raises contract. This test fails
    against that default.
    """
    calls: list = []

    async def fake_write(common_dir, entry, *, b_adjudication_present=False, _batch_context=None):
        calls.append(entry["sha_range"])
        if entry["sha_range"] == "bbb^..bbb":
            raise RuntimeError("simulated slice failure")
        return {"acted": [entry["sha_range"]], "skipped": [], "failed": [], "failed_critical": []}

    entries = [
        {"sha_range": f"{n}^..{n}", "reviewer": "code-reviewer", "scope": "session",
         "verdict": "ok", "diff_loc": "1"}
        for n in ("aaa", "bbb", "ccc")
    ]

    with patch.object(tail_ops, "write_review_trail", fake_write):
        result = asyncio.run(tail_ops.write_review_trail_many(Path("."), entries))

    assert "aaa^..aaa" in result["acted"], "a sibling before the raising slice lost its write"
    assert "ccc^..ccc" in result["acted"], "a sibling after the raising slice lost its write"
    assert any("bbb^..bbb" in m and "RuntimeError" in m for m in result["failed_critical"]),         "the raising slice must surface as failed_critical, not vanish"

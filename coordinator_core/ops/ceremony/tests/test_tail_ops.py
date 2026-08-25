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
    (l) fire_archive_sweeps_detached_spawns_expected_clis -- exactly the expected per-class
        CLIs are spawned, each with worktree_root as its repo_root arg; sweep-boot.py is
        never among them
    (m) fire_archive_sweeps_detached_records_spawn_failure -- a spawn_detached() False return
        lands in failed[], not raised

  coverage.gate and review_trail.write wrapper tests were removed along
  with their in-process wiring (coverage.gate: K-001, state/kill-ledger.md;
  review_trail.write: PM ruling 2026-08-23, kill review_trail.write -- the
  op module itself was deleted).

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


def test_fire_archive_sweeps_detached_spawns_expected_clis(tmp_path):
    """C2: the retired blocking archive_completed_handoffs/sweep_actioned_memos
    calls are replaced by detached CLI fires -- never the composite sweep-boot.py
    (plan § C2 anti-scope: it also runs the unintegrated-findings reap, a tracked
    git rm, out of scope for a call fired on every WSC pass). sweep-terminal-plans.py
    was removed when fleet.archive_completed_plans was killed and rebuilt from
    scratch (PM ruling 2026-08-23). sweep-actioned-memos.py was removed the same day
    when fleet.archive_actioned_memos was killed outright (PM ruling, no replacement
    op). sweep-shipped-handoffs.py itself was removed 2026-08-25 (C1b, docs/plans/
    2026-08-25-the-handoff-auto-archive-comes-back-capped.md -- the op it fired,
    fleet.archive_shipped_handoffs, was SUBSUMED into fleet.archive_completed_handoffs)
    -- C4 of the same plan re-earned the seam with sweep-terminal-handoffs.py, the
    sole current member of _ARCHIVE_SWEEP_SCRIPTS; this test now pins that single
    detached fire rather than the empty-roster interregnum."""
    worktree_root = tmp_path
    spawned: list[tuple] = []

    def _fake_spawn(repo_root, script_path, args):
        spawned.append((repo_root, script_path, tuple(args)))
        return True

    with patch.object(tail_ops, "spawn_detached", side_effect=_fake_spawn) as mock_spawn:
        result = tail_ops.fire_archive_sweeps_detached(worktree_root)

    repo_root_str = str(worktree_root)
    expected_script = str(Path(worktree_root, "coordinator", "bin", "sweep-terminal-handoffs.py"))
    assert mock_spawn.call_count == 1
    assert spawned == [(repo_root_str, expected_script, (repo_root_str,))]
    assert result == {"acted": ["detached_fire:sweep-terminal-handoffs.py"], "skipped": [], "failed": []}


def test_fire_archive_sweeps_detached_records_spawn_failure(tmp_path):
    """A `spawn_detached` False return for the sole current
    `_ARCHIVE_SWEEP_SCRIPTS` member (`sweep-terminal-handoffs.py`, C4) is
    recorded into `failed[]`, not silently dropped -- mirrors the loop body's
    unchanged spawn-failure handling."""
    worktree_root = tmp_path

    def _fake_spawn(repo_root, script_path, args):
        return False

    with patch.object(tail_ops, "spawn_detached", side_effect=_fake_spawn) as mock_spawn:
        result = tail_ops.fire_archive_sweeps_detached(worktree_root)

    assert mock_spawn.call_count == 1
    assert result == {
        "acted": [],
        "skipped": [],
        "failed": ["detached_fire:sweep-terminal-handoffs.py: spawn_detached returned False"],
    }


def test_unregistered_op_key_is_clean_failure(tmp_path):
    common_dir = _make_common_dir(tmp_path)
    result = _run(tail_ops._run_fleet_op_by_key("fleet.does_not_exist", "fleet.does_not_exist", common_dir))
    assert result["acted"] == []
    assert result["skipped"] == []
    assert result["failed"] == ["fleet.does_not_exist: fleet.does_not_exist not registered"]


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



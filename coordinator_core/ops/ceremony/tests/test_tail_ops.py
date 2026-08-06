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

  render_handoff_tracker / refresh_roadmap_callout (STEP_2_75, 2026-07-22 C9
  wiring-gap fix -- docs/plans/2026-07-16-wsc-pure-python-tail-rebuild.md § C9):
    (s) render_handoff_tracker_writes_file        -- happy path: section text lands
                                                      under the resolved state root
    (t) render_handoff_tracker_failure_is_fail_open -- a renderer/state-root exception
                                                        degrades to failed[], never raises
    (u) refresh_roadmap_callout_no_consumed_handoffs_is_clean_skip -- [] input -> skip,
                                                                       no subprocess call
    (v) refresh_roadmap_callout_missing_roadmap_id_skips_per_handoff -- a consumed handoff
                                                                         with no roadmap_id
                                                                         frontmatter is skipped
    (w) refresh_roadmap_callout_acts_when_id_present -- happy path: rc=0 -> acted[]
    (x) refresh_roadmap_callout_handler_exception_is_failed -- refresh main() raising
                                                                 degrades to failed[]

  render_handoff_tracker concurrency (C5, 2026-07-23 wsc-tail-slim-down, the Staff Engineer
  finding 5 -- atomic write + single-flight guard):
    (y) render_handoff_tracker_write_is_atomic_no_temp_file_left -- happy path leaves
        no stray `.tmp`/`.lock` file behind, only the final `handoff-tracker.md`
    (z) render_handoff_tracker_concurrent_render_is_clean_skip -- a pre-existing
        `.lock` file for the same target makes a second render a clean skip[],
        never a failure, and never touches the (simulated in-flight) target file

  fire_tracker_and_roadmap_detached (C5, 2026-07-23 wsc-tail-slim-down -- detached
  replacement for the BLOCKING render_handoff_tracker/refresh_roadmap_callout calls;
  mirrors fire_archive_sweeps_detached's C2 shape):
    (aa) fire_tracker_and_roadmap_detached_spawns_tracker_and_per_roadmap_callout --
         the tracker CLI fires unconditionally; the roadmap-callout CLI fires once
         per distinct allowlist-valid roadmap_id found in consumed_handoff_paths
    (bb) fire_tracker_and_roadmap_detached_no_roadmap_ids_is_clean_skip -- no
         consumed handoff carries a roadmap_id -> skipped[], zero callout spawns
    (cc) fire_tracker_and_roadmap_detached_dedupes_roadmap_ids -- two consumed
         handoffs sharing one roadmap_id fire the callout CLI only once
    (dd) fire_tracker_and_roadmap_detached_records_spawn_failure -- a spawn_detached()
         False return for either CLI lands in failed[], the other still fires

  housekeeping-liveness wiring (C17b follow-up -- the four missing `stamp_liveness`
  call sites; see housekeeping_liveness.py's own module docstring for the
  three-state contract these calls feed):
    (ee) render_handoff_tracker_success_stamps_tracker_regen -- happy-path write
         stamps TRACKER_REGEN
    (ff) render_handoff_tracker_failure_does_not_stamp -- the fail-open exception
         branch leaves TRACKER_REGEN unstamped
    (gg) refresh_roadmap_callout_success_stamps_roadmap_callout -- at least one
         acted[] entry stamps ROADMAP_CALLOUT
    (hh) refresh_roadmap_callout_all_skipped_does_not_stamp -- an all-skipped pass
         (no roadmap_id anywhere) leaves ROADMAP_CALLOUT unstamped
    (ii) coverage_gate_covered_stamps_coverage_gate -- exit_code == 0 stamps
         COVERAGE_GATE (against common_dir)
    (jj) coverage_gate_indeterminate_does_not_stamp -- exit_code == 2 leaves
         COVERAGE_GATE unstamped

Spec backlink: docs/plans/2026-07-16-wsc-pure-python-tail-rebuild.md § C6, § C9.
Spec backlink: docs/plans/2026-07-23-wsc-tail-slim-down.md § C5.
Spec backlink: docs/plans/2026-07-23-wsc-tail-slim-down.md § C17b (liveness call sites).
"""

from __future__ import annotations

import asyncio
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
# coverage.gate wrapper
# ---------------------------------------------------------------------------


def test_coverage_gate_covered_verdict_is_acted(tmp_path):
    common_dir = _make_common_dir(tmp_path)

    async def _handler(params: dict, repo_root=None) -> dict:
        return {"verdict_line": "VERDICT=COVERED range=abc..def", "exit_code": 0, "notes": []}

    with patch.object(tail_ops, "get_op_handler", return_value=_handler):
        result = _run(tail_ops.run_coverage_gate(common_dir, closing_session_id="sid-1"))

    assert result["failed"] == []
    assert result["acted"] == [f"{tail_ops.OP_COVERAGE_GATE}:COVERED"]


def test_coverage_gate_indeterminate_is_failed(tmp_path):
    common_dir = _make_common_dir(tmp_path)

    async def _handler(params: dict, repo_root=None) -> dict:
        return {"verdict_line": "VERDICT=INDETERMINATE", "exit_code": 2, "notes": ["no chain"]}

    with patch.object(tail_ops, "get_op_handler", return_value=_handler):
        result = _run(tail_ops.run_coverage_gate(common_dir, closing_session_id="sid-1"))

    assert result["acted"] == []
    assert len(result["failed"]) == 1
    assert "INDETERMINATE" in result["failed"][0]


def test_coverage_gate_not_registered_is_clean_failure(tmp_path):
    common_dir = _make_common_dir(tmp_path)
    with patch.object(tail_ops, "get_op_handler", return_value=None):
        result = _run(tail_ops.run_coverage_gate(common_dir))
    assert result["failed"] == [f"{tail_ops.OP_COVERAGE_GATE}: {tail_ops.OP_COVERAGE_GATE} not registered"]


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
# render_handoff_tracker / refresh_roadmap_callout (STEP_2_75, C9 wiring-gap fix)
# ---------------------------------------------------------------------------


def test_render_handoff_tracker_writes_file(tmp_path):
    worktree_root = tmp_path / "repo"
    worktree_root.mkdir()
    state_root = tmp_path / "repo" / "state"

    with (
        patch(
            "coordinator_core.ops.ceremony.renderers.render_repo_section",
            return_value="## Handoffs\n\nno active handoffs.",
        ),
        patch(
            "coordinator_core.state_root.coordinator_state_root",
            return_value=str(state_root),
        ),
    ):
        result = tail_ops.render_handoff_tracker(worktree_root)

    assert result == {
        "acted": [tail_ops.OP_HANDOFF_TRACKER], "skipped": [], "failed": [],
        "handoff_tracker_path": "state/handoff-tracker.md",
    }
    out_path = state_root / "handoff-tracker.md"
    assert out_path.is_file()
    content = out_path.read_text(encoding="utf-8")
    assert "# Handoff Tracker" in content
    assert "no active handoffs." in content


def test_render_handoff_tracker_outside_worktree_omits_stage_path(tmp_path):
    """A central (meta-repo) state root outside worktree_root -- e.g. Claude-klabauter's own
    state tree -- writes successfully but carries NO `handoff_tracker_path`, since
    the artifact lands in a DIFFERENT git repo and is never dirty in THIS worktree
    (nothing for the caller to stage)."""
    worktree_root = tmp_path / "repo"
    worktree_root.mkdir()
    central_state_root = tmp_path / "elsewhere" / "state"

    with (
        patch(
            "coordinator_core.ops.ceremony.renderers.render_repo_section",
            return_value="## Handoffs\n\nno active handoffs.",
        ),
        patch(
            "coordinator_core.state_root.coordinator_state_root",
            return_value=str(central_state_root),
        ),
    ):
        result = tail_ops.render_handoff_tracker(worktree_root)

    assert result["acted"] == [tail_ops.OP_HANDOFF_TRACKER]
    assert result["failed"] == []
    assert "handoff_tracker_path" not in result
    assert (central_state_root / "handoff-tracker.md").is_file()


def test_render_handoff_tracker_failure_is_fail_open(tmp_path):
    worktree_root = tmp_path / "repo"
    worktree_root.mkdir()

    with patch(
        "coordinator_core.ops.ceremony.renderers.render_repo_section",
        side_effect=RuntimeError("boom"),
    ):
        result = tail_ops.render_handoff_tracker(worktree_root)

    assert result["acted"] == []
    assert result["skipped"] == []
    assert len(result["failed"]) == 1
    assert tail_ops.OP_HANDOFF_TRACKER in result["failed"][0]
    assert "RuntimeError" in result["failed"][0]


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
# render_handoff_tracker concurrency -- atomic write + single-flight guard (C5)
# ---------------------------------------------------------------------------


def test_render_handoff_tracker_write_is_atomic_no_temp_file_left(tmp_path):
    """Happy path: only the final `handoff-tracker.md` (plus the housekeeping-liveness
    stamp -- C17b follow-up, TRACKER_REGEN's success-path `stamp_liveness` call) lands
    on disk -- no stray `.tmp` (atomic-rename scratch file) or `.lock` (single-flight
    guard) file is left behind after a successful render."""
    worktree_root = tmp_path / "repo"
    worktree_root.mkdir()
    state_root = tmp_path / "repo" / "state"

    with (
        patch(
            "coordinator_core.ops.ceremony.renderers.render_repo_section",
            return_value="## Handoffs\n\nno active handoffs.",
        ),
        patch(
            "coordinator_core.state_root.coordinator_state_root",
            return_value=str(state_root),
        ),
    ):
        result = tail_ops.render_handoff_tracker(worktree_root)

    assert result["failed"] == []
    entries = sorted(p.name for p in state_root.iterdir())
    assert entries == ["handoff-tracker.md", "housekeeping-liveness.json"]


def test_render_handoff_tracker_concurrent_render_is_clean_skip(tmp_path):
    """A pre-existing `.lock` file for the same target path (simulating a
    concurrent in-flight render) makes this call a clean single-flight skip --
    never a failure, and the target file is left untouched (never opened for
    write, per the O_CREAT|O_EXCL guard firing before the renderer output is
    computed... rendering itself already happened by then, so this asserts on
    the WRITE step specifically: the pre-existing target content is preserved)."""
    worktree_root = tmp_path / "repo"
    worktree_root.mkdir()
    state_root = tmp_path / "repo" / "state"
    state_root.mkdir(parents=True)
    out_path = state_root / "handoff-tracker.md"
    out_path.write_text("PRE-EXISTING CONTENT\n", encoding="utf-8")
    lock_path = state_root / "handoff-tracker.md.lock"
    lock_path.write_text("", encoding="utf-8")

    with (
        patch(
            "coordinator_core.ops.ceremony.renderers.render_repo_section",
            return_value="## Handoffs\n\nno active handoffs.",
        ),
        patch(
            "coordinator_core.state_root.coordinator_state_root",
            return_value=str(state_root),
        ),
    ):
        result = tail_ops.render_handoff_tracker(worktree_root)

    assert result["acted"] == []
    assert result["failed"] == []
    assert result["skipped"] == [f"{tail_ops.OP_HANDOFF_TRACKER}:render-in-flight"]
    # The in-flight lock holder owns the write -- this caller must not have
    # clobbered the pre-existing target content.
    assert out_path.read_text(encoding="utf-8") == "PRE-EXISTING CONTENT\n"
    # This call never created/removed the lock -- it saw it already there.
    assert lock_path.is_file()


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


def test_fire_tracker_and_roadmap_detached_spawns_tracker_and_per_roadmap_callout(tmp_path):
    worktree_root = tmp_path
    rel_path = _write_handoff(worktree_root, "2026-07-23-a.md", "goal-example")

    spawned: list[tuple] = []

    def _fake_spawn(repo_root, script_path, args):
        spawned.append((repo_root, script_path, tuple(args)))
        return True

    with patch.object(tail_ops, "spawn_detached", side_effect=_fake_spawn) as mock_spawn:
        result = tail_ops.fire_tracker_and_roadmap_detached(worktree_root, [rel_path])

    assert mock_spawn.call_count == 2
    fired_scripts = [Path(script_path).name for _repo_root, script_path, _args in spawned]
    assert fired_scripts == ["render-handoff-tracker.py", "refresh-roadmap-callout.py"]

    tracker_call = spawned[0]
    assert tracker_call[0] == str(worktree_root)
    assert tracker_call[2] == (str(worktree_root),)

    callout_call = spawned[1]
    assert callout_call[0] == str(worktree_root)
    assert callout_call[2] == ("--root", str(worktree_root), "goal-example")

    assert result["failed"] == []
    assert f"detached_fire:render-handoff-tracker.py" in result["acted"]
    assert f"detached_fire:refresh-roadmap-callout.py:goal-example" in result["acted"]


def test_fire_tracker_and_roadmap_detached_no_roadmap_ids_is_clean_skip(tmp_path):
    worktree_root = tmp_path
    rel_path = _write_handoff(worktree_root, "2026-07-23-a.md", None)

    with patch.object(tail_ops, "spawn_detached", return_value=True) as mock_spawn:
        result = tail_ops.fire_tracker_and_roadmap_detached(worktree_root, [rel_path])

    # Only the tracker CLI fires -- no roadmap-callout spawn.
    assert mock_spawn.call_count == 1
    assert result["failed"] == []
    assert "detached_fire:refresh-roadmap-callout.py:no-roadmap-id" in result["skipped"]


def test_fire_tracker_and_roadmap_detached_dedupes_roadmap_ids(tmp_path):
    worktree_root = tmp_path
    rel_a = _write_handoff(worktree_root, "2026-07-23-a.md", "goal-example")
    rel_b = _write_handoff(worktree_root, "2026-07-23-b.md", "goal-example")

    with patch.object(tail_ops, "spawn_detached", return_value=True) as mock_spawn:
        result = tail_ops.fire_tracker_and_roadmap_detached(worktree_root, [rel_a, rel_b])

    # Tracker (1) + exactly ONE roadmap-callout fire for the shared id (not two).
    assert mock_spawn.call_count == 2
    assert result["acted"].count("detached_fire:refresh-roadmap-callout.py:goal-example") == 1


def test_fire_tracker_and_roadmap_detached_records_spawn_failure(tmp_path):
    worktree_root = tmp_path
    rel_path = _write_handoff(worktree_root, "2026-07-23-a.md", "goal-example")

    def _fake_spawn(repo_root, script_path, args):
        return "render-handoff-tracker.py" not in script_path

    with patch.object(tail_ops, "spawn_detached", side_effect=_fake_spawn) as mock_spawn:
        result = tail_ops.fire_tracker_and_roadmap_detached(worktree_root, [rel_path])

    assert mock_spawn.call_count == 2
    assert len(result["failed"]) == 1
    assert "render-handoff-tracker.py" in result["failed"][0]
    assert "detached_fire:refresh-roadmap-callout.py:goal-example" in result["acted"]


# ---------------------------------------------------------------------------
# housekeeping-liveness wiring (C17b follow-up)
# ---------------------------------------------------------------------------


def test_render_handoff_tracker_success_stamps_tracker_regen(tmp_path):
    worktree_root = tmp_path / "repo"
    worktree_root.mkdir()
    state_root = worktree_root / "state"

    with (
        patch(
            "coordinator_core.ops.ceremony.renderers.render_repo_section",
            return_value="## Handoffs\n\nno active handoffs.",
        ),
        patch(
            "coordinator_core.state_root.coordinator_state_root",
            return_value=str(state_root),
        ),
    ):
        tail_ops.render_handoff_tracker(worktree_root)

    statuses = hl.liveness_status(str(worktree_root))
    assert statuses[hl.TRACKER_REGEN] == hl.STATUS_FRESH


def test_render_handoff_tracker_failure_does_not_stamp(tmp_path):
    worktree_root = tmp_path / "repo"
    worktree_root.mkdir()

    with patch(
        "coordinator_core.ops.ceremony.renderers.render_repo_section",
        side_effect=RuntimeError("boom"),
    ):
        tail_ops.render_handoff_tracker(worktree_root)

    statuses = hl.liveness_status(str(worktree_root))
    assert statuses[hl.TRACKER_REGEN] == hl.STATUS_NEVER_STAMPED


def test_refresh_roadmap_callout_success_stamps_roadmap_callout(tmp_path):
    worktree_root = tmp_path / "repo"
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


def test_coverage_gate_covered_stamps_coverage_gate(tmp_path):
    common_dir = _make_common_dir(tmp_path)

    async def _handler(params: dict, repo_root=None) -> dict:
        return {"verdict_line": "VERDICT=COVERED range=abc..def", "exit_code": 0, "notes": []}

    with patch.object(tail_ops, "get_op_handler", return_value=_handler):
        _run(tail_ops.run_coverage_gate(common_dir, closing_session_id="sid-1"))

    statuses = hl.liveness_status(str(common_dir))
    assert statuses[hl.COVERAGE_GATE] == hl.STATUS_FRESH


def test_coverage_gate_indeterminate_does_not_stamp(tmp_path):
    common_dir = _make_common_dir(tmp_path)

    async def _handler(params: dict, repo_root=None) -> dict:
        return {"verdict_line": "VERDICT=INDETERMINATE", "exit_code": 2, "notes": ["no chain"]}

    with patch.object(tail_ops, "get_op_handler", return_value=_handler):
        _run(tail_ops.run_coverage_gate(common_dir, closing_session_id="sid-1"))

    statuses = hl.liveness_status(str(common_dir))
    assert statuses[hl.COVERAGE_GATE] == hl.STATUS_NEVER_STAMPED

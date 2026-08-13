"""
coordinator_core.ops.tests.test_distill_apply_disposal

Unit tests for coordinator_core.ops.distill_apply_disposal — the
"distill.apply_disposal" op (C14 — Phase 5 delete tier).

Coverage (each fixture named per the classification.py five-question
affirmation block's bound->test citations, DR-228 § D2a/D2b):
  verify_stamp_and_throttle (gates 2-4):
    - test_verify_stamp_and_throttle_refuses_unstamped
    - test_verify_stamp_and_throttle_refuses_sha_drift
    - test_verify_stamp_and_throttle_refuses_unacknowledged_mass_throttle
    - test_verify_stamp_and_throttle_passes_acknowledged_mass_throttle
    - test_verify_stamp_and_throttle_passes_below_soft_band (E2 hard-cap fix)
    - test_verify_stamp_and_throttle_refuses_above_hard_cap_even_with_ack
      (E2 mass-throttle hard-cap fix — 2026-07-23 distill-guard memo-class
      blind-spots)
  verify_drain_ordering (gate 5, D2b-vii):
    - test_verify_drain_ordering_refuses_non_ancestor_sha (bare-ancestor-of-
      nothing negative fixture, F4)
    - test_verify_drain_ordering_refuses_ancestor_without_containment
    - test_verify_drain_ordering_passes_ancestor_with_log_containment
  compute_apply_plan (D2a-i/ii/iv):
    - test_toctou_reblock_skips_newly_blocked_row (D2a-iv)
    - test_already_deleted_path_is_skipped (D2a-i groundwork)
    - test_order_shuffled_is_commutative (D2a-ii)
  apply_disposal_manifest (full act, git-level):
    - test_apply_disposal_manifest_deletes_tracked_and_appends_log
    - test_apply_disposal_manifest_deletes_untracked_no_commit_for_that_half
    - test_replay_is_idempotent_no_op (D2a-i, full replay fixture)
    - test_apply_disposal_manifest_absent_run_raises (D2a-iii cwd-scope)
    - test_git_add_failure_reverts_log_path_and_replay_has_no_duplicates
      (log-append-failure revert-gap regression — see 2026-07-23 code review
      BLOCK finding on _delete_tracked_and_append_log's git-add-failure branch)
    - test_git_add_failure_and_revert_failure_both_surface_in_reason
      (revert-checkout-return-code regression — 2026-07-23 code review
      non-blocking observation: all three revert branches ignored the
      checkout's own return code)
  write_apply_receipt:
    - test_write_apply_receipt_shape
  handler:
    - test_handler_raises_on_repo_root_none
    - test_handler_raises_on_missing_harvest_sha
    - test_handler_end_to_end_dispatch_message_smoke (assemble -> stamp -> apply)
  lineage denormalization (2026-07-23 cockpit lineage-severing fix — module
  docstring § Lineage denormalization):
    - test_denorm_predecessor_edge_appends_disposed_successor_before_delete
    - test_denorm_origin_handoff_edge_appends_disposed_successor_before_delete
    - test_denorm_skips_when_parent_also_in_disposal_batch
    - test_denorm_idempotent_noop_when_parent_already_has_matching_entry
    - test_denorm_two_disposed_children_of_one_parent_both_get_entries
    - test_denorm_compute_apply_plan_alone_writes_nothing
    - test_denorm_partial_batch_failure_only_succeeded_child_gets_entry
      (gate-on-actual-delete-success regression — 2026-07-23 code review
      finding on commit 5e8f3d38: denorm writes were derived from the
      pre-delete plan, not per-child delete outcomes)
    - test_denorm_untracked_child_unlink_failure_writes_no_denorm_entry
      (same regression, untracked-child leg)

Spec backlink: pln-claude-klabauter-driven-ceremony-redesig-c7fe9a § C14
Governing DR: docs/decisions/DR-228-distill-disposal-substrate-writer-category.md § D2a, D2b, D3, D4
"""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from coordinator_core.distill import log_append as _log_append
from coordinator_core.distill import manifest_schema as _schema
from coordinator_core.ops.distill_apply_disposal import (
    MASS_THROTTLE_ACK_MARKER,
    MASS_THROTTLE_HARD_CAP,
    ApplyDisposalError,
    _handler,
    apply_disposal_manifest,
    compute_apply_plan,
    verify_drain_ordering,
    verify_stamp_and_throttle,
    write_apply_receipt,
)
from coordinator_core.ops.distill_stamp_disposal import (
    DisposalStampError,
    load_disposal_manifest,
    manifest_path_for_run,
)

_HAS_RG = shutil.which("rg") is not None
_requires_rg = pytest.mark.skipif(not _HAS_RG, reason="ripgrep (rg) not installed")


def _run(coro):
    return asyncio.run(coro)


def _git(*args: str, cwd: Path) -> str:
    result = subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    )
    return result.stdout.strip()


def _init_repo_with_commits(tmp_path: Path) -> dict[str, str]:
    """Sets up a repo with:
      commit A (root)   — touches only README.md (no log/wiki containment)
      commit B (harvest) — touches state/distillation-log.md (containment target)
      commit C (HEAD)    — adds the eligible handoff candidate

    Returns {"root": <sha>, "harvest": <sha>, "head": <sha>}.
    """
    _git("init", "-q", cwd=tmp_path)
    _git("config", "user.email", "test@example.com", cwd=tmp_path)
    _git("config", "user.name", "Test", cwd=tmp_path)

    (tmp_path / "README.md").write_text("root\n", encoding="utf-8")
    _git("add", "--", "README.md", cwd=tmp_path)
    _git("commit", "-q", "-m", "root commit", cwd=tmp_path)
    root_sha = _git("rev-parse", "HEAD", cwd=tmp_path)

    log_path = tmp_path / "state" / "distillation-log.md"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text("## Run seed\n- seed -> EPHEMERAL, seed (run: seed)\n", encoding="utf-8")
    _git("add", "--", "state/distillation-log.md", cwd=tmp_path)
    _git("commit", "-q", "-m", "harvest commit", cwd=tmp_path)
    harvest_sha = _git("rev-parse", "HEAD", cwd=tmp_path)

    (tmp_path / "state" / "cross-repo-commitments").mkdir(parents=True, exist_ok=True)
    handoff = tmp_path / "archive" / "handoffs" / "eligible.md"
    handoff.parent.mkdir(parents=True, exist_ok=True)
    handoff.write_text(
        "---\n"
        "shipped_in: 68b27420\n"
        "status: consumed\n"
        "deployment_state: shipped\n"
        "realized_by: inline\n"
        "---\n"
        "body\n",
        encoding="utf-8",
    )
    _git("add", "--", "archive/handoffs/eligible.md", cwd=tmp_path)
    _git("commit", "-q", "-m", "add candidate", cwd=tmp_path)
    head_sha = _git("rev-parse", "HEAD", cwd=tmp_path)

    return {"root": root_sha, "harvest": harvest_sha, "head": head_sha}


def _eligible_row(path: str, artifact_class: str = "handoff") -> dict:
    log_row = _log_append.render_row(path, "EPHEMERAL", f"disposed ({artifact_class})", "run-1")
    return _schema.make_disposal_row(
        path=path,
        artifact_class=artifact_class,
        guards_run=[_schema.make_guard_receipt("shipped_in", "pass", "sha=68b27420")],
        eligible=True,
        log_row=log_row,
    )


def _manifest(rows: list[dict], run_id: str = "2026-07-23-01h00", mass_throttle: bool = False) -> dict:
    total = len(rows)
    eligible = sum(1 for r in rows if r["eligible"])
    return _schema.make_disposal_manifest(
        run_id=run_id,
        rows=rows,
        scan_stats=_schema.make_scan_stats(total, eligible, total - eligible),
        mass_throttle=mass_throttle,
    )


def _stamped(manifest: dict, *, note: str = "approved") -> dict:
    sha = _schema.compute_manifest_sha(manifest)
    return _schema.apply_stamp(manifest, by="pm", at="2026-07-23T10:00:00Z", sha=sha, note=note)


# ---------------------------------------------------------------------------
# verify_stamp_and_throttle — gates 2-4
# ---------------------------------------------------------------------------


def test_verify_stamp_and_throttle_refuses_unstamped():
    manifest = _manifest([_eligible_row("archive/handoffs/eligible.md")])
    with pytest.raises(ApplyDisposalError, match="unstamped"):
        verify_stamp_and_throttle(manifest)


def test_verify_stamp_and_throttle_refuses_sha_drift():
    manifest = _manifest([_eligible_row("archive/handoffs/eligible.md")])
    stamped = _stamped(manifest)
    # Mutate the body AFTER stamping (simulates a re-assemble or hand-edit).
    drifted = dict(stamped)
    drifted["rows"] = drifted["rows"] + [_eligible_row("archive/handoffs/second.md")]
    with pytest.raises(ApplyDisposalError, match="DRIFTED"):
        verify_stamp_and_throttle(drifted)


def test_verify_stamp_and_throttle_refuses_unacknowledged_mass_throttle():
    manifest = _manifest([_eligible_row("archive/handoffs/eligible.md")], mass_throttle=True)
    stamped = _stamped(manifest, note="approved, no special mention")
    with pytest.raises(ApplyDisposalError, match="mass_throttle"):
        verify_stamp_and_throttle(stamped)


def test_verify_stamp_and_throttle_passes_acknowledged_mass_throttle():
    manifest = _manifest([_eligible_row("archive/handoffs/eligible.md")], mass_throttle=True)
    stamped = _stamped(manifest, note=f"approved, {MASS_THROTTLE_ACK_MARKER}")
    verify_stamp_and_throttle(stamped)  # must not raise


def test_verify_stamp_and_throttle_passes_below_soft_band():
    # No mass_throttle flag, no ack needed -- ordinary small run applies.
    manifest = _manifest([_eligible_row("archive/handoffs/eligible.md")])
    stamped = _stamped(manifest, note="approved")
    verify_stamp_and_throttle(stamped)  # must not raise


def test_verify_stamp_and_throttle_refuses_above_hard_cap_even_with_ack():
    # E2: MASS_THROTTLE_HARD_CAP is an unconditional ceiling -- the soft-band
    # ack (MASS_THROTTLE_ACK_MARKER) authorizes the ratio/absolute -> hard-cap
    # band but cannot lift the hard cap itself.
    rows = [
        _eligible_row(f"archive/handoffs/eligible-{i}.md")
        for i in range(MASS_THROTTLE_HARD_CAP + 1)
    ]
    manifest = _manifest(rows, mass_throttle=True)
    stamped = _stamped(manifest, note=f"approved, {MASS_THROTTLE_ACK_MARKER}")
    with pytest.raises(ApplyDisposalError, match="MASS_THROTTLE_HARD_CAP"):
        verify_stamp_and_throttle(stamped)


# ---------------------------------------------------------------------------
# verify_drain_ordering — gate 5 (D2b-vii)
# ---------------------------------------------------------------------------


def test_verify_drain_ordering_refuses_non_ancestor_sha(tmp_path: Path):
    shas = _init_repo_with_commits(tmp_path)
    bogus_sha = "0" * 40
    with pytest.raises(ApplyDisposalError, match="not an ancestor"):
        _run(verify_drain_ordering(tmp_path, bogus_sha))
    # Sanity: shas dict is well-formed (root predates harvest predates head).
    assert shas["root"] != shas["harvest"] != shas["head"]


def test_verify_drain_ordering_refuses_ancestor_without_containment(tmp_path: Path):
    shas = _init_repo_with_commits(tmp_path)
    with pytest.raises(ApplyDisposalError, match="neither"):
        _run(verify_drain_ordering(tmp_path, shas["root"]))


def test_verify_drain_ordering_passes_ancestor_with_log_containment(tmp_path: Path):
    shas = _init_repo_with_commits(tmp_path)
    _run(verify_drain_ordering(tmp_path, shas["harvest"]))  # must not raise


# ---------------------------------------------------------------------------
# compute_apply_plan — D2a-i/ii/iv
# ---------------------------------------------------------------------------


@_requires_rg
def test_already_deleted_path_is_skipped(tmp_path: Path):
    _init_repo_with_commits(tmp_path)
    manifest = _manifest([_eligible_row("archive/handoffs/does-not-exist.md")])
    plan = compute_apply_plan(tmp_path, manifest)
    assert plan.survivors == []
    assert plan.already_deleted == [
        {"path": "archive/handoffs/does-not-exist.md", "reason": "already-deleted"}
    ]


@_requires_rg
def test_toctou_reblock_skips_newly_blocked_row(tmp_path: Path):
    _init_repo_with_commits(tmp_path)
    # Overwrite the candidate AFTER assemble time to strip shipped_in — the
    # manifest still says eligible=True (stale), but a re-check now blocks it.
    handoff = tmp_path / "archive" / "handoffs" / "eligible.md"
    handoff.write_text("---\nstatus: consumed\n---\nbody\n", encoding="utf-8")

    manifest = _manifest([_eligible_row("archive/handoffs/eligible.md")])
    plan = compute_apply_plan(tmp_path, manifest)
    assert plan.survivors == []
    assert len(plan.newly_blocked) == 1
    assert plan.newly_blocked[0]["path"] == "archive/handoffs/eligible.md"
    assert "newly-blocked-at-apply-time" in plan.newly_blocked[0]["reason"]


@_requires_rg
def test_order_shuffled_is_commutative(tmp_path: Path):
    _init_repo_with_commits(tmp_path)
    second = tmp_path / "archive" / "handoffs" / "aaa-second.md"
    second.write_text(
        "---\nshipped_in: 68b27420\nstatus: consumed\ndeployment_state: shipped\n"
        "realized_by: inline\n---\nbody\n",
        encoding="utf-8",
    )
    _git("add", "--", "archive/handoffs/aaa-second.md", cwd=tmp_path)
    _git("commit", "-q", "-m", "add second candidate", cwd=tmp_path)

    rows = [
        _eligible_row("archive/handoffs/eligible.md"),
        _eligible_row("archive/handoffs/aaa-second.md"),
    ]
    manifest_forward = _manifest(list(rows))
    manifest_reversed = _manifest(list(reversed(rows)))

    plan_forward = compute_apply_plan(tmp_path, manifest_forward)
    plan_reversed = compute_apply_plan(tmp_path, manifest_reversed)

    forward_paths = [r["path"] for r in plan_forward.survivors]
    reversed_paths = [r["path"] for r in plan_reversed.survivors]
    assert forward_paths == reversed_paths == sorted(forward_paths)


# ---------------------------------------------------------------------------
# apply_disposal_manifest — full act (git-level)
# ---------------------------------------------------------------------------


@_requires_rg
def test_apply_disposal_manifest_deletes_tracked_and_appends_log(tmp_path: Path):
    shas = _init_repo_with_commits(tmp_path)
    manifest = _manifest([_eligible_row("archive/handoffs/eligible.md")])
    stamped = _stamped(manifest)

    result = _run(
        apply_disposal_manifest(tmp_path, stamped, harvest_committed_sha=shas["harvest"])
    )
    assert result.deleted_tracked == ["archive/handoffs/eligible.md"]
    assert result.deleted_untracked == []
    assert result.already_deleted == []
    assert result.newly_blocked == []
    assert result.failed == []

    assert not (tmp_path / "archive" / "handoffs" / "eligible.md").exists()
    log_text = (tmp_path / "state" / "distillation-log.md").read_text(encoding="utf-8")
    assert "archive/handoffs/eligible.md -> EPHEMERAL, disposed (handoff)" in log_text

    # Committed, not merely written to the worktree.
    status = _git("status", "--porcelain", cwd=tmp_path)
    assert status == ""


@_requires_rg
def test_apply_disposal_manifest_deletes_untracked_no_commit_for_that_half(tmp_path: Path):
    shas = _init_repo_with_commits(tmp_path)
    untracked = tmp_path / "archive" / "handoffs" / "untracked.md"
    untracked.write_text(
        "---\nshipped_in: 68b27420\nstatus: consumed\ndeployment_state: shipped\n"
        "realized_by: inline\n---\nbody\n",
        encoding="utf-8",
    )
    # Deliberately NOT git-added — stays untracked.

    manifest = _manifest([_eligible_row("archive/handoffs/untracked.md")])
    stamped = _stamped(manifest)

    result = _run(
        apply_disposal_manifest(tmp_path, stamped, harvest_committed_sha=shas["harvest"])
    )
    assert result.deleted_tracked == []
    assert result.deleted_untracked == ["archive/handoffs/untracked.md"]
    assert not untracked.exists()

    log_text = (tmp_path / "state" / "distillation-log.md").read_text(encoding="utf-8")
    assert "archive/handoffs/untracked.md -> EPHEMERAL, disposed (handoff)" in log_text
    # The log file itself IS tracked, so its update is committed.
    status = _git("status", "--porcelain", cwd=tmp_path)
    assert status == ""


@_requires_rg
def test_replay_is_idempotent_no_op(tmp_path: Path):
    shas = _init_repo_with_commits(tmp_path)
    manifest = _manifest([_eligible_row("archive/handoffs/eligible.md")])
    stamped = _stamped(manifest)

    first = _run(
        apply_disposal_manifest(tmp_path, stamped, harvest_committed_sha=shas["harvest"])
    )
    assert first.deleted_tracked == ["archive/handoffs/eligible.md"]

    log_text_after_first = (tmp_path / "state" / "distillation-log.md").read_text(encoding="utf-8")

    second = _run(
        apply_disposal_manifest(tmp_path, stamped, harvest_committed_sha=shas["harvest"])
    )
    assert second.deleted_tracked == []
    assert second.deleted_untracked == []
    assert second.already_deleted == [
        {"path": "archive/handoffs/eligible.md", "reason": "already-deleted"}
    ]

    log_text_after_second = (tmp_path / "state" / "distillation-log.md").read_text(encoding="utf-8")
    assert log_text_after_second == log_text_after_first
    assert log_text_after_second.count("archive/handoffs/eligible.md ->") == 1


@_requires_rg
def test_git_add_failure_reverts_log_path_and_replay_has_no_duplicates(
    tmp_path: Path, monkeypatch
):
    """A `git add` failure on the canonical-log path (simulated via a
    real-git-backed wrapper that fails only that one call) must revert
    log_path's own on-disk mutation, not just the git-rm'd candidates —
    otherwise a retry of the same run_id appends duplicate log rows
    (violates D2a-i idempotent replay)."""
    import coordinator_core.ops.distill_apply_disposal as _mod

    shas = _init_repo_with_commits(tmp_path)
    manifest = _manifest([_eligible_row("archive/handoffs/eligible.md")])
    stamped = _stamped(manifest)

    log_path = tmp_path / "state" / "distillation-log.md"
    pre_content = log_path.read_bytes()

    real_run_git = _mod._run_git

    async def failing_add_run_git(*args: str, cwd: Path, env: dict[str, str]):
        if args and args[0] == "add" and str(log_path) in args:
            return 1, b"", b"simulated git-add failure for log_path"
        return await real_run_git(*args, cwd=cwd, env=env)

    monkeypatch.setattr(_mod, "_run_git", failing_add_run_git)

    first = _run(
        apply_disposal_manifest(tmp_path, stamped, harvest_committed_sha=shas["harvest"])
    )
    assert first.deleted_tracked == []
    assert any("log-stage-failed" in f["reason"] for f in first.failed)

    # Both halves of the failed attempt are reverted: the git-rm'd candidate
    # is back, and the log's on-disk content matches its pre-run state.
    assert (tmp_path / "archive" / "handoffs" / "eligible.md").exists()
    assert log_path.read_bytes() == pre_content
    assert _git("status", "--porcelain", cwd=tmp_path) == ""

    monkeypatch.setattr(_mod, "_run_git", real_run_git)

    second = _run(
        apply_disposal_manifest(tmp_path, stamped, harvest_committed_sha=shas["harvest"])
    )
    assert second.deleted_tracked == ["archive/handoffs/eligible.md"]
    log_text = log_path.read_text(encoding="utf-8")
    assert log_text.count("archive/handoffs/eligible.md ->") == 1


def test_git_add_failure_and_revert_failure_both_surface_in_reason(
    tmp_path: Path, monkeypatch
):
    """When the log-stage-failed revert path's own `git checkout HEAD --`
    ALSO fails (disk full, index lock contention, ...), the original
    log-stage failure must remain the primary reported cause — never masked
    by the revert failure — while the revert failure is surfaced as
    additional context naming the un-reverted path, so neither the receipt
    nor the caller silently inherits a working tree left inconsistent with a
    false disposal claim still on disk. Regression for the gap named in the
    2026-07-23 code-review non-blocking observation on
    `_delete_tracked_and_append_log`'s three revert branches: all three
    previously ignored the checkout's own return code."""
    import coordinator_core.ops.distill_apply_disposal as _mod

    shas = _init_repo_with_commits(tmp_path)
    manifest = _manifest([_eligible_row("archive/handoffs/eligible.md")])
    stamped = _stamped(manifest)

    log_path = tmp_path / "state" / "distillation-log.md"
    candidate_path = tmp_path / "archive" / "handoffs" / "eligible.md"

    real_run_git = _mod._run_git

    async def failing_add_and_revert_run_git(*args: str, cwd: Path, env: dict[str, str]):
        if args and args[0] == "add" and str(log_path) in args:
            return 1, b"", b"simulated git-add failure for log_path"
        if args and args[0] == "checkout" and str(candidate_path) in args:
            return 1, b"", b"simulated checkout-revert failure"
        return await real_run_git(*args, cwd=cwd, env=env)

    monkeypatch.setattr(_mod, "_run_git", failing_add_and_revert_run_git)

    result = _run(
        apply_disposal_manifest(tmp_path, stamped, harvest_committed_sha=shas["harvest"])
    )
    monkeypatch.setattr(_mod, "_run_git", real_run_git)

    assert result.deleted_tracked == []
    candidate_failure = next(
        f for f in result.failed if f["path"] == "archive/handoffs/eligible.md"
    )
    # Original cause still primary...
    assert "log-stage-failed" in candidate_failure["reason"]
    # ...with the revert failure surfaced as additional context, not silence.
    assert "revert-also-failed" in candidate_failure["reason"]


def test_apply_disposal_manifest_absent_run_raises(tmp_path: Path):
    _init_repo_with_commits(tmp_path)
    with pytest.raises(DisposalStampError, match="not found on disk"):
        load_disposal_manifest(manifest_path_for_run(tmp_path, "no-such-run"))


# ---------------------------------------------------------------------------
# write_apply_receipt
# ---------------------------------------------------------------------------


def test_write_apply_receipt_shape(tmp_path: Path):
    from coordinator_core.ops.distill_apply_disposal import ApplyDisposalResult

    (tmp_path / ".git").mkdir()
    result = ApplyDisposalResult(
        run_id="2026-07-23-01h00",
        deleted_tracked=["archive/handoffs/eligible.md"],
        deleted_untracked=[],
        already_deleted=[],
        newly_blocked=[],
        failed=[],
    )
    path = write_apply_receipt(tmp_path, result, emitted_at="20260723T100000Z")
    assert path.parent == tmp_path / "state" / "ceremony" / "distill-apply-disposal"
    body = json.loads(path.read_text(encoding="utf-8"))
    assert body["schema_version"] == _schema.SCHEMA_VERSION
    assert body["run_id"] == "2026-07-23-01h00"
    assert body["deleted_tracked"] == ["archive/handoffs/eligible.md"]


# ---------------------------------------------------------------------------
# handler
# ---------------------------------------------------------------------------


def test_handler_raises_on_repo_root_none():
    with pytest.raises(ValueError, match="repo_root is None"):
        _run(_handler({"run_id": "x", "harvest_committed_sha": "abc"}, repo_root=None))


def test_handler_raises_on_missing_harvest_sha(tmp_path: Path):
    (tmp_path / ".git").mkdir()
    with pytest.raises(ValueError, match="harvest_committed_sha"):
        _run(_handler({"run_id": "x"}, repo_root=tmp_path / ".git"))


@_requires_rg
def test_handler_end_to_end_dispatch_message_smoke(tmp_path: Path):
    """assemble (C12) -> stamp (C13) -> apply (C14) via the REAL registered
    wiring (ops/__init__.py eager import + _registry_map.py + op_scopes.py +
    the @register_op decorator)."""
    import coordinator_core.ipc as ipc
    import coordinator_core.ops  # noqa: F401 — triggers eager registration

    shas = _init_repo_with_commits(tmp_path)

    assemble_msg = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "distill.assemble_disposal_manifest",
        "params": {
            "run_id": "2026-07-23-02h00",
            "candidates": ["archive/handoffs/eligible.md"],
        },
        "_origin_worktree": str(tmp_path),
    }
    assembled = _run(ipc.dispatch_message(assemble_msg))
    assert "result" in assembled, f"assemble must succeed; got error: {assembled.get('error')}"

    stamp_msg = {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "distill.stamp_disposal",
        "params": {
            "run_id": "2026-07-23-02h00",
            "by": "pm",
            "at": "2026-07-23T10:00:00Z",
            "note": "approved, mass-throttle-ack (single-candidate fixture)",
        },
        "_origin_worktree": str(tmp_path),
    }
    stamped = _run(ipc.dispatch_message(stamp_msg))
    assert "result" in stamped, f"stamp must succeed; got error: {stamped.get('error')}"

    apply_msg = {
        "jsonrpc": "2.0",
        "id": 3,
        "method": "distill.apply_disposal",
        "params": {
            "run_id": "2026-07-23-02h00",
            "harvest_committed_sha": shas["harvest"],
        },
        "_origin_worktree": str(tmp_path),
    }
    applied = _run(ipc.dispatch_message(apply_msg))
    assert "result" in applied, f"apply must succeed; got error: {applied.get('error')}"
    assert applied["result"]["deleted_tracked"] == ["archive/handoffs/eligible.md"]
    assert applied["result"]["receipt_path"].startswith("state/ceremony/distill-apply-disposal/")
    assert not (tmp_path / "archive" / "handoffs" / "eligible.md").exists()

    # Unstamped-manifest refusal: a SECOND run_id, assembled but never stamped.
    assemble_msg2 = dict(assemble_msg)
    assemble_msg2["id"] = 4
    assemble_msg2["params"] = dict(assemble_msg["params"])
    assemble_msg2["params"]["run_id"] = "2026-07-23-03h00"
    assembled2 = _run(ipc.dispatch_message(assemble_msg2))
    assert "result" in assembled2

    apply_msg2 = dict(apply_msg)
    apply_msg2["id"] = 5
    apply_msg2["params"] = dict(apply_msg["params"])
    apply_msg2["params"]["run_id"] = "2026-07-23-03h00"
    unstamped_result = _run(ipc.dispatch_message(apply_msg2))
    assert "error" in unstamped_result
    assert "result" not in unstamped_result


# ---------------------------------------------------------------------------
# Lineage denormalization (2026-07-23 cockpit lineage-severing fix) —
# module docstring § Lineage denormalization.
# ---------------------------------------------------------------------------


def _init_repo_for_denorm(tmp_path: Path) -> dict[str, str]:
    """Same commit shape as `_init_repo_with_commits` (root -> harvest, for
    drain-ordering) but WITHOUT pre-seeding a delete candidate — denorm tests
    author their own parent/child pair on top."""
    _git("init", "-q", cwd=tmp_path)
    _git("config", "user.email", "test@example.com", cwd=tmp_path)
    _git("config", "user.name", "Test", cwd=tmp_path)

    (tmp_path / "README.md").write_text("root\n", encoding="utf-8")
    _git("add", "--", "README.md", cwd=tmp_path)
    _git("commit", "-q", "-m", "root commit", cwd=tmp_path)

    log_path = tmp_path / "state" / "distillation-log.md"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text("## Run seed\n- seed -> EPHEMERAL, seed (run: seed)\n", encoding="utf-8")
    _git("add", "--", "state/distillation-log.md", cwd=tmp_path)
    _git("commit", "-q", "-m", "harvest commit", cwd=tmp_path)
    harvest_sha = _git("rev-parse", "HEAD", cwd=tmp_path)

    (tmp_path / "state" / "cross-repo-commitments").mkdir(parents=True, exist_ok=True)

    return {"harvest": harvest_sha}


def _write_child_handoff(
    path: Path, *, edge_field: str, edge_value: str, handoff_id: str | None = None
) -> None:
    """A delete-guard-eligible archived handoff carrying ONE succession edge
    field (predecessor or origin_handoff) naming its parent.

    `handoff_id`, when given, is what `_successor_ref` (reused from
    migrate_handoff_vocabulary.py) prefers as the `disposed_successors` entry
    value over a bare repo-relative path — tests that pre-seed a parent's
    `disposed_successors` to simulate a PRIOR run use this so the pre-seeded
    value is an opaque id, not the child's own path (a path would make guard
    3's active-reference scan see the child as "still referenced" by its own
    parent and block its delete this run — a real, separate guard
    interaction, not what these fixtures are testing)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    id_line = f"handoff_id: {handoff_id}\n" if handoff_id else ""
    path.write_text(
        "---\n"
        "shipped_in: 68b27420\n"
        "status: consumed\n"
        "deployment_state: shipped\n"
        f"{edge_field}: {edge_value}\n"
        f"{id_line}"
        "realized_by: inline\n"
        "---\n"
        "body\n",
        encoding="utf-8",
    )


def _write_parent_handoff(path: Path, *, extra_fm: str = "") -> None:
    """A minimal surviving (non-candidate) handoff — enough frontmatter shape
    for split_frontmatter/insert_fm_field to operate on."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\ndeployment_state: open\n{extra_fm}---\nparent body\n",
        encoding="utf-8",
    )


@_requires_rg
def test_denorm_predecessor_edge_appends_disposed_successor_before_delete(tmp_path: Path):
    shas = _init_repo_for_denorm(tmp_path)
    parent = tmp_path / "state" / "handoffs" / "parent.md"
    _write_parent_handoff(parent)
    child = tmp_path / "archive" / "handoffs" / "child.md"
    _write_child_handoff(child, edge_field="predecessor", edge_value="state/handoffs/parent.md")
    _git("add", "--", "state/handoffs/parent.md", "archive/handoffs/child.md", cwd=tmp_path)
    _git("commit", "-q", "-m", "add parent+child", cwd=tmp_path)

    manifest = _manifest([_eligible_row("archive/handoffs/child.md")])
    stamped = _stamped(manifest)

    result = _run(
        apply_disposal_manifest(tmp_path, stamped, harvest_committed_sha=shas["harvest"])
    )
    assert result.deleted_tracked == ["archive/handoffs/child.md"]
    assert not child.exists()

    parent_text = parent.read_text(encoding="utf-8")
    assert "disposed_successors: [archive/handoffs/child.md]" in parent_text
    assert "# distill: archive/handoffs/child.md disposed by /distill on" in parent_text

    assert result.denorm_written == [{
        "parent": "state/handoffs/parent.md",
        "child": "archive/handoffs/child.md",
        "successor_ref": "archive/handoffs/child.md",
        "provenance_comment": result.denorm_written[0]["provenance_comment"],
    }]
    assert _git("status", "--porcelain", cwd=tmp_path) == ""


@_requires_rg
def test_denorm_origin_handoff_edge_appends_disposed_successor_before_delete(tmp_path: Path):
    shas = _init_repo_for_denorm(tmp_path)
    parent = tmp_path / "state" / "handoffs" / "origin-parent.md"
    _write_parent_handoff(parent)
    child = tmp_path / "archive" / "handoffs" / "spinoff-child.md"
    _write_child_handoff(
        child, edge_field="origin_handoff", edge_value="state/handoffs/origin-parent.md"
    )
    _git(
        "add", "--", "state/handoffs/origin-parent.md", "archive/handoffs/spinoff-child.md",
        cwd=tmp_path,
    )
    _git("commit", "-q", "-m", "add origin parent+spinoff child", cwd=tmp_path)

    manifest = _manifest([_eligible_row("archive/handoffs/spinoff-child.md")])
    stamped = _stamped(manifest)

    result = _run(
        apply_disposal_manifest(tmp_path, stamped, harvest_committed_sha=shas["harvest"])
    )
    assert result.deleted_tracked == ["archive/handoffs/spinoff-child.md"]
    assert not child.exists()

    parent_text = parent.read_text(encoding="utf-8")
    assert "disposed_successors: [archive/handoffs/spinoff-child.md]" in parent_text
    assert result.denorm_written[0]["parent"] == "state/handoffs/origin-parent.md"


@_requires_rg
def test_denorm_skips_when_parent_also_in_disposal_batch(tmp_path: Path):
    shas = _init_repo_for_denorm(tmp_path)
    parent = tmp_path / "archive" / "handoffs" / "doomed-parent.md"
    _write_child_handoff(parent, edge_field="predecessor", edge_value="none")
    child = tmp_path / "archive" / "handoffs" / "doomed-child.md"
    # Bare-filename predecessor (dag.resolve_target resolves this fine against
    # the child's own handoff_dir) — deliberately NOT the full repo-relative
    # path, so this edge text does not ALSO trip guard 3's active-reference
    # scan against the parent's own candidacy (a full-path predecessor value
    # would make the parent read as "still referenced" and guard-block it
    # outright, which is a different, already-covered guard interaction —
    # this fixture isolates the denorm-specific "parent also in this
    # disposal batch" skip path instead).
    _write_child_handoff(
        child, edge_field="predecessor", edge_value="doomed-parent.md"
    )
    _git(
        "add", "--", "archive/handoffs/doomed-parent.md", "archive/handoffs/doomed-child.md",
        cwd=tmp_path,
    )
    _git("commit", "-q", "-m", "add doomed pair", cwd=tmp_path)

    manifest = _manifest([
        _eligible_row("archive/handoffs/doomed-parent.md"),
        _eligible_row("archive/handoffs/doomed-child.md"),
    ])
    plan = compute_apply_plan(tmp_path, manifest)
    assert plan.denorm_writes == []
    assert any(
        s["parent"] == "archive/handoffs/doomed-parent.md"
        and s["reason"] == "parent-also-in-disposal-batch"
        for s in plan.denorm_skipped
    )

    stamped = _stamped(manifest)
    result = _run(
        apply_disposal_manifest(tmp_path, stamped, harvest_committed_sha=shas["harvest"])
    )
    assert set(result.deleted_tracked) == {
        "archive/handoffs/doomed-parent.md", "archive/handoffs/doomed-child.md",
    }
    assert result.denorm_written == []


@_requires_rg
def test_denorm_idempotent_noop_when_parent_already_has_matching_entry(tmp_path: Path):
    shas = _init_repo_for_denorm(tmp_path)
    parent = tmp_path / "state" / "handoffs" / "already-denormed-parent.md"
    _write_parent_handoff(
        parent,
        extra_fm=(
            "disposed_successors: [hnd-already-denormed-child]\n"
            "# distill: hnd-already-denormed-child disposed by /distill on "
            "2026-07-20 (run prior-run) — survives only in git history from here on\n"
        ),
    )
    child = tmp_path / "archive" / "handoffs" / "already-denormed-child.md"
    _write_child_handoff(
        child, edge_field="predecessor",
        edge_value="state/handoffs/already-denormed-parent.md",
        handoff_id="hnd-already-denormed-child",
    )
    _git(
        "add", "--",
        "state/handoffs/already-denormed-parent.md",
        "archive/handoffs/already-denormed-child.md",
        cwd=tmp_path,
    )
    _git("commit", "-q", "-m", "add pre-denormed pair", cwd=tmp_path)
    pre_parent_text = parent.read_text(encoding="utf-8")

    manifest = _manifest([_eligible_row("archive/handoffs/already-denormed-child.md")])
    plan = compute_apply_plan(tmp_path, manifest)
    assert plan.denorm_writes == []
    assert plan.denorm_skipped == []

    stamped = _stamped(manifest)
    result = _run(
        apply_disposal_manifest(tmp_path, stamped, harvest_committed_sha=shas["harvest"])
    )
    assert result.deleted_tracked == ["archive/handoffs/already-denormed-child.md"]
    assert result.denorm_written == []
    assert parent.read_text(encoding="utf-8") == pre_parent_text
    assert parent.read_text(encoding="utf-8").count("disposed_successors:") == 1


@_requires_rg
def test_denorm_two_disposed_children_of_one_parent_both_get_entries(tmp_path: Path):
    """The case the single-valued continued_into design silently dropped: a
    parent with TWO children disposed in the SAME batch must end up with
    BOTH successor_refs in disposed_successors, not just the first."""
    shas = _init_repo_for_denorm(tmp_path)
    parent = tmp_path / "state" / "handoffs" / "shared-parent.md"
    _write_parent_handoff(parent)
    child_a = tmp_path / "archive" / "handoffs" / "child-a.md"
    _write_child_handoff(
        child_a, edge_field="predecessor", edge_value="state/handoffs/shared-parent.md"
    )
    child_b = tmp_path / "archive" / "handoffs" / "child-b.md"
    _write_child_handoff(
        child_b, edge_field="origin_handoff", edge_value="state/handoffs/shared-parent.md"
    )
    _git(
        "add", "--", "state/handoffs/shared-parent.md",
        "archive/handoffs/child-a.md", "archive/handoffs/child-b.md",
        cwd=tmp_path,
    )
    _git("commit", "-q", "-m", "add shared parent + two children", cwd=tmp_path)

    manifest = _manifest([
        _eligible_row("archive/handoffs/child-a.md"),
        _eligible_row("archive/handoffs/child-b.md"),
    ])
    plan = compute_apply_plan(tmp_path, manifest)
    assert {w["successor_ref"] for w in plan.denorm_writes} == {
        "archive/handoffs/child-a.md", "archive/handoffs/child-b.md",
    }
    assert all(w["parent"] == "state/handoffs/shared-parent.md" for w in plan.denorm_writes)

    stamped = _stamped(manifest)
    result = _run(
        apply_disposal_manifest(tmp_path, stamped, harvest_committed_sha=shas["harvest"])
    )
    assert set(result.deleted_tracked) == {
        "archive/handoffs/child-a.md", "archive/handoffs/child-b.md",
    }
    assert {e["successor_ref"] for e in result.denorm_written} == {
        "archive/handoffs/child-a.md", "archive/handoffs/child-b.md",
    }

    parent_text = parent.read_text(encoding="utf-8")
    assert "disposed_successors: [archive/handoffs/child-a.md, archive/handoffs/child-b.md]" in parent_text
    assert "# distill: archive/handoffs/child-a.md disposed by /distill on" in parent_text
    assert "# distill: archive/handoffs/child-b.md disposed by /distill on" in parent_text
    assert _git("status", "--porcelain", cwd=tmp_path) == ""


@_requires_rg
def test_denorm_partial_batch_failure_only_succeeded_child_gets_entry(tmp_path: Path):
    """Two children of one parent, tracked; one child's git rm fails (it
    carries an uncommitted local edit, so plain `git rm` — never `git rm -f`,
    per module docstring — correctly refuses to remove it) while the other
    succeeds. The parent's disposed_successors must contain ONLY the
    succeeded child's entry — the failed child must never be recorded as
    disposed while it is still present and tracked on disk (2026-07-23 code
    review finding: denorm writes were previously derived from the
    pre-delete plan, not actual per-child delete outcomes)."""
    shas = _init_repo_for_denorm(tmp_path)
    parent = tmp_path / "state" / "handoffs" / "shared-parent.md"
    _write_parent_handoff(parent)
    child_a = tmp_path / "archive" / "handoffs" / "child-a.md"
    _write_child_handoff(
        child_a, edge_field="predecessor", edge_value="state/handoffs/shared-parent.md"
    )
    child_b = tmp_path / "archive" / "handoffs" / "child-b.md"
    _write_child_handoff(
        child_b, edge_field="origin_handoff", edge_value="state/handoffs/shared-parent.md"
    )
    _git(
        "add", "--", "state/handoffs/shared-parent.md",
        "archive/handoffs/child-a.md", "archive/handoffs/child-b.md",
        cwd=tmp_path,
    )
    _git("commit", "-q", "-m", "add shared parent + two children", cwd=tmp_path)

    # child_a now carries an uncommitted local edit — the real-world failure
    # mode named in the finding.
    with child_a.open("a", encoding="utf-8") as f:
        f.write("uncommitted local edit\n")

    manifest = _manifest([
        _eligible_row("archive/handoffs/child-a.md"),
        _eligible_row("archive/handoffs/child-b.md"),
    ])
    stamped = _stamped(manifest)

    result = _run(
        apply_disposal_manifest(tmp_path, stamped, harvest_committed_sha=shas["harvest"])
    )

    assert result.deleted_tracked == ["archive/handoffs/child-b.md"]
    assert child_a.exists()
    assert not child_b.exists()
    assert any(f["path"] == "archive/handoffs/child-a.md" for f in result.failed)

    parent_text = parent.read_text(encoding="utf-8")
    assert "disposed_successors: [archive/handoffs/child-b.md]" in parent_text
    assert "archive/handoffs/child-a.md" not in parent_text

    assert result.denorm_written == [{
        "parent": "state/handoffs/shared-parent.md",
        "child": "archive/handoffs/child-b.md",
        "successor_ref": "archive/handoffs/child-b.md",
        "provenance_comment": result.denorm_written[0]["provenance_comment"],
    }]


@_requires_rg
def test_denorm_untracked_child_unlink_failure_writes_no_denorm_entry(
    tmp_path: Path, monkeypatch
):
    """An untracked child whose `unlink()` raises must produce NO denorm
    entry on its parent — the write is a direct synchronous
    `Path.write_text` with no commit gate, so writing it BEFORE the unlink
    outcome is known leaves a false "survives only in git history" claim on
    disk with no revert path (2026-07-23 code review finding)."""
    shas = _init_repo_for_denorm(tmp_path)
    parent = tmp_path / "state" / "handoffs" / "untracked-parent.md"
    _write_parent_handoff(parent)
    _git("add", "--", "state/handoffs/untracked-parent.md", cwd=tmp_path)
    _git("commit", "-q", "-m", "add parent", cwd=tmp_path)
    pre_parent_text = parent.read_text(encoding="utf-8")

    child = tmp_path / "archive" / "handoffs" / "untracked-child.md"
    _write_child_handoff(
        child, edge_field="predecessor", edge_value="state/handoffs/untracked-parent.md"
    )
    # Deliberately NOT git-added — stays untracked.

    real_unlink = Path.unlink

    def failing_unlink(self, *args, **kwargs):
        if self == child:
            raise OSError("simulated unlink failure")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", failing_unlink)

    manifest = _manifest([_eligible_row("archive/handoffs/untracked-child.md")])
    stamped = _stamped(manifest)

    result = _run(
        apply_disposal_manifest(tmp_path, stamped, harvest_committed_sha=shas["harvest"])
    )

    assert result.deleted_untracked == []
    assert child.exists()
    assert any(
        f["path"] == "archive/handoffs/untracked-child.md" and "unlink-failed" in f["reason"]
        for f in result.failed
    )
    assert result.denorm_written == []
    assert parent.read_text(encoding="utf-8") == pre_parent_text


@_requires_rg
def test_denorm_compute_apply_plan_alone_writes_nothing(tmp_path: Path):
    """Dry-run must stay write-free: compute_apply_plan (never
    apply_disposal_manifest) must not touch the parent or child files on
    disk, even though it computes a non-empty denorm_writes plan."""
    _init_repo_for_denorm(tmp_path)
    parent = tmp_path / "state" / "handoffs" / "untouched-parent.md"
    _write_parent_handoff(parent)
    child = tmp_path / "archive" / "handoffs" / "untouched-child.md"
    _write_child_handoff(
        child, edge_field="predecessor", edge_value="state/handoffs/untouched-parent.md"
    )
    _git(
        "add", "--", "state/handoffs/untouched-parent.md", "archive/handoffs/untouched-child.md",
        cwd=tmp_path,
    )
    _git("commit", "-q", "-m", "add untouched pair", cwd=tmp_path)
    pre_parent_text = parent.read_text(encoding="utf-8")
    pre_child_text = child.read_text(encoding="utf-8")

    manifest = _manifest([_eligible_row("archive/handoffs/untouched-child.md")])
    plan = compute_apply_plan(tmp_path, manifest)

    assert len(plan.denorm_writes) == 1
    assert plan.denorm_writes[0]["parent"] == "state/handoffs/untouched-parent.md"
    assert parent.read_text(encoding="utf-8") == pre_parent_text
    assert child.read_text(encoding="utf-8") == pre_child_text
    assert _git("status", "--porcelain", cwd=tmp_path) == ""


# ---------------------------------------------------------------------------
# Main-index resync: acquisition count + lock-retry (AC-7 / AC-8,
# docs/plans/2026-08-13-commit-seams-inherit-lock-reap-and-retry.md C6)
#
# Every OTHER git call `apply_disposal_manifest`/`_delete_tracked_and_
# append_log` makes (read-tree, rm, add-for-denorm, add-for-log, commit)
# runs against a PRIVATE `GIT_INDEX_FILE`, so none of them ever takes the
# shared `.git/index.lock` -- the main-index resync below is the ONLY step
# in this whole module that ever touches it, and the two tests here pin
# both halves of C6's fix at that single site.
# ---------------------------------------------------------------------------


@_requires_rg
def test_main_index_resync_collapses_to_one_call_not_per_path(tmp_path: Path, monkeypatch):
    """AC-7 — counted demonstration: the main-index resync after a
    multi-survivor tracked delete must cost ONE `git update-index`
    subprocess call, not one per reaped/denorm/log path."""
    shas = _init_repo_with_commits(tmp_path)
    for name in ("second", "third"):
        candidate = tmp_path / "archive" / "handoffs" / f"{name}.md"
        candidate.write_text(
            "---\nshipped_in: 68b27420\nstatus: consumed\ndeployment_state: shipped\n"
            "realized_by: inline\n---\nbody\n",
            encoding="utf-8",
        )
        _git("add", "--", f"archive/handoffs/{name}.md", cwd=tmp_path)
    _git("commit", "-q", "-m", "add two more candidates", cwd=tmp_path)

    manifest = _manifest(
        [
            _eligible_row("archive/handoffs/eligible.md"),
            _eligible_row("archive/handoffs/second.md"),
            _eligible_row("archive/handoffs/third.md"),
        ]
    )
    stamped = _stamped(manifest)

    real_exec = asyncio.create_subprocess_exec
    update_index_calls: list[tuple] = []

    async def _counting_exec(*args, **kwargs):
        if "update-index" in args:
            update_index_calls.append(args)
        return await real_exec(*args, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _counting_exec)

    result = _run(
        apply_disposal_manifest(tmp_path, stamped, harvest_committed_sha=shas["harvest"])
    )

    assert sorted(result.deleted_tracked) == sorted(
        [
            "archive/handoffs/eligible.md",
            "archive/handoffs/second.md",
            "archive/handoffs/third.md",
        ]
    )
    assert result.failed == []
    assert len(update_index_calls) == 1, (
        f"expected ONE batched `git update-index`, got {len(update_index_calls)}: "
        f"{update_index_calls}"
    )
    for name in ("eligible.md", "second.md", "third.md"):
        assert any(name in str(arg) for arg in update_index_calls[0])


@_requires_rg
def test_main_index_resync_retries_through_lock_contention(tmp_path: Path, monkeypatch):
    """AC-8 — retry composed locally via
    `coordinator_core.ops.fleet._common._update_index_with_retry` (see this
    module's in-code note, above the resync call site, for the rejected
    `git_native` route): a transient `.git/index.lock` collision on the
    main-index resync is retried and actually SUCCEEDS, rather than
    burning its only attempt and leaving the main index stale."""
    shas = _init_repo_with_commits(tmp_path)
    manifest = _manifest([_eligible_row("archive/handoffs/eligible.md")])
    stamped = _stamped(manifest)

    real_exec = asyncio.create_subprocess_exec
    attempts = {"update-index": 0}

    class _FakeLockedProc:
        returncode = 128

        async def communicate(self):
            return b"", b"fatal: Unable to create '.../.git/index.lock': File exists."

    async def _flaky_exec(*args, **kwargs):
        if "update-index" in args:
            attempts["update-index"] += 1
            if attempts["update-index"] < 3:
                return _FakeLockedProc()
        return await real_exec(*args, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _flaky_exec)

    result = _run(
        apply_disposal_manifest(tmp_path, stamped, harvest_committed_sha=shas["harvest"])
    )

    assert result.deleted_tracked == ["archive/handoffs/eligible.md"]
    assert result.failed == []
    assert attempts["update-index"] == 3, (
        "expected exactly 2 lock-contention retries before success"
    )
    # Main index actually converged -- a clean status proves the resync
    # eventually SUCCEEDED, not merely that its non-fatal failure was
    # swallowed.
    assert _git("status", "--porcelain", cwd=tmp_path) == ""


@_requires_rg
def test_main_index_resync_persistent_failure_log_is_honest_about_scope(
    tmp_path: Path, monkeypatch, caplog
):
    """Review: code-reviewer P2 (2026-08-13) — on a persistent (retry-
    exhausted) resync failure, the error log must NOT claim every batched
    path is confirmed affected (git update-index applies positionally and
    can partially succeed before failing on a later path). The message must
    (a) frame the path list as the attempted batch, not a confirmed-dirty
    list, and (b) surface git's own stderr."""
    import logging

    shas = _init_repo_with_commits(tmp_path)
    manifest = _manifest([_eligible_row("archive/handoffs/eligible.md")])
    stamped = _stamped(manifest)

    real_exec = asyncio.create_subprocess_exec

    class _FakeLockedProc:
        returncode = 128

        async def communicate(self):
            return (
                b"",
                b"fatal: Unable to create '.../.git/index.lock': File exists.",
            )

    async def _always_locked_exec(*args, **kwargs):
        if "update-index" in args:
            return _FakeLockedProc()
        return await real_exec(*args, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _always_locked_exec)

    with caplog.at_level(logging.ERROR, logger="coordinator_core.ops.distill_apply_disposal"):
        result = _run(
            apply_disposal_manifest(tmp_path, stamped, harvest_committed_sha=shas["harvest"])
        )

    # The tracked delete + commit itself is unaffected -- the resync failure
    # is non-fatal by design (mirrors rm_and_commit's posture).
    assert result.deleted_tracked == ["archive/handoffs/eligible.md"]

    resync_records = [
        r for r in caplog.records if "main-index resync FAILED" in r.getMessage()
    ]
    assert len(resync_records) == 1
    message = resync_records[0].getMessage()
    # Honest framing: the path list is the attempted batch, not confirmed-dirty.
    assert "not all necessarily affected" in message
    assert "positionally" in message
    # git's own stderr is surfaced, not swallowed.
    assert "index.lock" in message

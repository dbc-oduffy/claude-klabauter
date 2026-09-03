"""
coordinator_core.ops.fleet.tests.test_archive_sizings

C3 of docs/plans/2026-08-13-terminal-sizings-boot-sweep-family.md. Tests
the terminal-sizings archival family (coordinator_core.ops.fleet.
archive_sizings) — AC2-AC7 — on the non-spawning git-free seam
(archive_git_free_seam.py), the LANDED C1 non-spawning fixture. Never a
real `git init`/subprocess — see that module's docstring for why (the
2026-08-07 incident, commit 1d4e686a9).

Coverage map:
  - terminal record moves; non-terminal record untouched (AC2/AC3)
  - malformed record surfaced, never flipped/silently dropped (AC3)
  - AC3 never-infer: file bytes unchanged for records that stay put
  - AC4 collision matrix: source-gone -> already-archived; byte-identical
    dst -> converges (force-move); differing dst -> _REASON_DEST_CONFLICT,
    dst untouched
  - AC5 worktree-dirty retention gate: a dirty survivor is retained (never
    moved), a clean survivor still moves, T1 preview is unaffected (stays
    status-only)
  - AC6 forward-pointer refusal gate: non-terminal plan FK refuses in
    place; null/absent FK proceeds; terminal plan FK proceeds. Family
    never writes to the plan file.
  - AC7 tripwire row appended to test_dest_conflict_reason_tripwires.py
    (this file only supplies the coverage; the tripwire assertion itself
    lives in that file per plan instruction — do not start a parallel one)

Spec backlinks:
  - Plan (C3): docs/plans/2026-08-13-terminal-sizings-boot-sweep-family.md
  - Plan (AC5): docs/plans/2026-09-03-close-verb-archival-stops-asking-for-wri.md
  - archive_git_free_seam.py: the non-spawning fixture this module drives
  - test_archive_git_free_seam_smoke.py: the worked example this module follows

Negative-spec:
  - Does NOT run a real `git init` or any subprocess anywhere in this file.
  - Does NOT assert anything about archive_and_commit's own git mechanics —
    that stays behind the patched mover seam per archive_git_free_seam's
    documented discriminator.
  - Does NOT spawn a real `git status` either — `_dirty_sizing_relpaths`
    delegates to `coordinator_core.ops.ceremony.git_native.
    dirty_relpaths_from_porcelain`, whose own `status_porcelain` call is
    patched (on the `git_native` module itself, not on `archive_sizings`)
    by `_act` below the same way `archive_git_free_seam.
    patched_disposition_seam` patches the mover, since `archive_git_free_
    seam.py` is a fixed shared fixture out of this chunk's scope and does
    not itself know about this module's newest git call site.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from coordinator_core.ops.ceremony import git_native
from coordinator_core.ops.ceremony.git_native import GitResult
from coordinator_core.ops.fleet import archive_sizings
from coordinator_core.ops.fleet._common import _REASON_DEST_CONFLICT
from coordinator_core.ops.fleet.tests.archive_git_free_seam import (
    make_recording_mover,
    patched_disposition_seam,
    run,
)

_TERMINAL_BODY = "---\nstatus: shipped\ntitle: {title}\n---\n"
_NON_TERMINAL_BODY = "---\nstatus: draft\ntitle: {title}\n---\n"

#: Default fake `status_porcelain` reply for `_act` below: a clean tree
#: (no output, rc=0) — never a real `git status` subprocess. See this
#: module's own Negative-spec.
_CLEAN_STATUS = GitResult(returncode=0, stdout="", stderr="")


def _write_sizing(worktree: Path, name: str, body: str) -> Path:
    sizings_dir = worktree / "state" / "sizings"
    sizings_dir.mkdir(parents=True, exist_ok=True)
    path = sizings_dir / name
    path.write_text(body, encoding="utf-8")
    return path


def _write_plan(worktree: Path, rel: str, status: str) -> Path:
    plan_path = worktree / rel
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path.write_text(f"---\nstatus: {status}\ntitle: a plan\n---\n", encoding="utf-8")
    return plan_path


def _act(worktree: Path, candidate_ids, *, mover=None, status_result: GitResult = _CLEAN_STATUS):
    """`status_result` defaults to a clean tree so AC5's dirty gate never
    fires for the pre-existing (AC2-AC7) test population above — pass a
    dirty `GitResult` to exercise AC5's own retention path.
    """
    with patched_disposition_seam(archive_sizings, worktree=worktree, mover=mover) as m, \
            patch.object(git_native, "status_porcelain", lambda cwd, paths=None: status_result):
        result = run(archive_sizings._archive_terminal_sizings(
            {"mode": "already-terminal", "dry_run": False, "candidate_ids": candidate_ids},
            repo_root=str(worktree),
        ))
    return result, m


def _preview(worktree: Path):
    with patched_disposition_seam(archive_sizings, worktree=worktree) as m:
        result = run(archive_sizings._archive_terminal_sizings(
            {"mode": "already-terminal", "dry_run": True, "candidate_ids": None},
            repo_root=str(worktree),
        ))
    return result, m


# ---------------------------------------------------------------------------
# Terminal moves / non-terminal untouched / malformed surfaced
# ---------------------------------------------------------------------------


def test_terminal_record_moves_to_archive_yyyy_mm_from_filename_date(tmp_path: Path) -> None:
    worktree = tmp_path / "repo"
    # Filename date deliberately NOT today's month (today is 2026-08-13).
    cid = "state/sizings/2026-01-15-old-terminal.yaml"
    _write_sizing(worktree, "2026-01-15-old-terminal.yaml", _TERMINAL_BODY.format(title="x"))

    result, mover = _act(worktree, [cid])

    assert result["acted"] == [{"id": cid, "archived": True}]
    assert result["skipped"] == []
    assert result["failed"] == []
    assert mover.captured is not None
    assert len(mover.captured) == 1
    move = mover.captured[0]
    assert move.candidate_id == cid
    assert move.dst == worktree / "archive" / "sizings" / "2026-01" / "2026-01-15-old-terminal.yaml"


def test_non_terminal_record_untouched_and_not_a_candidate(tmp_path: Path) -> None:
    worktree = tmp_path / "repo"
    cid = "state/sizings/2026-01-15-draft.yaml"
    src = _write_sizing(worktree, "2026-01-15-draft.yaml", _NON_TERMINAL_BODY.format(title="x"))
    original_bytes = src.read_bytes()

    preview_result, _ = _preview(worktree)
    assert preview_result["candidates"] == []

    act_result, mover = _act(worktree, [cid])
    # Not terminal -> not archived; source not moved.
    assert act_result["acted"] == []
    assert mover.captured is None
    assert src.exists()
    assert src.read_bytes() == original_bytes


def test_malformed_record_surfaced_not_flipped_not_silently_skipped(tmp_path: Path) -> None:
    worktree = tmp_path / "repo"
    cid = "state/sizings/2026-01-15-malformed.yaml"
    # No status field at all / unparseable-ish.
    src = _write_sizing(worktree, "2026-01-15-malformed.yaml", "not: yaml: frontmatter: at: all:\n")
    original_bytes = src.read_bytes()

    preview_result, _ = _preview(worktree)
    assert preview_result["candidates"] == []

    act_result, mover = _act(worktree, [cid])
    assert act_result["acted"] == []
    assert mover.captured is None
    # Surfaced, not silently dropped — a re-verify skip reason is present.
    assert len(act_result["skipped"]) == 1
    assert act_result["skipped"][0]["id"] == cid
    assert "terminality-drift" in act_result["skipped"][0]["reason"]
    # Never flipped.
    assert src.exists()
    assert src.read_bytes() == original_bytes


# ---------------------------------------------------------------------------
# AC3 never-infer: bytes unchanged for records that stay put
# ---------------------------------------------------------------------------


def test_ac3_never_infer_frontmatter_untouched_for_staying_records(tmp_path: Path) -> None:
    worktree = tmp_path / "repo"
    draft = _write_sizing(worktree, "2026-01-15-stays-draft.yaml", _NON_TERMINAL_BODY.format(title="x"))
    draft_bytes = draft.read_bytes()

    malformed = _write_sizing(worktree, "2026-01-16-stays-malformed.yaml", "@@@ not yaml\n")
    malformed_bytes = malformed.read_bytes()

    cids = [
        "state/sizings/2026-01-15-stays-draft.yaml",
        "state/sizings/2026-01-16-stays-malformed.yaml",
    ]
    result, mover = _act(worktree, cids)

    assert result["acted"] == []
    assert mover.captured is None
    assert draft.read_bytes() == draft_bytes
    assert malformed.read_bytes() == malformed_bytes


# ---------------------------------------------------------------------------
# AC4 collision matrix
# ---------------------------------------------------------------------------


def test_ac4_source_gone_is_already_archived(tmp_path: Path) -> None:
    worktree = tmp_path / "repo"
    cid = "state/sizings/2026-01-15-gone.yaml"
    # Never write the source — source is already gone.
    (worktree / "state" / "sizings").mkdir(parents=True, exist_ok=True)

    result, mover = _act(worktree, [cid])

    assert result["skipped"] == [{"id": cid, "reason": "already-archived"}]
    assert result["acted"] == []
    assert mover.captured is None


def test_ac4_byte_identical_dst_converges_via_force_move(tmp_path: Path) -> None:
    worktree = tmp_path / "repo"
    cid = "state/sizings/2026-01-15-dup.yaml"
    body = _TERMINAL_BODY.format(title="dup")
    src = _write_sizing(worktree, "2026-01-15-dup.yaml", body)

    dst_dir = worktree / "archive" / "sizings" / "2026-01"
    dst_dir.mkdir(parents=True)
    dst = dst_dir / "2026-01-15-dup.yaml"
    dst.write_text(body, encoding="utf-8")

    result, mover = _act(worktree, [cid])

    assert result["skipped"] == []
    assert result["acted"] == [{"id": cid, "archived": True}]
    assert mover.captured is not None
    assert len(mover.captured) == 1
    move = mover.captured[0]
    assert move.candidate_id == cid
    assert move.force is True
    assert src.exists()  # the seam's mover is a recording fake; it never actually moves.
    assert dst.read_text(encoding="utf-8") == body


def test_ac4_differing_dst_skips_with_dest_conflict_reason_never_clobbered(tmp_path: Path) -> None:
    worktree = tmp_path / "repo"
    cid = "state/sizings/2026-01-15-conflict.yaml"
    src = _write_sizing(worktree, "2026-01-15-conflict.yaml", _TERMINAL_BODY.format(title="mine"))

    dst_dir = worktree / "archive" / "sizings" / "2026-01"
    dst_dir.mkdir(parents=True)
    dst = dst_dir / "2026-01-15-conflict.yaml"
    dst_body = "status: shipped\ntitle: a DIFFERENT archived copy\n"
    dst.write_text(dst_body, encoding="utf-8")

    result, mover = _act(worktree, [cid])

    assert result["skipped"] == [{"id": cid, "reason": _REASON_DEST_CONFLICT}]
    assert result["skipped"][0]["reason"] != "already-archived"
    assert result["acted"] == []
    assert mover.captured is None
    assert src.exists()
    assert dst.read_text(encoding="utf-8") == dst_body  # never clobbered


# ---------------------------------------------------------------------------
# AC6 forward-pointer refusal gate
# ---------------------------------------------------------------------------


def test_ac6_non_terminal_plan_fk_refuses_in_place(tmp_path: Path) -> None:
    worktree = tmp_path / "repo"
    cid = "state/sizings/2026-01-15-fk-live-plan.yaml"
    src = _write_sizing(
        worktree, "2026-01-15-fk-live-plan.yaml",
        "---\nstatus: shipped\ntitle: x\nplan: docs/plans/a-live-plan.md\n---\n",
    )
    plan_path = _write_plan(worktree, "docs/plans/a-live-plan.md", status="draft")
    plan_bytes = plan_path.read_bytes()

    preview_result, _ = _preview(worktree)
    assert preview_result["candidates"] == []

    act_result, mover = _act(worktree, [cid])
    assert act_result["acted"] == []
    assert mover.captured is None
    assert len(act_result["skipped"]) == 1
    assert act_result["skipped"][0]["id"] == cid
    assert "forward-plan-not-terminal" in act_result["skipped"][0]["reason"]

    # Family never writes to the plan file, and never moves the sizing.
    assert plan_path.read_bytes() == plan_bytes
    assert src.exists()


def test_ac6_null_plan_fk_proceeds(tmp_path: Path) -> None:
    worktree = tmp_path / "repo"
    cid = "state/sizings/2026-01-15-fk-null.yaml"
    _write_sizing(worktree, "2026-01-15-fk-null.yaml", _TERMINAL_BODY.format(title="x"))

    result, mover = _act(worktree, [cid])

    assert result["acted"] == [{"id": cid, "archived": True}]
    assert result["skipped"] == []
    assert mover.captured is not None


def test_ac6_terminal_plan_fk_proceeds(tmp_path: Path) -> None:
    worktree = tmp_path / "repo"
    cid = "state/sizings/2026-01-15-fk-terminal-plan.yaml"
    src = _write_sizing(
        worktree, "2026-01-15-fk-terminal-plan.yaml",
        "---\nstatus: shipped\ntitle: x\nplan: docs/plans/a-done-plan.md\n---\n",
    )
    plan_path = _write_plan(worktree, "docs/plans/a-done-plan.md", status="implemented")
    plan_bytes = plan_path.read_bytes()

    result, mover = _act(worktree, [cid])

    assert result["acted"] == [{"id": cid, "archived": True}]
    assert result["skipped"] == []
    assert mover.captured is not None
    assert plan_path.read_bytes() == plan_bytes
    assert src.exists()  # the seam's mover is a recording fake; it never actually moves.


def test_ac6_dry_run_preview_excludes_forward_pointer_refused_candidate(tmp_path: Path) -> None:
    worktree = tmp_path / "repo"
    _write_sizing(
        worktree, "2026-01-15-fk-live-plan-preview.yaml",
        "---\nstatus: shipped\ntitle: x\nplan: docs/plans/a-live-plan-2.md\n---\n",
    )
    _write_plan(worktree, "docs/plans/a-live-plan-2.md", status="sized")

    # A second, unconstrained terminal sizing should still surface.
    _write_sizing(worktree, "2026-01-15-plain.yaml", _TERMINAL_BODY.format(title="y"))

    preview_result, _ = _preview(worktree)
    ids = {c["id"] for c in preview_result["candidates"]}
    assert ids == {"state/sizings/2026-01-15-plain.yaml"}


# ---------------------------------------------------------------------------
# AC5 worktree-dirty retention gate
# ---------------------------------------------------------------------------


def test_ac5_dirty_candidate_retained_not_moved(tmp_path: Path) -> None:
    worktree = tmp_path / "repo"
    cid = "state/sizings/2026-01-15-dirty.yaml"
    src = _write_sizing(worktree, "2026-01-15-dirty.yaml", _TERMINAL_BODY.format(title="dirty"))
    original_bytes = src.read_bytes()

    dirty_status = GitResult(
        returncode=0, stdout=f" M {cid}\n", stderr="",
    )
    result, mover = _act(worktree, [cid], status_result=dirty_status)

    assert result["acted"] == []
    assert result["skipped"] == [{"id": cid, "reason": archive_sizings._REASON_WORKTREE_DIRTY}]
    assert mover.captured is None
    # Retained, not flipped.
    assert src.exists()
    assert src.read_bytes() == original_bytes


def test_ac5_clean_candidate_still_moves(tmp_path: Path) -> None:
    worktree = tmp_path / "repo"
    cid = "state/sizings/2026-01-15-clean.yaml"
    _write_sizing(worktree, "2026-01-15-clean.yaml", _TERMINAL_BODY.format(title="clean"))

    result, mover = _act(worktree, [cid])

    assert result["acted"] == [{"id": cid, "archived": True}]
    assert result["skipped"] == []
    assert mover.captured is not None


def test_ac5_dirty_status_call_failure_fails_closed_retains_candidate(tmp_path: Path) -> None:
    worktree = tmp_path / "repo"
    cid = "state/sizings/2026-01-15-status-failed.yaml"
    src = _write_sizing(
        worktree, "2026-01-15-status-failed.yaml", _TERMINAL_BODY.format(title="x"),
    )
    original_bytes = src.read_bytes()

    failed_status = GitResult(returncode=128, stdout="", stderr="fatal: not a git repository")
    result, mover = _act(worktree, [cid], status_result=failed_status)

    assert result["acted"] == []
    assert result["skipped"] == [{"id": cid, "reason": archive_sizings._REASON_WORKTREE_DIRTY}]
    assert mover.captured is None
    assert src.read_bytes() == original_bytes


def test_ac5_rename_record_dirties_both_sides(tmp_path: Path) -> None:
    """A porcelain `R  old -> new` record must exclude the candidate
    whichever side of the rename it names — the branch this gate's shared
    tail (`git_native.dirty_relpaths_from_porcelain`) inherited verbatim
    from `archive_terminal_handoffs.py`'s own rename handling, previously
    exercised nowhere in this module's own suite."""
    worktree = tmp_path / "repo"
    cid = "state/sizings/2026-01-15-renamed.yaml"
    src = _write_sizing(worktree, "2026-01-15-renamed.yaml", _TERMINAL_BODY.format(title="renamed"))
    original_bytes = src.read_bytes()

    rename_status = GitResult(
        returncode=0,
        stdout=f"R  state/sizings/2026-01-15-old-name.yaml -> {cid}\n",
        stderr="",
    )
    result, mover = _act(worktree, [cid], status_result=rename_status)

    assert result["acted"] == []
    assert result["skipped"] == [{"id": cid, "reason": archive_sizings._REASON_WORKTREE_DIRTY}]
    assert mover.captured is None
    assert src.exists()
    assert src.read_bytes() == original_bytes


def test_ac5_mixed_batch_only_dirty_candidate_retained(tmp_path: Path) -> None:
    """Two candidates in one act call, one dirty and one clean — only the
    dirty one is retained; the clean one still moves. Every existing AC5
    test passes exactly one candidate_id, so a divergence that swapped
    which side of the dirty set got excluded would have gone uncaught."""
    worktree = tmp_path / "repo"
    dirty_cid = "state/sizings/2026-01-15-mix-dirty.yaml"
    clean_cid = "state/sizings/2026-01-15-mix-clean.yaml"
    dirty_src = _write_sizing(worktree, "2026-01-15-mix-dirty.yaml", _TERMINAL_BODY.format(title="dirty"))
    original_bytes = dirty_src.read_bytes()
    _write_sizing(worktree, "2026-01-15-mix-clean.yaml", _TERMINAL_BODY.format(title="clean"))

    mixed_status = GitResult(returncode=0, stdout=f" M {dirty_cid}\n", stderr="")
    result, mover = _act(worktree, [dirty_cid, clean_cid], status_result=mixed_status)

    assert result["acted"] == [{"id": clean_cid, "archived": True}]
    assert result["skipped"] == [{"id": dirty_cid, "reason": archive_sizings._REASON_WORKTREE_DIRTY}]
    assert dirty_src.exists()
    assert dirty_src.read_bytes() == original_bytes


def test_ac5_preview_unaffected_by_dirty_gate_stays_status_only(tmp_path: Path) -> None:
    """T1 preview never runs the dirty check at all — this test does not
    even patch `status_porcelain`, so a preview call that DID reach it would
    spawn a real `git status` against a non-repo tmp_path and fail loudly
    (or, worse, silently fail-open on an unhandled exception) rather than
    quietly passing. A passing preview here is itself the assertion that
    T1 never touches the gate.
    """
    worktree = tmp_path / "repo"
    _write_sizing(worktree, "2026-01-15-preview-only.yaml", _TERMINAL_BODY.format(title="x"))

    preview_result, _ = _preview(worktree)

    ids = {c["id"] for c in preview_result["candidates"]}
    assert ids == {"state/sizings/2026-01-15-preview-only.yaml"}

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
  - AC6 forward-pointer refusal gate: non-terminal plan FK refuses in
    place; null/absent FK proceeds; terminal plan FK proceeds. Family
    never writes to the plan file.
  - AC7 tripwire row appended to test_dest_conflict_reason_tripwires.py
    (this file only supplies the coverage; the tripwire assertion itself
    lives in that file per plan instruction — do not start a parallel one)

Spec backlinks:
  - Plan (C3): docs/plans/2026-08-13-terminal-sizings-boot-sweep-family.md
  - archive_git_free_seam.py: the non-spawning fixture this module drives
  - test_archive_git_free_seam_smoke.py: the worked example this module follows

Negative-spec:
  - Does NOT run a real `git init` or any subprocess anywhere in this file.
  - Does NOT assert anything about archive_and_commit's own git mechanics —
    that stays behind the patched mover seam per archive_git_free_seam's
    documented discriminator.
"""

from __future__ import annotations

from pathlib import Path

from coordinator_core.ops.fleet import archive_sizings
from coordinator_core.ops.fleet._common import _REASON_DEST_CONFLICT
from coordinator_core.ops.fleet.tests.archive_git_free_seam import (
    make_recording_mover,
    patched_disposition_seam,
    run,
)

_TERMINAL_BODY = "---\nstatus: shipped\ntitle: {title}\n---\n"
_NON_TERMINAL_BODY = "---\nstatus: draft\ntitle: {title}\n---\n"


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


def _act(worktree: Path, candidate_ids, *, mover=None):
    with patched_disposition_seam(archive_sizings, worktree=worktree, mover=mover) as m:
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

"""
coordinator_core.ops.fleet.tests.test_archive_sizings_xl_exit_roadmap

Item 6 (docs/plans/2026-09-26-inbox-blitz-claude-klabauter-fixes-fyi-rest.md, row R06):
a roadmap-exit sizing (`route: roadmap`, or `route: pm-decision` with
`xl_exit: roadmap`) is terminal only once every stub/plan that cites it via
`sizing_object:` is itself terminal — not the moment the first one ships.

Follows test_archive_sizings.py's own git-free seam discipline: never a real
`git init`/subprocess, `patched_disposition_seam` + the recording mover fake
stand in for `archive_and_commit`.
"""

from __future__ import annotations

from pathlib import Path

from unittest.mock import patch

from coordinator_core.ops.ceremony import git_native
from coordinator_core.ops.ceremony.git_native import GitResult
from coordinator_core.ops.fleet import archive_sizings
from coordinator_core.ops.fleet.tests.archive_git_free_seam import (
    patched_disposition_seam,
    run,
)

_TERMINAL_BODY = "---\nstatus: shipped\ntitle: {title}\n---\n"

#: Fake `status_porcelain` reply for `_act` below: a clean tree (no output,
#: rc=0) — never a real `git status` subprocess. Mirrors
#: test_archive_sizings.py's own `_CLEAN_STATUS` / `_act` discipline (see
#: this module's own Negative-spec analogue: no real git call in this file).
_CLEAN_STATUS = GitResult(returncode=0, stdout="", stderr="")


def _write_sizing(worktree: Path, name: str, body: str) -> Path:
    sizings_dir = worktree / "state" / "sizings"
    sizings_dir.mkdir(parents=True, exist_ok=True)
    path = sizings_dir / name
    path.write_text(body, encoding="utf-8")
    return path


def _write_plan(worktree: Path, rel: str, status: str, sizing_object: str) -> Path:
    plan_path = worktree / rel
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path.write_text(
        f"---\nstatus: {status}\ntitle: a plan\nsizing_object: {sizing_object}\n---\n",
        encoding="utf-8",
    )
    return plan_path


def _write_handoff(worktree: Path, rel: str, deployment_state: str, sizing_object: str) -> Path:
    handoff_path = worktree / rel
    handoff_path.parent.mkdir(parents=True, exist_ok=True)
    handoff_path.write_text(
        "---\n"
        "status: claimed\n"
        "title: a stub\n"
        f"deployment_state: {deployment_state}\n"
        f"sizing_object: {sizing_object}\n"
        "---\n",
        encoding="utf-8",
    )
    return handoff_path


def _act(worktree: Path, candidate_ids, *, status_result: GitResult = _CLEAN_STATUS):
    with patched_disposition_seam(archive_sizings, worktree=worktree) as m, \
            patch.object(git_native, "status_porcelain", lambda cwd, paths=None, **_kw: status_result):
        result = run(archive_sizings._archive_terminal_sizings(
            {"mode": "already-terminal", "dry_run": False, "candidate_ids": candidate_ids},
            repo_root=str(worktree),
        ))
    return result, m


def _preview(worktree: Path, scan_skipped=None):
    with patched_disposition_seam(archive_sizings, worktree=worktree) as m:
        result = run(archive_sizings._archive_terminal_sizings(
            {"mode": "already-terminal", "dry_run": True, "candidate_ids": None},
            repo_root=str(worktree),
            scan_skipped=scan_skipped,
        ))
    return result, m


# ---------------------------------------------------------------------------
# Item 6: roadmap-exit sizing stays live until every citing stub is terminal
# ---------------------------------------------------------------------------


def test_roadmap_route_sizing_with_two_stubs_one_shipped_stays_live(tmp_path: Path) -> None:
    worktree = tmp_path / "repo"
    cid = "state/sizings/2026-01-15-roadmap-exit.yaml"
    src = _write_sizing(
        worktree, "2026-01-15-roadmap-exit.yaml",
        "---\nstatus: shipped\ntitle: x\nroute: roadmap\n---\n",
    )

    _write_handoff(
        worktree, "state/handoffs/2026-01-16-stub-one.md",
        deployment_state="shipped", sizing_object=cid,
    )
    _write_handoff(
        worktree, "state/handoffs/2026-01-16-stub-two.md",
        deployment_state="in_flight", sizing_object=cid,
    )

    preview_result, _ = _preview(worktree)
    assert preview_result["candidates"] == []

    act_result, mover = _act(worktree, [cid])
    assert act_result["acted"] == []
    assert mover.captured is None
    assert len(act_result["skipped"]) == 1
    assert act_result["skipped"][0]["id"] == cid
    assert "roadmap-stubs-not-terminal" in act_result["skipped"][0]["reason"]
    assert src.exists()


def test_roadmap_exit_sizing_moves_once_every_citing_stub_is_terminal(tmp_path: Path) -> None:
    worktree = tmp_path / "repo"
    cid = "state/sizings/2026-01-15-roadmap-exit-done.yaml"
    _write_sizing(
        worktree, "2026-01-15-roadmap-exit-done.yaml",
        "---\nstatus: shipped\ntitle: x\nroute: roadmap\n---\n",
    )

    _write_handoff(
        worktree, "state/handoffs/2026-01-16-stub-a.md",
        deployment_state="shipped", sizing_object=cid,
    )
    _write_handoff(
        worktree, "state/handoffs/2026-01-16-stub-b.md",
        deployment_state="abandoned", sizing_object=cid,
    )

    result, mover = _act(worktree, [cid])

    assert result["acted"] == [{"id": cid, "archived": True}]
    assert result["skipped"] == []
    assert mover.captured is not None


def test_pm_decision_route_with_xl_exit_roadmap_is_also_a_roadmap_exit(tmp_path: Path) -> None:
    worktree = tmp_path / "repo"
    cid = "state/sizings/2026-01-15-pm-decision-roadmap.yaml"
    _write_sizing(
        worktree, "2026-01-15-pm-decision-roadmap.yaml",
        "---\nstatus: shipped\ntitle: x\nroute: pm-decision\nxl_exit: roadmap\n---\n",
    )
    _write_plan(
        worktree, "docs/plans/a-live-stub-plan.md",
        status="sized", sizing_object=cid,
    )

    act_result, mover = _act(worktree, [cid])
    assert act_result["acted"] == []
    assert mover.captured is None
    assert "roadmap-stubs-not-terminal" in act_result["skipped"][0]["reason"]


def test_pm_decision_route_with_a_different_xl_exit_is_not_a_roadmap_exit(tmp_path: Path) -> None:
    worktree = tmp_path / "repo"
    cid = "state/sizings/2026-01-15-pm-decision-split.yaml"
    _write_sizing(
        worktree, "2026-01-15-pm-decision-split.yaml",
        "---\nstatus: shipped\ntitle: x\nroute: pm-decision\nxl_exit: split\n---\n",
    )
    _write_plan(
        worktree, "docs/plans/a-live-non-roadmap-stub.md",
        status="sized", sizing_object=cid,
    )

    result, mover = _act(worktree, [cid])
    # Not a roadmap exit, so the new gate does not apply — the plan FK path
    # is also silent here (no `plan:` field on this sizing).
    assert result["acted"] == [{"id": cid, "archived": True}]
    assert mover.captured is not None


def test_roadmap_exit_sizing_with_no_citers_proceeds(tmp_path: Path) -> None:
    worktree = tmp_path / "repo"
    cid = "state/sizings/2026-01-15-roadmap-exit-no-citers.yaml"
    _write_sizing(
        worktree, "2026-01-15-roadmap-exit-no-citers.yaml",
        "---\nstatus: shipped\ntitle: x\nroute: roadmap\n---\n",
    )

    result, mover = _act(worktree, [cid])

    assert result["acted"] == [{"id": cid, "archived": True}]
    assert mover.captured is not None


def test_ordinary_dispatch_route_sizing_is_unaffected_by_the_new_gate(tmp_path: Path) -> None:
    worktree = tmp_path / "repo"
    cid = "state/sizings/2026-01-15-plain-dispatch.yaml"
    _write_sizing(
        worktree, "2026-01-15-plain-dispatch.yaml",
        "---\nstatus: shipped\ntitle: x\nroute: dispatch\n---\n",
    )
    _write_handoff(
        worktree, "state/handoffs/2026-01-16-unrelated-stub.md",
        deployment_state="in_flight", sizing_object="state/sizings/some-other-sizing.yaml",
    )

    result, mover = _act(worktree, [cid])

    assert result["acted"] == [{"id": cid, "archived": True}]
    assert mover.captured is not None


def test_preview_excludes_roadmap_exit_still_open_and_reports_scan_skipped(tmp_path: Path) -> None:
    worktree = tmp_path / "repo"
    cid = "state/sizings/2026-01-15-roadmap-exit-preview.yaml"
    _write_sizing(
        worktree, "2026-01-15-roadmap-exit-preview.yaml",
        "---\nstatus: shipped\ntitle: x\nroute: roadmap\n---\n",
    )
    _write_handoff(
        worktree, "state/handoffs/2026-01-16-preview-stub.md",
        deployment_state="in_flight", sizing_object=cid,
    )
    # A second, unconstrained terminal sizing should still surface.
    _write_sizing(worktree, "2026-01-15-plain.yaml", _TERMINAL_BODY.format(title="y"))

    scan_skipped: list = []
    preview_result, _ = _preview(worktree, scan_skipped=scan_skipped)

    ids = {c["id"] for c in preview_result["candidates"]}
    assert ids == {"state/sizings/2026-01-15-plain.yaml"}
    assert len(scan_skipped) == 1
    assert scan_skipped[0]["id"] == cid
    assert "roadmap-stubs-not-terminal" in scan_skipped[0]["reason"]

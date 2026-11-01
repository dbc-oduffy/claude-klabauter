"""The terminal-sizing drain at default invocation: every movable terminal sizing in one call,
one `git status`, one commit; forward-plan-not-terminal sizings refused and named.

Negative-spec: no real git; the commit is the recording mover and `status_porcelain` a fake.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from coordinator_core.ops.ceremony import git_native
from coordinator_core.ops.ceremony.git_native import GitResult
from coordinator_core.ops.fleet import archive_sizings, terminal_sizing_drain
from coordinator_core.ops.fleet._common import Move
from coordinator_core.ops.fleet.tests.archive_git_free_seam import (
    patched_disposition_seam,
    run,
)

_CLEAN = GitResult(returncode=0, stdout="", stderr="")


def _sizing(worktree: Path, name: str, body: str) -> None:
    d = worktree / "state" / "sizings"
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text(body, encoding="utf-8")


def _plan(worktree: Path, name: str, status: str) -> None:
    d = worktree / "docs" / "plans"
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text(f"---\nstatus: {status}\n---\n", encoding="utf-8")


def _seed(worktree: Path) -> None:
    for i in range(3):
        _sizing(worktree, f"2026-10-0{i + 1}-movable-{i}.yaml", f"status: shipped\ntitle: m{i}\n")
    _sizing(worktree, "2026-10-04-draft.yaml", "status: draft\n")
    _plan(worktree, "live.md", "executing")
    for i in range(2):
        _sizing(
            worktree, f"2026-10-1{i}-forward-{i}.yaml",
            "status: shipped\nplan: docs/plans/live.md\n",
        )


def _invoke(worktree: Path, params: dict, status=_CLEAN, scan_skipped=None):
    calls: list = []

    def fake_status(cwd, paths=None, **_kw):
        calls.append(list(paths) if paths else None)
        return status

    with patched_disposition_seam(archive_sizings, worktree=worktree) as mover, \
            patch.object(git_native, "status_porcelain", fake_status):
        result = run(archive_sizings._archive_terminal_sizings(
            params, repo_root=str(worktree), scan_skipped=scan_skipped,
        ))
    return result, mover, calls


def test_empty_params_acts_on_every_movable_in_one_commit_and_one_status(tmp_path: Path) -> None:
    worktree = tmp_path / "repo"
    _seed(worktree)

    result, mover, status_calls = _invoke(worktree, {})

    assert result["exit_code"] == 0
    assert sorted(a["id"] for a in result["acted"]) == [
        f"state/sizings/2026-10-0{i + 1}-movable-{i}.yaml" for i in range(3)
    ]
    assert len(mover.captured) == 3
    assert status_calls == [["state/sizings"]]


def test_default_act_still_refuses_forward_plan_not_terminal_and_names_each(tmp_path: Path) -> None:
    worktree = tmp_path / "repo"
    _seed(worktree)

    result, mover, _ = _invoke(worktree, {})

    refused = {s["id"]: s["reason"] for s in result["skipped"]}
    assert set(refused) == {
        "state/sizings/2026-10-10-forward-0.yaml",
        "state/sizings/2026-10-11-forward-1.yaml",
    }
    assert all(r.startswith("forward-plan-not-terminal") for r in refused.values())
    assert {m.candidate_id for m in mover.captured}.isdisjoint(refused)
    assert (worktree / "state" / "sizings" / "2026-10-10-forward-0.yaml").exists()


def test_forward_plan_gate_opens_once_the_plan_is_terminal(tmp_path: Path) -> None:
    worktree = tmp_path / "repo"
    _seed(worktree)
    _plan(worktree, "live.md", "implemented")

    result, mover, _ = _invoke(worktree, {})

    assert len(mover.captured) == 5
    assert result["skipped"] == []


def test_dry_run_keeps_the_preview_envelope_and_reports_scan_skipped(tmp_path: Path) -> None:
    worktree = tmp_path / "repo"
    _seed(worktree)
    census: list = []

    result, mover, status_calls = _invoke(worktree, {"dry_run": True}, scan_skipped=census)

    assert set(result) == {"exit_code", "mode", "dry_run", "candidates", "acted", "skipped", "failed"}
    assert result["dry_run"] is True
    assert len(result["candidates"]) == 3
    assert result["candidates"][0]["title"] == "m0"
    assert result["acted"] == result["skipped"] == result["failed"] == []
    assert len(census) == 2 and all("forward-plan-not-terminal" in c["reason"] for c in census)
    assert mover.captured is None
    assert status_calls == []


def test_dirty_candidate_is_retained_and_clean_ones_still_move(tmp_path: Path) -> None:
    worktree = tmp_path / "repo"
    _seed(worktree)
    dirty = "state/sizings/2026-10-02-movable-1.yaml"

    result, mover, _ = _invoke(
        worktree, {}, status=GitResult(returncode=0, stdout=f" M {dirty}\n", stderr=""),
    )

    assert {"id": dirty, "reason": archive_sizings._REASON_WORKTREE_DIRTY} in result["skipped"]
    assert len(mover.captured) == 2
    assert dirty not in {m.candidate_id for m in mover.captured}


def test_failed_status_retains_every_candidate(tmp_path: Path) -> None:
    worktree = tmp_path / "repo"
    _seed(worktree)

    result, mover, _ = _invoke(
        worktree, {}, status=GitResult(returncode=128, stdout="", stderr="fatal"),
    )

    assert result["acted"] == []
    assert mover.captured is None
    assert sum(1 for s in result["skipped"] if s["reason"] == archive_sizings._REASON_WORKTREE_DIRTY) == 3


def test_no_movable_sizing_spawns_nothing(tmp_path: Path) -> None:
    worktree = tmp_path / "repo"
    _sizing(worktree, "2026-10-04-draft.yaml", "status: draft\n")

    result, mover, status_calls = _invoke(worktree, {})

    assert result["acted"] == [] and mover.captured is None
    assert status_calls == []


def test_explicit_empty_candidate_ids_is_a_noop_not_act_on_all(tmp_path: Path) -> None:
    worktree = tmp_path / "repo"
    _seed(worktree)

    result, mover, _ = _invoke(worktree, {"candidate_ids": []})

    assert result["acted"] == [] and mover.captured is None


def test_unknown_mode_and_non_bool_dry_run_are_setup_errors(tmp_path: Path) -> None:
    worktree = tmp_path / "repo"
    _seed(worktree)

    assert _invoke(worktree, {"mode": "bogus"})[0]["exit_code"] == 1
    assert _invoke(worktree, {"dry_run": "yes"})[0]["exit_code"] == 1


@pytest.mark.spawns_process
def test_in_process_dirty_arm_agrees_with_git_on_a_real_repo(tmp_path: Path) -> None:
    import subprocess

    worktree = tmp_path / "repo"
    _seed(worktree)
    for args in (["init", "-q"], ["config", "user.email", "t@example.invalid"],
                 ["config", "user.name", "t"], ["config", "commit.gpgsign", "false"],
                 ["add", "-A"], ["commit", "-q", "-m", "seed"]):
        subprocess.run(["git", "-C", str(worktree), *args], check=True, capture_output=True,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    rels = [f"state/sizings/2026-10-0{i + 1}-movable-{i}.yaml" for i in range(3)]
    assert terminal_sizing_drain.dirty_among(worktree, rels) == set()

    (worktree / rels[1]).write_text("status: shipped\ntitle: edited\n", encoding="utf-8")

    assert terminal_sizing_drain.dirty_among(worktree, rels) == {rels[1]}


def _git(worktree: Path, *args: str) -> str:
    import subprocess

    done = subprocess.run(["git", "-C", str(worktree), *args], check=True, capture_output=True, text=True,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return done.stdout


@pytest.mark.spawns_process
def test_land_moves_commits_once_and_leaves_the_index_clean_on_a_real_repo(tmp_path: Path) -> None:
    worktree = tmp_path / "repo"
    _seed(worktree)
    for args in (["init", "-q"], ["config", "user.email", "t@example.invalid"],
                 ["config", "user.name", "t"], ["config", "commit.gpgsign", "false"],
                 ["add", "-A"], ["commit", "-q", "-m", "seed"]):
        _git(worktree, *args)
    _sizing(worktree, "2026-10-09-untracked.yaml", "status: shipped\n")

    result = terminal_sizing_drain.drain(worktree)

    movable = [f"state/sizings/2026-10-0{i + 1}-movable-{i}.yaml" for i in range(3)]
    assert sorted(a["id"] for a in result["acted"]) == movable
    assert (worktree / "state/sizings/2026-10-09-untracked.yaml").is_file()

    # Called directly, a src HEAD never carried has no blob to repath: refused alone, not renamed.
    src = worktree / "state/sizings/2026-10-09-untracked.yaml"
    move = Move(src=src, dst=worktree / "archive/sizings/2026-10/2026-10-09-untracked.yaml",
                candidate_id="state/sizings/2026-10-09-untracked.yaml")
    acted, failed = run(terminal_sizing_drain.land_moves(worktree, [move], "x"))
    assert acted == [] and failed[0]["reason"].startswith("untracked-at-head")
    assert src.is_file()
    assert _git(worktree, "rev-list", "--count", "HEAD").strip() == "2"
    assert _git(worktree, "status", "--porcelain", "--", "state/sizings/2026-10-0[1-3]*",
                "archive").strip() == ""
    assert all((worktree / "archive/sizings/2026-10" / Path(m).name).is_file() for m in movable)
    assert all(not (worktree / m).exists() for m in movable)


@pytest.mark.spawns_process
def test_index_splice_keeps_an_unrelated_staged_change_and_dst_collision_is_refused(tmp_path: Path) -> None:
    worktree = tmp_path / "repo"
    _seed(worktree)
    (worktree / "notes.txt").write_text("a\n", encoding="utf-8")
    for args in (["init", "-q"], ["config", "user.email", "t@example.invalid"],
                 ["config", "user.name", "t"], ["config", "commit.gpgsign", "false"],
                 ["add", "-A"], ["commit", "-q", "-m", "seed"]):
        _git(worktree, *args)
    (worktree / "notes.txt").write_text("b\n", encoding="utf-8")
    _git(worktree, "add", "notes.txt")

    result = terminal_sizing_drain.drain(worktree)

    assert len(result["acted"]) == 3
    assert _git(worktree, "diff", "--cached", "--name-only").split() == ["notes.txt"]

    taken = worktree / "state/sizings/2026-10-01-clash.yaml"
    taken.write_text("status: shipped\n", encoding="utf-8")
    _git(worktree, "add", str(taken))
    _git(worktree, "commit", "-q", "-m", "clash")
    dst = worktree / "archive/sizings/2026-10/2026-10-01-clash.yaml"
    dst.write_text("occupied\n", encoding="utf-8")
    move = Move(src=taken, dst=dst, candidate_id="state/sizings/2026-10-01-clash.yaml")
    acted, failed = run(terminal_sizing_drain.land_moves(worktree, [move], "x"))
    assert acted == [] and failed[0]["reason"].startswith("dst-exists")
    assert taken.is_file() and dst.read_text(encoding="utf-8") == "occupied\n"


def test_plan_fk_and_route_past_the_head_are_still_read(tmp_path: Path) -> None:
    worktree = tmp_path / "repo"
    _sizing(
        worktree, "2026-10-01-late-fk.yaml",
        "status: shipped\nscout:\n  - " + "x" * 5000 + "\nplan: docs/plans/live.md\nroute: roadmap\n",
    )

    record = terminal_sizing_drain.scan_sizings(worktree).records["state/sizings/2026-10-01-late-fk.yaml"]

    assert (record.plan, record.route) == ("docs/plans/live.md", "roadmap")


def test_scan_reads_each_head_once_and_never_opens_a_body_decoy(tmp_path: Path) -> None:
    worktree = tmp_path / "repo"
    _sizing(worktree, "2026-10-01-fenced.yaml", "---\ntitle: t\n---\nstatus: shipped\n")
    _sizing(worktree, "2026-10-02-bare.yaml", "status: 'shipped'  # c\nplan: null\n")

    scan = terminal_sizing_drain.scan_sizings(worktree)

    assert scan.records["state/sizings/2026-10-01-fenced.yaml"].status == ""
    bare = scan.records["state/sizings/2026-10-02-bare.yaml"]
    assert bare.status == "shipped" and bare.plan == ""


def test_scalar_reader_takes_the_first_parseable_top_level_key_and_ignores_nested_ones(tmp_path: Path) -> None:
    worktree = tmp_path / "repo"
    _sizing(
        worktree, "2026-10-01-shapes.yaml",
        'plan:\n  nested: 1\n  plan: ignored.md\nplan: "docs/plans/p.md"\nstatus: shipped # c\n'
        "route:   roadmap\nroute: plan\ntitle:  Quoted title  # note\nxl_exit: ~\nplanned: nope\n",
    )

    record = terminal_sizing_drain.scan_sizings(worktree).records["state/sizings/2026-10-01-shapes.yaml"]

    assert (record.plan, record.status, record.route, record.xl_exit, record.title) == (
        "docs/plans/p.md", "shipped", "roadmap", "", "Quoted title",
    )

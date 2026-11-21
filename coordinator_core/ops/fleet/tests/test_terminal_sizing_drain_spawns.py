"""The terminal-sizing drain at default invocation: every movable terminal sizing in one call,
one `git status`, one commit; forward-plan-not-terminal sizings refused and named.

Negative-spec: no real git; the commit is the recording mover and `status_porcelain` a fake.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = [pytest.mark.complete, pytest.mark.spawns_process, pytest.mark.cadence]

from coordinator_core.ops.ceremony.git_native import GitResult
from coordinator_core.ops.fleet import terminal_sizing_drain
from coordinator_core.ops.fleet._common import Move
from coordinator_core.ops.fleet.tests.archive_git_free_seam import run

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
def test_land_moves_commits_once_and_leaves_the_index_clean_on_a_real_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    worktree = tmp_path / "repo"
    _registry(tmp_path, monkeypatch, worktree)
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
def test_index_splice_keeps_an_unrelated_staged_change_and_dst_collision_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    worktree = tmp_path / "repo"
    _registry(tmp_path, monkeypatch, worktree)
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


def _registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *repos: Path) -> None:
    reg = tmp_path / "registry"
    reg.mkdir()
    lines = [f'{k} = "{p.as_posix()}"' for k, p in zip("abc", repos)]
    (reg / "registry.local.toml").write_text(
        "[repos]\n" + "\n".join(lines) + "\n", encoding="utf-8"
    )
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(reg))

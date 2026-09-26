
from __future__ import annotations

import os
import subprocess

import pytest

from coordinator_core.bash_guards import dispatch_checks as dc
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]


def _git(*args: str, cwd: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        **no_console_creationflags(),
    )


def _repo_with_dirty_files(tmp_path, count: int):
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    root = str(repo)
    _git("init", "-q", cwd=root)
    _git("config", "user.email", "t@example.com", cwd=root)
    _git("config", "user.name", "t", cwd=root)
    (repo / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git("add", "seed.txt", cwd=root)
    _git("commit", "-qm", "seed", cwd=root)

    work = repo / "work"
    work.mkdir()
    targets = []
    for i in range(count):
        f = work / f"f{i}.py"
        f.write_text(f"# uncommitted {i}\n", encoding="utf-8")
        targets.append(str(f))
    return root, targets


def _count_status_spawns(monkeypatch, root: str, targets: list[str]):
    original = dc._run_git
    status_calls: list[tuple] = []

    def counting(args, *a, **k):
        if "status" in args:
            status_calls.append(tuple(args))
        return original(args, *a, **k)

    monkeypatch.setattr(dc, "_run_git", counting)
    monkeypatch.chdir(root)
    verdict = dc.check_destructive_rm(
        "rm " + " ".join(targets), session_id="spawn-batch-probe", payload={}
    )
    return verdict, status_calls


def test_status_spawn_count_does_not_grow_with_target_count(tmp_path, monkeypatch):
    root, targets = _repo_with_dirty_files(tmp_path, 5)
    _verdict, calls = _count_status_spawns(monkeypatch, root, targets)
    assert len(calls) <= 1, (
        "the dirty-work probe spawned one `git status` per rm target again -- it "
        f"takes many pathspecs, so five targets need one call, not {len(calls)}: {calls}"
    )


def test_one_target_and_many_targets_cost_the_same(tmp_path, monkeypatch):
    root_one, one = _repo_with_dirty_files(tmp_path / "a", 1)
    _v1, calls_one = _count_status_spawns(monkeypatch, root_one, one)
    root_many, many = _repo_with_dirty_files(tmp_path / "b", 8)
    _v2, calls_many = _count_status_spawns(monkeypatch, root_many, many)
    assert len(calls_many) == len(calls_one), (
        f"one target cost {len(calls_one)} status spawn(s), eight cost "
        f"{len(calls_many)} -- the probe is still per-target"
    )


def test_each_target_keeps_its_own_status(tmp_path):
    root, targets = _repo_with_dirty_files(tmp_path, 3)
    _git("add", os.path.relpath(targets[1], root).replace("\\", "/"), cwd=root)
    _git("commit", "-qm", "clean-one", cwd=root)

    out = subprocess.run(
        ["git", "-C", root, "--no-optional-locks", "status", "--porcelain", "--", *targets],
        capture_output=True,
        text=True,
        check=False,
        **no_console_creationflags(),
    )
    table = dc._attribute_porcelain(out.stdout, root, targets)
    assert table is not None, "the plain `XY <path>` shape must be attributable"

    key = lambda p: os.path.normcase(os.path.normpath(p))  # noqa: E731
    assert table[key(targets[1])] == "", "a committed target must read clean"
    assert table[key(targets[0])], "an uncommitted target must keep its own rows"
    assert table[key(targets[2])], "an uncommitted target must keep its own rows"


def test_unparseable_shapes_decline_rather_than_report_clean():
    assert dc._attribute_porcelain(' M "odd\\303\\251.py"\n', "/repo", ["/repo/odd.py"]) is None
    assert dc._attribute_porcelain("R  old.py -> new.py\n", "/repo", ["/repo/new.py"]) is None

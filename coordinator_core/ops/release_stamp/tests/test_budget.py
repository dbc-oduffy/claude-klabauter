"""Process-time and spawn budget of completion.stamp_releases: 20 version tags, 400 pending entries."""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from coordinator_core.benchmarks.process_time import batched_process_time_ms, in_process_time_ms
from coordinator_core.ipc import _REGISTRY
from coordinator_core.ops import release_stamp as rs
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_TAGS = 20
_PER_TAG = 20
_BUDGET_MS = 500.0
_POOL = 40
_ROOT = "archive/completed"


def _git(repo: Path, *args: str, date: str) -> None:
    env = {
        "GIT_AUTHOR_DATE": date,
        "GIT_COMMITTER_DATE": date,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.invalid",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.invalid",
    }
    out = subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        env={**os.environ, **env},
        **no_console_creationflags(),
    )
    assert out.returncode == 0, f"git {args}: {out.stderr}"


@pytest.fixture(scope="module")
def fixture_repo(tmp_path_factory) -> Path:
    repo = tmp_path_factory.mktemp("budget") / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", date="2026-01-01T10:00:00")
    for t in range(_TAGS):
        date = f"2026-02-{t + 1:02d}T10:00:00"
        month = repo / _ROOT / f"2026-{(t % 12) + 1:02d}"
        month.mkdir(parents=True, exist_ok=True)
        for i in range(_PER_TAG):
            name = f"t{t:02d}-e{i:02d}.md"
            (month / name).write_text(
                f"---\ntitle: {name}\nstatus: pending-release\n---\nbody {name}\n",
                encoding="utf-8",
                newline="",
            )
        _git(repo, "add", "-A", date=date)
        _git(repo, "commit", "-q", "-m", f"c{t}", date=date)
        _git(repo, "tag", "-m", "rel", f"v0.{t + 1}.0", date=date)
    return repo


class _CountingPopen(subprocess.Popen):
    count = 0

    def __init__(self, *args, **kwargs):
        type(self).count += 1
        super().__init__(*args, **kwargs)


def _unstamped_copies(source: Path, dest: Path, n: int) -> list[Path]:
    """Worktree copies sharing the source's object store through a gitdir pointer."""
    copies = []
    for i in range(n):
        copy = dest / f"copy{i}"
        shutil.copytree(source / _ROOT, copy / _ROOT)
        (copy / ".git").write_text(f"gitdir: {(source / '.git').as_posix()}\n", encoding="utf-8")
        copies.append(copy)
    return copies


def _call(repo: Path, **params) -> dict:
    return asyncio.run(_REGISTRY["completion.stamp_releases"](params, repo))


def test_write_mode_spawns_exactly_one_git_child(fixture_repo: Path, tmp_path: Path, monkeypatch) -> None:
    (copy,) = _unstamped_copies(fixture_repo, tmp_path, 1)
    _CountingPopen.count = 0
    monkeypatch.setattr(subprocess, "Popen", _CountingPopen)

    res = _call(copy)

    monkeypatch.undo()
    assert len(res["stamped"]) == _TAGS * _PER_TAG and res["unreleased"]["count"] == 0
    assert res["tags_considered"] == _TAGS and res["skipped"] == []
    assert _CountingPopen.count == 1


def test_write_mode_process_time_under_budget(fixture_repo: Path, tmp_path: Path) -> None:
    copies = iter(_unstamped_copies(fixture_repo, tmp_path, _POOL))
    outcomes: list[dict] = []

    def one_call() -> None:
        outcomes.append(_call(next(copies)))

    parent = in_process_time_ms(one_call)
    assert all(len(o["stamped"]) == _TAGS * _PER_TAG and o["skipped"] == [] for o in outcomes), (
        "the measured operation did not stamp every entry"
    )

    child = batched_process_time_ms(
        ["git", "for-each-ref", f"--format={rs._FORMAT}", "refs/tags/v*"],
        k=20,
        cwd=str(fixture_repo),
    )
    assert child["rc"] == 0

    total = parent["process_time_ms"] + child["process_time_ms"]
    print(
        f"release-stamp process time: parent {parent['process_time_ms']}ms"
        f" + child {child['process_time_ms']}ms = {total}ms ({sys.platform})"
    )
    assert total < _BUDGET_MS, (
        f"parent {parent['process_time_ms']}ms + child {child['process_time_ms']}ms = {total}ms"
    )

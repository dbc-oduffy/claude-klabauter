"""Falsifier: one baton.bulk_set call sets owner+eta on 809 batons in 0 spawns, one commit, <500ms."""

from __future__ import annotations

import asyncio
import importlib
import os
import subprocess
import time
from pathlib import Path

import pytest

N = 809
PROCESS_BUDGET_S = 0.5
ETA = "2026-12-01"
OWNER = "sess-bulk-0001"
_GIT_ENV = {
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid",
}


def _git(repo: Path, *args: str) -> str:
    out = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True,
        env={**os.environ, **_GIT_ENV}, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return out.stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-q")
    (tmp_path / "state" / "handoffs").mkdir(parents=True)
    for i in range(N):
        (tmp_path / "state" / "handoffs" / f"b{i:03d}.md").write_bytes(
            f"---\ndeliverable_id: d-{i:03d}\nstatus: ready_to_fire\n---\nbody {i}\n".encode("utf-8"),
        )
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "fixture")
    return tmp_path


def _items() -> list[dict]:
    return [
        {"path": f"state/handoffs/b{i:03d}.md", "owner_session": OWNER, "eta": ETA}
        for i in range(N)
    ]


def _call(repo: Path, **params) -> tuple[dict, int, float]:
    mod = importlib.import_module("coordinator_core.ops.baton_bulk_set")
    spawns: list[object] = []
    real_popen, real_system = subprocess.Popen, os.system

    class _Counting(real_popen):  # type: ignore[misc, valid-type]
        def __init__(self, *a, **k):
            spawns.append(a[0] if a else k.get("args"))
            super().__init__(*a, **k)

    def _system(cmd):
        spawns.append(cmd)
        return real_system(cmd)

    subprocess.Popen, os.system = _Counting, _system
    try:
        t0 = time.process_time()
        result = asyncio.run(mod._handler(params, repo))
        elapsed = time.process_time() - t0
    finally:
        subprocess.Popen, os.system = real_popen, real_system
    return result, len(spawns), elapsed


def _snapshot(repo: Path) -> dict[str, bytes]:
    return {p.name: p.read_bytes() for p in sorted((repo / "state" / "handoffs").glob("*.md"))}


def test_809_batons_one_call_zero_spawns_one_commit_under_budget(repo: Path):
    head = _git(repo, "rev-parse", "HEAD")
    result, spawns, elapsed = _call(repo, items=_items())
    assert result["exit_code"] == 0
    assert len(result["applied"]) == N
    assert result["unchanged"] == [] and result["refused"] == []
    assert spawns == 0
    assert elapsed < PROCESS_BUDGET_S
    assert _git(repo, "rev-list", "--count", f"{head}..HEAD") == "1"
    assert _git(repo, "rev-parse", "HEAD") == result["commit_sha"]
    assert set(_git(repo, "diff", "--name-only", head, "HEAD").splitlines()) == set(result["pathspec"])
    assert len(result["pathspec"]) == N
    text = (repo / "state" / "handoffs" / "b000.md").read_text(encoding="utf-8")
    assert f"owner_session: {OWNER}" in text and f"eta: {ETA}" in text


def test_dry_run_lists_changes_and_writes_nothing(repo: Path):
    head = _git(repo, "rev-parse", "HEAD")
    before = _snapshot(repo)
    result, spawns, _ = _call(repo, items=_items(), dry_run=True)
    assert result["dry_run"] is True
    assert len(result["applied"]) == N
    assert result["commit_sha"] is None
    assert spawns == 0
    assert _git(repo, "rev-parse", "HEAD") == head
    assert _snapshot(repo) == before


def test_repeat_call_is_a_no_op(repo: Path):
    _call(repo, items=_items())
    head = _git(repo, "rev-parse", "HEAD")
    result, spawns, _ = _call(repo, items=_items())
    assert result["applied"] == []
    assert len(result["unchanged"]) == N
    assert result["commit_sha"] is None
    assert spawns == 0
    assert _git(repo, "rev-parse", "HEAD") == head

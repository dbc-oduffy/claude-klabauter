"""run_git / run_git_stream carry `-c gc.auto=0` on every spawned argv.

Pins `coordinator_core/git/run.py :: _GC_AUTO_OFF`: the guarantee is per-invocation argv, so
auto-gc cannot start inside an engine landing even where the repo config sets `gc.auto` high or
was never written. The behavioural test builds a repo with `gc.auto=1` and loose objects past the
threshold; its positive control (a bare git commit packs) proves the fixture can fail.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

from coordinator_core.git.run import run_git

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _git(repo: Path, *args: str, stdin: bytes | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args], input=stdin, check=True, capture_output=True,
        creationflags=_NO_WINDOW,
    )


def _packs(repo: Path) -> list[str]:
    return sorted(p.name for p in (repo / ".git" / "objects" / "pack").glob("*.pack"))


def _loose_in_17(repo: Path) -> int:
    d = repo / ".git" / "objects" / "17"
    return len(list(d.iterdir())) if d.is_dir() else 0


@pytest.fixture()
def loose_repo(tmp_path: Path) -> Path:
    """A repo with gc.auto=1 and enough loose objects that `gc --auto` would repack."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    for key, value in (
        ("user.name", "t"), ("user.email", "t@example.invalid"), ("gc.auto", "1"), ("maintenance.auto", "true"),
        ("maintenance.autoDetach", "false"), ("maintenance.gc.enabled", "true"),
        ("gc.autoDetach", "false"), ("commit.gpgsign", "false"), ("core.hooksPath", "none"),
    ):
        _git(repo, "config", key, value)
    # Staged (index-reachable) blobs: `gc --auto` repacks reachable loose objects only.
    batch = 0
    while _loose_in_17(repo) < 3:
        for i in range(500):
            (repo / f"f{batch}_{i}.txt").write_text(f"blob {batch} {i}\n")
        _git(repo, "add", "-A")
        batch += 1
        assert batch < 20, "could not seed loose objects past the auto-gc threshold"
    assert _packs(repo) == []
    return repo


def test_positive_control_a_bare_commit_auto_gcs(loose_repo: Path):
    _git(loose_repo, "commit", "-q", "--allow-empty", "-m", "bare")
    assert _packs(loose_repo), "fixture cannot trigger auto-gc, so the guard test proves nothing"


def test_run_git_commit_creates_no_pack_despite_gc_auto_config(loose_repo: Path):
    result = run_git(["commit", "-q", "--allow-empty", "-m", "engine"], cwd=str(loose_repo))
    assert result.ok, result.stderr
    assert _packs(loose_repo) == []

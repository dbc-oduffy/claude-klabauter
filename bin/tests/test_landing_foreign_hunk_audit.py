from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_REPO_ROOT = Path(__file__).parent.parent.parent.resolve()
_SCRIPT = _REPO_ROOT / "bin" / "landing-foreign-hunk-audit.py"
_NOWIN = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _git(repo: Path, *args: str) -> str:
    env = {**os.environ, "GIT_AUTHOR_DATE": "2026-10-29T10:00:00Z", "GIT_COMMITTER_DATE": "2026-10-29T10:00:00Z"}
    return subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *args],
        capture_output=True, text=True, check=True, env=env, creationflags=_NOWIN,
    ).stdout


def _make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "a.txt").write_text("one\n", encoding="utf-8")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-q", "-m", "engine landing with no run record\n\nLands-Chunks: C9")
    return repo


def _run(repo: Path, *args: str) -> subprocess.CompletedProcess:
    """Run the launcher with cwd=repo and no --repo, so the cwd default is exercised."""
    return subprocess.run(
        [sys.executable, str(_SCRIPT), *args],
        capture_output=True, text=True, cwd=str(repo), creationflags=_NOWIN,
    )


def test_default_arguments_print_table_and_leave_tree_unchanged(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    before = _git(repo, "status", "--porcelain")
    proc = _run(repo)
    assert proc.returncode == 0, proc.stderr
    assert "COMMIT" in proc.stdout and "LIKELY_OWNER" in proc.stdout
    assert "a.txt" in proc.stdout
    assert _git(repo, "status", "--porcelain") == before


def test_json_output_has_landings_array(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    before = _git(repo, "status", "--porcelain")
    proc = _run(repo, "--json")
    assert proc.returncode == 0, proc.stderr
    doc = json.loads(proc.stdout)
    assert isinstance(doc["landings"], list) and doc["landings"]
    entry = doc["landings"][0]
    assert entry["run_id"] is None
    assert [f["path"] for f in entry["foreign"]] == ["a.txt"]
    assert _git(repo, "status", "--porcelain") == before

"""check-no-illegal-paths reads git's NUL-separated listing, never the
C-quoted one (example-stats-repo EM report 2026-10-05: every non-ASCII filename
failed merge-assemble d6 on the quote git wrapped it in)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_SCRIPT = Path(__file__).resolve().parent.parent / "check-no-illegal-paths.py"


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    return repo


def _check(repo: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(_SCRIPT), str(repo)], capture_output=True, text=True
    )


def test_a_non_ascii_filename_is_clean(tmp_path):
    repo = _repo(tmp_path)
    (repo / "data").mkdir()
    (repo / "data" / "Copa Am\u00e9rica Femenina _ FBref.com.json").write_text("{}")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "seed")
    (repo / "S\u00e3o Paulo.txt").write_text("staged only")
    _git(repo, "add", ".")

    result = _check(repo)

    assert result.returncode == 0, result.stderr


def test_a_genuinely_illegal_component_is_still_reported(tmp_path):
    repo = _repo(tmp_path)
    try:
        (repo / "what?.txt").write_text("x")
    except OSError:
        pytest.skip("this filesystem cannot hold the fixture name")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "seed")

    result = _check(repo)

    assert result.returncode == 1
    assert "what?.txt" in result.stderr

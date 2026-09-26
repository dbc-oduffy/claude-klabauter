"""`percolate.publish_sync._orphan_provenance` — telling a branch divergence
from a real orphan.

WHY THIS MATTERS ENOUGH TO OWN A FILE. A destination directory with no source
counterpart has two causes that look identical at the destination and have
OPPOSITE remedies: it was deleted on purpose (the sweep is right), or it was
published from a source branch this run is not on (deleting it reverts a peer's
work). The refusal used to offer only `COORDINATOR_OVERRIDE_ORPHAN_SWEEP` --
which is the WRONG remedy in the second case, offered as the only one -- and
telling the two apart cost an operator manual cross-branch archaeology. These
tests pin that the archaeology now runs itself.

Real git repositories, not mocks: the question is literally "what does this
repo's history say", so a mock would pin this module's belief about `git log
--all` rather than its answer.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from percolate.publish_sync import _orphan_provenance  # noqa: E402

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_NO_CONSOLE = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True, text=True, check=True, **_NO_CONSOLE,
    ).stdout.strip()


@pytest.fixture()
def source_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "source"
    (repo / "pkg").mkdir(parents=True)
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "t@example.invalid")
    _git(repo, "config", "user.name", "t")
    (repo / "pkg" / "kept.py").write_text("kept\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "seed")
    return repo


def test_a_directory_on_another_branch_reads_as_a_divergence_not_an_orphan(source_repo):
    _git(source_repo, "checkout", "-b", "work/feature")
    (source_repo / "pkg" / "p4").mkdir()
    (source_repo / "pkg" / "p4" / "runner.py").write_text("x\n", encoding="utf-8")
    _git(source_repo, "add", "-A")
    _git(source_repo, "commit", "-m", "p4")
    _git(source_repo, "checkout", "main")

    sentence = _orphan_provenance(source_repo / "pkg", "p4")

    assert "BRANCH DIVERGENCE" in sentence
    assert "work/feature" in sentence
    assert "Do NOT override" in sentence


def test_a_directory_in_no_branch_at_all_says_so_rather_than_claiming_orphaned(source_repo):
    sentence = _orphan_provenance(source_repo / "pkg", "never-existed")

    assert "appears nowhere" in sentence
    assert "Confirm before deleting" in sentence


def test_a_source_dir_outside_any_repository_degrades_to_silence(tmp_path):
    plain = tmp_path / "not-a-repo"
    plain.mkdir()
    assert _orphan_provenance(plain, "whatever") == ""

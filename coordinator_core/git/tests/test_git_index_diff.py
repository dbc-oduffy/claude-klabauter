"""Tests for `coordinator_core.git.git_index.diff_index_name_status`.

Split out of `test_git_index.py` on the same "real git vs synthesised
fixture" line `test_git_state_against_real_git.py` draws against its own
sibling: a HEAD-vs-index sha comparison is exactly the kind of assertion a
synthesised index/commit pair would just be re-checking against itself.
Every test here spawns real `git` in its own setup (never in the module
under test -- `diff_index_name_status` itself spawns nothing beyond the
ONE memoised `head_blobs` call it documents), so the whole file is
`pytestmark`-tiered onto cadence per that sibling's own module docstring.

Negative spec: nothing here may be de-tiered by faking its git.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from coordinator_core.git.git_index import diff_index_name_status  # noqa: E402
from coordinator_core.win_portability import no_console_creationflags  # noqa: E402

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def _git(args, *, cwd):
    kwargs = dict(cwd=str(cwd), check=True, capture_output=True, text=True, **no_console_creationflags())
    return subprocess.run(["git", *args], **kwargs)


def _init_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    _git(["init", "-q"], cwd=repo)
    _git(["config", "user.email", "t@t.example"], cwd=repo)
    _git(["config", "user.name", "t"], cwd=repo)


def test_unchanged_path_absent_from_result(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "a.txt").write_text("one", encoding="utf-8")
    _git(["add", "--", "a.txt"], cwd=repo)
    _git(["commit", "-q", "-m", "seed"], cwd=repo)

    verdicts = diff_index_name_status(repo, ["a.txt"])

    assert verdicts == {}


def test_staged_modification_reads_m(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "a.txt").write_text("one", encoding="utf-8")
    _git(["add", "--", "a.txt"], cwd=repo)
    _git(["commit", "-q", "-m", "seed"], cwd=repo)

    (repo / "a.txt").write_text("two", encoding="utf-8")
    _git(["add", "--", "a.txt"], cwd=repo)

    verdicts = diff_index_name_status(repo, ["a.txt"])

    assert verdicts == {"a.txt": "M"}


def test_staged_new_path_reads_a(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "seed.txt").write_text("seed", encoding="utf-8")
    _git(["add", "--", "seed.txt"], cwd=repo)
    _git(["commit", "-q", "-m", "seed"], cwd=repo)

    (repo / "new.txt").write_text("new", encoding="utf-8")
    _git(["add", "--", "new.txt"], cwd=repo)

    verdicts = diff_index_name_status(repo, ["new.txt"])

    assert verdicts == {"new.txt": "A"}


def test_staged_deletion_reads_d(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "gone.txt").write_text("bye", encoding="utf-8")
    _git(["add", "--", "gone.txt"], cwd=repo)
    _git(["commit", "-q", "-m", "seed"], cwd=repo)

    _git(["rm", "-q", "--", "gone.txt"], cwd=repo)

    verdicts = diff_index_name_status(repo, ["gone.txt"])

    assert verdicts == {"gone.txt": "D"}


def test_scoped_to_pathspec_ignores_unrelated_staged_change(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "a.txt").write_text("one", encoding="utf-8")
    (repo / "b.txt").write_text("one", encoding="utf-8")
    _git(["add", "--", "a.txt", "b.txt"], cwd=repo)
    _git(["commit", "-q", "-m", "seed"], cwd=repo)

    (repo / "a.txt").write_text("two", encoding="utf-8")
    (repo / "b.txt").write_text("two", encoding="utf-8")
    _git(["add", "--", "a.txt", "b.txt"], cwd=repo)

    verdicts = diff_index_name_status(repo, ["a.txt"])

    assert verdicts == {"a.txt": "M"}
    assert "b.txt" not in verdicts


def test_second_call_after_git_add_observes_newly_staged_path(tmp_path):
    """Pins the ordering property this function's docstring names: two
    calls around a `git add` are NOT duplicates, and this function must
    never cache a diff result across them -- the second call has to see
    the path the `git add` in between just staged.
    """
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "seed.txt").write_text("seed", encoding="utf-8")
    _git(["add", "--", "seed.txt"], cwd=repo)
    _git(["commit", "-q", "-m", "seed"], cwd=repo)

    (repo / "new.txt").write_text("new", encoding="utf-8")

    before = diff_index_name_status(repo, ["new.txt"])
    assert before == {}

    _git(["add", "--", "new.txt"], cwd=repo)

    after = diff_index_name_status(repo, ["new.txt"])
    assert after == {"new.txt": "A"}

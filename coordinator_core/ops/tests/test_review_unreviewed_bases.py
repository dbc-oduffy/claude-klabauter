"""unreviewed_introducer_bases: base of the newest unreviewed commit that touched each declared path."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops import review_coverage_core as rcc
from coordinator_core.ops import review_unreviewed_bases as rub
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    ).stdout.strip()


def _commit(repo: Path, name: str, text: str, trailer: str = "") -> str:
    p = repo / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", f"edit {name}\n\n{trailer}")
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path):
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@example.com")
    _git(tmp_path, "config", "user.name", "T")
    return tmp_path


@pytest.fixture(autouse=True)
def empty_reviewed_set(monkeypatch):
    monkeypatch.setattr(rcc, "read_reviewed_set", lambda _cwd: frozenset())


def test_unstamped_introducer_maps_to_its_first_parent(repo):
    before = _commit(repo, "seed.py", "s = 1\n")
    _commit(repo, "pkg/a.py", "a = 1\n")
    head = _commit(repo, "other.py", "o = 1\n")
    assert rub.unreviewed_introducer_bases(repo, head, ["pkg/a.py"]) == {"pkg/a.py": before}


def test_newest_toucher_wins(repo):
    _commit(repo, "pkg/a.py", "a = 1\n")
    parent = _commit(repo, "x.py", "x = 1\n")
    head = _commit(repo, "pkg/a.py", "a = 2\n")
    assert rub.unreviewed_introducer_bases(repo, head, ["pkg/a.py"]) == {"pkg/a.py": parent}


def test_root_commit_maps_to_empty_tree(repo):
    head = _commit(repo, "pkg/a.py", "a = 1\n")
    assert rub.unreviewed_introducer_bases(repo, head, ["pkg/a.py"]) == {"pkg/a.py": rub.EMPTY_TREE_SHA}


def test_inline_review_trailer_keeps_path_excluded(repo):
    _commit(repo, "seed.py", "s = 1\n")
    head = _commit(repo, "pkg/a.py", "a = 1\n", "Inline-Review: applies stem-1")
    assert rub.unreviewed_introducer_bases(repo, head, ["pkg/a.py"]) == {}


def test_post_landing_trailer_is_not_a_review(repo):
    before = _commit(repo, "seed.py", "s = 1\n")
    head = _commit(repo, "pkg/a.py", "a = 1\n", "review: post-landing")
    assert rub.unreviewed_introducer_bases(repo, head, ["pkg/a.py"]) == {"pkg/a.py": before}


def test_commit_in_reviewed_set_keeps_path_excluded(repo, monkeypatch):
    _commit(repo, "seed.py", "s = 1\n")
    head = _commit(repo, "pkg/a.py", "a = 1\n")
    monkeypatch.setattr(rcc, "read_reviewed_set", lambda _cwd: frozenset({head}))
    assert rub.unreviewed_introducer_bases(repo, head, ["pkg/a.py"]) == {}


def test_reviewed_newest_toucher_does_not_fall_back_to_older_unreviewed(repo):
    _commit(repo, "pkg/a.py", "a = 1\n")
    head = _commit(repo, "pkg/a.py", "a = 2\n", "Inline-Review: applies stem-1")
    assert rub.unreviewed_introducer_bases(repo, head, ["pkg/a.py"]) == {}


def test_directory_path_keys_changed_files(repo):
    before = _commit(repo, "seed.py", "s = 1\n")
    head = _commit(repo, "pkg/sub/a.py", "a = 1\n")
    assert rub.unreviewed_introducer_bases(repo, head, ["pkg"]) == {"pkg/sub/a.py": before}


def test_sibling_prefix_path_is_not_matched(repo):
    _commit(repo, "pkg_other/a.py", "a = 1\n")
    head = _git(repo, "rev-parse", "HEAD")
    assert rub.unreviewed_introducer_bases(repo, head, ["pkg"]) == {}


def test_base_bounds_the_walk(repo):
    base = _commit(repo, "pkg/a.py", "a = 1\n")
    _commit(repo, "pkg/a.py", "a = 2\n")
    assert rub.unreviewed_introducer_bases(repo, base, ["pkg/a.py"]) == {"pkg/a.py": rub.EMPTY_TREE_SHA}


def test_toucher_beyond_the_cap_keeps_run_base(repo, monkeypatch):
    _commit(repo, "pkg/a.py", "a = 1\n")
    for i in range(3):
        _commit(repo, "pkg/b.py", f"b = {i}\n")
    head = _git(repo, "rev-parse", "HEAD")
    declared = ["pkg/a.py", "pkg/b.py"]
    monkeypatch.setattr(rub, "_INTRODUCER_COMMIT_CAP", 2)
    assert set(rub.unreviewed_introducer_bases(repo, head, declared)) == {"pkg/b.py"}
    monkeypatch.setattr(rub, "_INTRODUCER_COMMIT_CAP", 10)
    assert set(rub.unreviewed_introducer_bases(repo, head, declared)) == {"pkg/a.py", "pkg/b.py"}


def test_empty_paths_spawns_nothing(repo, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("spawned")

    monkeypatch.setattr(rub, "run_git", boom)
    assert rub.unreviewed_introducer_bases(repo, "HEAD", []) == {}


def test_one_spawn_for_many_paths(repo, monkeypatch):
    for n in ("a", "b", "c"):
        _commit(repo, f"{n}.py", f"{n} = 1\n")
    calls = []
    real = rub.run_git
    monkeypatch.setattr(rub, "run_git", lambda *a, **k: calls.append(1) or real(*a, **k))
    got = rub.unreviewed_introducer_bases(repo, "HEAD", ["a.py", "b.py", "c.py"])
    assert set(got) == {"a.py", "b.py", "c.py"}
    assert len(calls) == 1


def test_git_failure_returns_empty(repo):
    _commit(repo, "pkg/a.py", "a = 1\n")
    assert rub.unreviewed_introducer_bases(repo, "no-such-ref", ["pkg/a.py"]) == {}

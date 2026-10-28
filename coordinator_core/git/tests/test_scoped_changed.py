"""Parity of `scoped_changed` against real `git status` on tmp_path repos.

Every fixture is built under `tmp_path` with real git. A shape the module
gates must return `None`; every other shape must equal git's `1`/`u` records
(changed) and its `?` records (untracked files).
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from coordinator_core.git.scoped_changed import scoped_changed, scoped_changed_detail  # noqa: E402

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_NOWIN = getattr(subprocess, "CREATE_NO_WINDOW", 0)


@pytest.fixture(autouse=True)
def _isolated_git_env(monkeypatch):
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for var in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
        monkeypatch.delenv(var, raising=False)


def _git(repo: Path, *args: str, stdin: bytes | None = None) -> bytes:
    res = subprocess.run(
        ["git", "-C", str(repo), *args],
        input=stdin,
        capture_output=True,
        creationflags=_NOWIN,
    )
    assert res.returncode == 0, res.stderr.decode()
    return res.stdout


def _write(repo: Path, rel: str, text: str = "x\n") -> None:
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(text.encode())


def _commit_all(repo: Path, message: str = "c") -> None:
    time.sleep(0.05)
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", message)
    time.sleep(0.05)


def _repo(tmp_path: Path, files: dict, autocrlf: str = "true") -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "core.autocrlf", autocrlf)
    _git(repo, "config", "core.filemode", "false")
    for rel, text in files.items():
        _write(repo, rel, text)
    _commit_all(repo)
    return repo


_BASE = {
    "a.txt": "a\n",
    "d/b.txt": "b\n",
    "d/sub/c.txt": "c\n",
    "r/x.txt": "x\n",
    "r/deep/y.txt": "y\n",
    "other/z.txt": "z\n",
    "crlf.txt": "q\r\n",
}


def _real(repo: Path, literals, roots, globs):
    specs = [f":(literal){p}" for p in literals] + [f":(literal){r}/" for r in roots]
    specs += [f":(glob){g}/*" if g else ":(glob)*" for g in globs]
    out = _git(
        repo, "status", "--porcelain=v2", "-z", "--no-renames",
        "--untracked-files=all", "--ignored=matching", "--", *specs,
    )
    changed, untracked = set(), set()
    for raw in out.split(b"\0"):
        if len(raw) < 3:
            continue
        kind = raw[:1]
        fields = {b"1": 8, b"u": 10, b"?": 1}.get(kind)
        if fields is None:
            continue
        path = raw.split(b" ", fields)[fields].decode()
        (untracked if kind == b"?" else changed).add(path)
    return changed, untracked


def _check(repo: Path, literals=(), roots=(), globs=()):
    got = scoped_changed(repo, list(literals), list(roots), list(globs))
    assert got is not None, "fixture is a parity shape, not a gap"
    changed, candidates = got
    real_changed, real_untracked = _real(repo, literals, roots, globs)
    assert changed == real_changed
    assert {c for c in candidates if not c.endswith("/")} == real_untracked
    return changed, candidates


def test_clean_tree_reports_nothing(tmp_path):
    repo = _repo(tmp_path, _BASE)
    assert _check(repo, ["a.txt"], ["r"], ["d"]) == (set(), [d for d in ["d/sub/"] if False])


def test_modified_file_is_changed(tmp_path):
    repo = _repo(tmp_path, _BASE)
    _write(repo, "a.txt", "changed content\n")
    _write(repo, "r/deep/y.txt", "yy yy\n")
    changed, _ = _check(repo, ["a.txt"], ["r"], [])
    assert changed == {"a.txt", "r/deep/y.txt"}


def test_staged_only_change_is_changed(tmp_path):
    repo = _repo(tmp_path, _BASE)
    _write(repo, "d/b.txt", "staged\n")
    time.sleep(0.05)
    _git(repo, "add", "d/b.txt")
    time.sleep(0.05)
    changed, _ = _check(repo, ["d/b.txt"], [], [])
    assert changed == {"d/b.txt"}


def test_staged_then_reverted_is_changed(tmp_path):
    repo = _repo(tmp_path, _BASE)
    _write(repo, "d/b.txt", "staged\n")
    time.sleep(0.05)
    _git(repo, "add", "d/b.txt")
    _write(repo, "d/b.txt", "b\n")
    time.sleep(0.05)
    changed, _ = _check(repo, [], [], ["d"])
    assert changed == {"d/b.txt"}


def test_deleted_file_is_changed(tmp_path):
    repo = _repo(tmp_path, _BASE)
    (repo / "r" / "x.txt").unlink()
    changed, _ = _check(repo, [], ["r"], [])
    assert changed == {"r/x.txt"}


def test_staged_delete_with_file_left_behind(tmp_path):
    repo = _repo(tmp_path, _BASE)
    _git(repo, "rm", "-q", "--cached", "r/x.txt")
    changed, candidates = _check(repo, ["r/x.txt"], ["r"], [])
    assert "r/x.txt" in changed and "r/x.txt" in candidates


def test_staged_delete_of_a_file_no_longer_in_the_worktree(tmp_path):
    repo = _repo(tmp_path, _BASE)
    _git(repo, "rm", "-q", "r/deep/y.txt")
    changed, _ = _check(repo, [], ["r"], ["d"])
    assert changed == {"r/deep/y.txt"}


def test_staged_add_is_changed(tmp_path):
    repo = _repo(tmp_path, _BASE)
    _write(repo, "r/new.txt", "n\n")
    time.sleep(0.05)
    _git(repo, "add", "r/new.txt")
    time.sleep(0.05)
    changed, _ = _check(repo, [], ["r"], [])
    assert changed == {"r/new.txt"}


def test_untracked_files_under_roots_globs_and_literals(tmp_path):
    repo = _repo(tmp_path, _BASE)
    _write(repo, "r/deep/new.txt")
    _write(repo, "d/new.txt")
    _write(repo, "d/sub/nested-untracked.txt")
    _write(repo, "solo.txt")
    _, candidates = _check(repo, ["solo.txt", "nothere.txt"], ["r"], ["d"])
    assert "r/deep/new.txt" in candidates
    assert "d/new.txt" in candidates
    assert "solo.txt" in candidates
    assert "d/sub/nested-untracked.txt" not in candidates
    assert "nothere.txt" not in candidates


def test_root_glob_lists_root_level_files_and_skips_dot_git(tmp_path):
    repo = _repo(tmp_path, _BASE)
    _write(repo, "root-new.txt")
    _, candidates = _check(repo, [], [], [""])
    assert "root-new.txt" in candidates
    assert not any(c.startswith(".git") for c in candidates)


def test_untracked_directories_are_surfaced_with_a_trailing_slash(tmp_path):
    repo = _repo(tmp_path, _BASE)
    _write(repo, "r/fresh/inner/f.txt")
    _write(repo, "d/sub/fresh/g.txt")
    _, candidates = _check(repo, [], ["r"], ["d"])
    assert "r/fresh/" in candidates and "r/fresh/inner/" in candidates
    assert "d/sub/fresh/" in candidates
    assert "r/deep/" not in candidates


def test_crlf_touch_under_autocrlf_true_is_clean(tmp_path):
    repo = _repo(tmp_path, _BASE)
    _write(repo, "crlf.txt", "q\r\n")
    changed, _ = _check(repo, ["crlf.txt"], [], [])
    assert changed == set()


def test_crlf_rewrite_that_changes_the_size_is_changed_like_git(tmp_path):
    repo = _repo(tmp_path, _BASE)
    _write(repo, "a.txt", "a\r\n")
    changed, _ = _check(repo, ["a.txt"], [], [])
    assert changed == {"a.txt"}


def test_content_change_with_equal_size_under_autocrlf_true_is_changed(tmp_path):
    repo = _repo(tmp_path, _BASE)
    _write(repo, "a.txt", "z\n")
    changed, _ = _check(repo, ["a.txt"], [], [])
    assert changed == {"a.txt"}


def test_stat_mismatch_with_autocrlf_unset_declines(tmp_path):
    repo = _repo(tmp_path, _BASE, autocrlf="false")
    assert scoped_changed(repo, ["a.txt"], [], []) == (set(), [])
    _write(repo, "a.txt", "z\n")
    assert scoped_changed(repo, ["a.txt"], [], []) is None
    _write(repo, "a.txt", "longer than before\n")
    assert scoped_changed(repo, ["a.txt"], [], []) == ({"a.txt"}, [])


def test_skip_worktree_entry_gates(tmp_path):
    repo = _repo(tmp_path, _BASE)
    _git(repo, "update-index", "--skip-worktree", "d/b.txt")
    assert scoped_changed(repo, [], [], ["d"]) is None
    assert scoped_changed(repo, ["a.txt"], [], []) is not None


def test_assume_unchanged_entry_gates(tmp_path):
    repo = _repo(tmp_path, _BASE)
    _git(repo, "update-index", "--assume-unchanged", "d/b.txt")
    assert scoped_changed(repo, ["d/b.txt"], [], []) is None


def test_intent_to_add_entry_gates(tmp_path):
    repo = _repo(tmp_path, _BASE)
    _write(repo, "r/ita.txt")
    _git(repo, "add", "-N", "r/ita.txt")
    assert scoped_changed(repo, [], ["r"], []) is None


def test_unmerged_entry_gates(tmp_path):
    repo = _repo(tmp_path, _BASE)
    shas = [
        _git(repo, "hash-object", "-w", "--stdin", stdin=t).decode().strip()
        for t in (b"1\n", b"2\n", b"3\n")
    ]
    _git(repo, "update-index", "--force-remove", "r/x.txt")
    info = "".join(f"100644 {sha} {stage}\tr/x.txt\n" for stage, sha in enumerate(shas, 1))
    _git(repo, "update-index", "--index-info", stdin=info.encode())
    assert scoped_changed(repo, [], ["r"], []) is None


def test_gitlink_entry_gates(tmp_path):
    repo = _repo(tmp_path, _BASE)
    sha = _git(repo, "rev-parse", "HEAD").decode().strip()
    _git(repo, "update-index", "--add", "--cacheinfo", f"160000,{sha},r/sub")
    assert scoped_changed(repo, [], ["r"], []) is None


def test_nested_dot_git_gates(tmp_path):
    repo = _repo(tmp_path, _BASE)
    (repo / "r" / "nested" / ".git").mkdir(parents=True)
    assert scoped_changed(repo, [], ["r"], []) is None


def test_racily_clean_entry_gates(tmp_path):
    repo = _repo(tmp_path, _BASE)
    future = time.time() + 3600
    os.utime(repo / "a.txt", (future, future))
    _git(repo, "update-index", "--refresh")
    assert scoped_changed(repo, ["a.txt"], [], []) is None


class _OsView:
    """Stand-in for the module's `os` that overrides only `name`."""

    def __init__(self, name: str) -> None:
        self.name = name

    def __getattr__(self, attr):
        return getattr(os, attr)


def test_executable_bit_disagreement_gates(tmp_path, monkeypatch):
    from coordinator_core.git import scoped_changed as mod

    monkeypatch.setattr(mod, "os", _OsView("posix"))
    repo = _repo(tmp_path, _BASE)
    _git(repo, "update-index", "--chmod=+x", "a.txt")
    assert scoped_changed(repo, ["a.txt"], [], []) is None


def test_win32_gates_on_an_exec_index_entry_and_not_on_a_plain_one(tmp_path, monkeypatch):
    import types

    repo = _repo(tmp_path, _BASE)
    monkeypatch.setattr(
        "coordinator_core.git.scoped_changed.sys", types.SimpleNamespace(platform="win32")
    )
    assert scoped_changed(repo, ["a.txt"], [], []) is not None
    _git(repo, "update-index", "--chmod=+x", "a.txt")
    assert scoped_changed(repo, ["a.txt"], [], []) is None


def test_literal_naming_a_directory_gates(tmp_path):
    repo = _repo(tmp_path, _BASE)
    assert scoped_changed(repo, ["r"], [], []) is None
    assert scoped_changed(repo, ["r/deep"], [], []) is None


def test_unborn_head_gates(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _write(repo, "a.txt")
    assert scoped_changed(repo, ["a.txt"], [], []) is None


def test_unsafe_scope_paths_gate(tmp_path):
    repo = _repo(tmp_path, _BASE)
    assert scoped_changed(repo, ["../outside"], [], []) is None
    assert scoped_changed(repo, [], ["/abs"], []) is None
    assert scoped_changed(repo, [".git/config"], [], []) is None


def test_directory_walk_budget_gates(tmp_path):
    repo = _repo(tmp_path, _BASE)
    assert scoped_changed_detail(repo, [], ["r"], [], max_dirs=1) is None
    assert scoped_changed_detail(repo, [], ["r"], [], max_dirs=2) is not None


def test_symlink_in_worktree_gates(tmp_path):
    repo = _repo(tmp_path, _BASE)
    try:
        os.symlink(repo / "a.txt", repo / "r" / "link.txt")
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    assert scoped_changed(repo, [], ["r"], []) is None


def test_case_only_collision_with_the_index_gates(tmp_path):
    repo = _repo(tmp_path, _BASE)
    _write(repo, "d/B.TXT")
    if (repo / "d" / "b.txt").read_text() == "x\n":
        pytest.skip("case-insensitive filesystem cannot hold both names")
    assert scoped_changed(repo, [], [], ["d"]) is None


def test_descend_false_surfaces_the_directory_without_entering_it(tmp_path):
    repo = _repo(tmp_path, _BASE)
    _write(repo, "r/big/one.txt")
    _write(repo, "r/big/two/three.txt")
    entered = []

    def descend(rel: str) -> bool:
        entered.append(rel)
        return False

    res = scoped_changed_detail(repo, [], ["r"], [], descend=descend)
    assert res is not None
    assert "r/big/" in res.candidates
    assert not any(c.startswith("r/big/") and c != "r/big/" for c in res.candidates)
    assert entered == ["r/big"]


def test_tracked_directories_are_reported_and_always_entered(tmp_path):
    repo = _repo(tmp_path, _BASE)
    _write(repo, "r/deep/inside.txt")
    res = scoped_changed_detail(repo, [], ["r"], [], descend=lambda rel: False)
    assert res is not None
    assert set(res.tracked_dirs) == {"r", "r/deep"}
    assert "r/deep/inside.txt" in res.candidates


def test_extra_prefixes_load_ignored_top_directories_into_tracked_dirs(tmp_path):
    repo = _repo(tmp_path, _BASE)
    res = scoped_changed_detail(repo, ["a.txt"], [], [], extra_prefixes=["other"])
    assert res is not None
    assert "other" in res.tracked_dirs
    assert res.changed == set()


def test_combined_ask_shaped_request_matches_git(tmp_path):
    repo = _repo(tmp_path, _BASE)
    _write(repo, "a.txt", "modified!\n")
    _write(repo, "r/deep/y.txt", "yyyy\n")
    (repo / "r" / "x.txt").unlink()
    _write(repo, "r/run/new.txt")
    _write(repo, "d/new.txt")
    _write(repo, "plan.md")
    _git(repo, "rm", "-q", "other/z.txt")
    _check(
        repo,
        ["a.txt", "plan.md", "other/z.txt", "d/b.txt"],
        ["r"],
        ["d", ""],
    )

"""_git_tree_facts toplevel/show_prefix/head_ref/head_commit equal the git commands, with no spawn."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from coordinator_core.bash_guards import _git_tree_facts as facts
from coordinator_core.git import repo_root

pytestmark = pytest.mark.complete

_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e",
}


def _git(cwd: Path, *args: str) -> str:
    out = subprocess.run(
        ["git", *args], cwd=str(cwd), env=_ENV, capture_output=True, text=True, check=True
    )
    return out.stdout.strip()


def _norm(p: str) -> str:
    return os.path.normcase(os.path.realpath(p))


@pytest.fixture(autouse=True)
def _fresh():
    facts.reset()
    repo_root.clear_memo()
    yield
    facts.reset()
    repo_root.clear_memo()


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "plain"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    (root / "sub" / "deep").mkdir(parents=True)
    (root / "a.txt").write_text("a")
    _git(root, "add", "a.txt")
    _git(root, "commit", "-q", "-m", "one")
    return root.resolve()


def _assert_equal_to_git(cwd: Path) -> None:
    facts.reset()
    assert _norm(facts.toplevel(str(cwd))) == _norm(_git(cwd, "rev-parse", "--show-toplevel"))
    assert facts.show_prefix(str(cwd)) == _git(cwd, "rev-parse", "--show-prefix")
    assert facts.head_ref(str(cwd)) == _git(cwd, "symbolic-ref", "HEAD")
    assert facts.head_commit(str(cwd)) == _git(cwd, "rev-parse", "HEAD")


def test_plain_repo_root_and_subdirs(repo):
    _assert_equal_to_git(repo)
    _assert_equal_to_git(repo / "sub")
    _assert_equal_to_git(repo / "sub" / "deep")


def test_gitfile_worktree(repo, tmp_path):
    wt = tmp_path / "wt"
    _git(repo, "worktree", "add", "-q", "-b", "side", str(wt))
    (wt / "x").mkdir()
    _assert_equal_to_git(wt.resolve())
    _assert_equal_to_git(wt.resolve() / "x")
    assert facts.head_ref(str(wt)) == "refs/heads/side"


def test_unborn_head(tmp_path):
    root = tmp_path / "unborn"
    root.mkdir()
    _git(root, "init", "-q", "-b", "trunk")
    root = root.resolve()
    assert facts.toplevel(str(root)) is not None
    assert facts.show_prefix(str(root)) == ""
    assert facts.head_ref(str(root)) == _git(root, "symbolic-ref", "HEAD")
    assert facts.head_commit(str(root)) is None


def test_packed_refs_only(repo):
    _git(repo, "pack-refs", "--all", "--prune")
    assert not (repo / ".git" / "refs" / "heads" / "main").exists()
    _assert_equal_to_git(repo)


def test_detached_head_ref_is_none_commit_is_sha(repo):
    sha = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", "--detach")
    assert facts.head_ref(str(repo)) is None
    assert facts.head_commit(str(repo)) == sha


def test_non_repo_is_none(tmp_path):
    plain = tmp_path / "nothing"
    plain.mkdir()
    for fn in (facts.toplevel, facts.show_prefix, facts.head_ref, facts.head_commit):
        assert fn(str(plain)) is None


def test_bare_repo_is_none(tmp_path):
    bare = tmp_path / "bare.git"
    bare.mkdir()
    _git(bare, "init", "-q", "--bare")
    for fn in (facts.toplevel, facts.show_prefix, facts.head_ref, facts.head_commit):
        assert fn(str(bare)) is None


def test_inside_dot_git_has_no_prefix(repo):
    assert facts.show_prefix(str(repo / ".git")) is None


def test_no_git_process_spawned(repo, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("git spawned")

    monkeypatch.setattr(subprocess, "Popen", boom)
    monkeypatch.setattr(subprocess, "run", boom)
    cwd = str(repo / "sub")
    assert facts.toplevel(cwd) is not None
    assert facts.show_prefix(cwd) == "sub/"
    assert facts.head_ref(cwd) == "refs/heads/main"
    assert len(facts.head_commit(cwd)) == 40


def test_reader_exception_returns_none(repo, monkeypatch):
    from coordinator_core.git import git_state

    def boom(*a, **k):
        raise RuntimeError("unreadable")

    monkeypatch.setattr(git_state, "head_sha", boom)
    monkeypatch.setattr(git_state, "head_branch", boom)
    assert facts.head_commit(str(repo)) is None
    assert facts.head_ref(str(repo)) is None
    assert facts.toplevel(str(repo)) is not None


def test_non_sha_head_content_is_none(repo, monkeypatch):
    from coordinator_core.git import git_state

    monkeypatch.setattr(git_state, "head_sha", lambda *a, **k: "z" * 40)
    assert facts.head_commit(str(repo)) is None
    facts.reset()
    monkeypatch.setattr(git_state, "head_sha", lambda *a, **k: "a" * 64)
    assert facts.head_commit(str(repo)) is None


def test_case_folded_prefix_on_case_insensitive_repo(repo, monkeypatch):
    from coordinator_core.bash_guards import _declared_delete_license as ddl

    monkeypatch.setattr(repo_root, "_resolve_cwd", lambda cwd: str(repo / "SUB"))
    monkeypatch.setattr(ddl, "case_insensitive", lambda p: True)
    assert facts.show_prefix(str(repo)) == "SUB/"
    facts.reset()
    monkeypatch.setattr(ddl, "case_insensitive", lambda p: False)
    monkeypatch.setattr(repo_root, "show_toplevel", lambda cwd=None: str(repo).swapcase())
    if os.name != "nt":
        assert facts.show_prefix(str(repo)) is None


def _git_staged(cwd: Path) -> list:
    out = _git(cwd, "diff", "--cached", "--no-renames", "--name-status")
    return out.splitlines() if out else []


def _staged_repo(tmp_path):
    root = tmp_path / "st"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    for rel in ("a.txt", "d/x.txt", "d/e/y.txt", "d/e/z.txt", "keep/k.txt", "gone/g.txt"):
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(rel)
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "base")
    return root.resolve()


def test_staged_clean_is_empty_list(tmp_path):
    root = _staged_repo(tmp_path)
    assert facts.staged_name_status(str(root)) == []


def test_staged_add_modify_delete_equals_git(tmp_path):
    root = _staged_repo(tmp_path)
    (root / "a.txt").write_text("changed")
    (root / "d" / "e" / "y.txt").write_text("changed")
    (root / "new" / "n").mkdir(parents=True)
    (root / "new" / "n" / "f.txt").write_text("f")
    (root / "top.txt").write_text("t")
    _git(root, "rm", "-q", "-r", "gone")
    _git(root, "rm", "-q", "d/e/z.txt")
    _git(root, "add", "-A")
    got = facts.staged_name_status(str(root))
    assert got == _git_staged(root)
    assert {line[0] for line in got} == {"A", "M", "D"}


def test_staged_unstaged_work_is_not_reported(tmp_path):
    root = _staged_repo(tmp_path)
    (root / "a.txt").write_text("unstaged only")
    assert facts.staged_name_status(str(root)) == []


def test_staged_mode_change_is_modified(tmp_path):
    root = _staged_repo(tmp_path)
    _git(root, "update-index", "--chmod=+x", "a.txt")
    assert facts.staged_name_status(str(root)) == _git_staged(root) == ["M\ta.txt"]


def test_staged_file_replaced_by_dir_and_back(tmp_path):
    root = _staged_repo(tmp_path)
    (root / "a.txt").unlink()
    (root / "a.txt").mkdir()
    (root / "a.txt" / "in.txt").write_text("i")
    _git(root, "add", "-A")
    assert facts.staged_name_status(str(root)) == _git_staged(root)
    facts.reset()
    _git(root, "reset", "-q", "--hard")
    (root / "d").rename(root / "d_old")
    (root / "d").write_text("now a file")
    _git(root, "add", "-A")
    assert facts.staged_name_status(str(root)) == _git_staged(root)


def test_staged_symlink_type_change_is_none(tmp_path):
    root = _staged_repo(tmp_path)
    blob = subprocess.run(
        ["git", "hash-object", "-w", "--stdin"], cwd=str(root), env=_ENV,
        input="a.txt", capture_output=True, text=True, check=True,
    ).stdout.strip()
    _git(root, "update-index", "--cacheinfo", f"120000,{blob},a.txt")
    assert facts.staged_name_status(str(root)) is None


def test_staged_non_ascii_path_is_none(tmp_path):
    root = _staged_repo(tmp_path)
    (root / "café.txt").write_text("c")
    _git(root, "add", "-A")
    assert facts.staged_name_status(str(root)) is None


def test_staged_unborn_head_is_none(tmp_path):
    root = tmp_path / "unb"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    (root / "f").write_text("f")
    _git(root, "add", "f")
    assert facts.staged_name_status(str(root.resolve())) is None


def test_staged_unmerged_index_is_none(tmp_path):
    root = _staged_repo(tmp_path)
    _git(root, "checkout", "-q", "-b", "other")
    (root / "a.txt").write_text("other")
    _git(root, "commit", "-q", "-am", "o")
    _git(root, "checkout", "-q", "main")
    (root / "a.txt").write_text("main")
    _git(root, "commit", "-q", "-am", "m")
    subprocess.run(["git", "merge", "other"], cwd=str(root), env=_ENV, capture_output=True)
    assert facts.staged_name_status(str(root)) is None


def test_staged_missing_head_tree_object_is_none(tmp_path):
    root = _staged_repo(tmp_path)
    (root / "d" / "x.txt").write_text("changed")
    _git(root, "add", "-A")
    tree = _git(root, "rev-parse", "HEAD:d")
    loose = root / ".git" / "objects" / tree[:2] / tree[2:]
    assert loose.exists()
    os.chmod(loose, 0o644)
    loose.unlink()
    assert facts.staged_name_status(str(root)) is None


def test_staged_sha256_repo_is_none(tmp_path):
    root = tmp_path / "s256"
    root.mkdir()
    probe = subprocess.run(
        ["git", "init", "-q", "--object-format=sha256", "-b", "main"],
        cwd=str(root), env=_ENV, capture_output=True,
    )
    if probe.returncode != 0:
        pytest.skip("git lacks sha256 support")
    (root / "f").write_text("f")
    _git(root, "add", "f")
    _git(root, "commit", "-q", "-m", "c")
    (root / "f").write_text("g")
    _git(root, "add", "f")
    assert facts.staged_name_status(str(root.resolve())) is None


def test_staged_no_git_process_spawned(tmp_path, monkeypatch):
    root = _staged_repo(tmp_path)
    (root / "a.txt").write_text("changed")
    _git(root, "add", "-A")

    def boom(*a, **k):
        raise AssertionError("git spawned")

    monkeypatch.setattr(subprocess, "Popen", boom)
    monkeypatch.setattr(subprocess, "run", boom)
    assert facts.staged_name_status(str(root)) == ["M\ta.txt"]


def test_staged_packed_head_objects(tmp_path):
    root = _staged_repo(tmp_path)
    _git(root, "gc", "-q", "--prune=now")
    (root / "d" / "e" / "y.txt").write_text("changed")
    _git(root, "add", "-A")
    assert facts.staged_name_status(str(root)) == _git_staged(root)


def test_memoised_per_dispatch_and_reset(repo):
    cwd = str(repo)
    first = facts.head_commit(cwd)
    (repo / "b.txt").write_text("b")
    _git(repo, "add", "b.txt")
    _git(repo, "commit", "-q", "-m", "two")
    assert facts.head_commit(cwd) == first
    facts.reset()
    assert facts.head_commit(cwd) == _git(repo, "rev-parse", "HEAD") != first


class TestOversizedIndexIsOneSpawn:
    """An index over INPROCESS_INDEX_BYTES_MAX is answered by one memoised git spawn, never parsed."""

    @staticmethod
    def _changes(root):
        (root / "a.txt").write_text("changed")
        (root / "new.txt").write_text("n")
        _git(root, "rm", "-q", "d/e/z.txt")
        _git(root, "mv", "keep/k.txt", "keep/renamed.txt")
        _git(root, "add", "-A")

    def test_spawn_rows_equal_git_and_in_process(self, tmp_path, monkeypatch):
        root = _staged_repo(tmp_path)
        self._changes(root)
        in_process = facts.staged_name_status(str(root))
        facts.reset()
        monkeypatch.setattr(facts, "INPROCESS_INDEX_BYTES_MAX", 0)
        from coordinator_core.git import git_state

        monkeypatch.setattr(
            git_state, "read_index", lambda *a, **k: (_ for _ in ()).throw(AssertionError("parsed"))
        )
        assert facts.staged_name_status(str(root)) == in_process == _git_staged(root)
        lines = facts.staged_name_status_renames(str(root))
        assert any(line.startswith("R") and line.endswith("keep/k.txt\tkeep/renamed.txt") for line in lines)

    def test_one_spawn_serves_both_answers(self, tmp_path, monkeypatch):
        from coordinator_core.git import run as git_run

        root = _staged_repo(tmp_path)
        self._changes(root)
        monkeypatch.setattr(facts, "INPROCESS_INDEX_BYTES_MAX", 0)
        calls = []
        real = git_run.run_git
        monkeypatch.setattr(git_run, "run_git", lambda *a, **k: calls.append(a) or real(*a, **k))
        facts.staged_name_status(str(root))
        facts.staged_name_status_renames(str(root))
        facts.staged_name_status(str(root))
        assert len(calls) == 1

    def test_small_index_is_not_renames_answered(self, tmp_path):
        root = _staged_repo(tmp_path)
        self._changes(root)
        assert facts.staged_name_status_renames(str(root)) is None

    def test_spent_budget_declines_unspawned(self, tmp_path, monkeypatch):
        from coordinator_core.bash_guards import _dc_git_probe
        from coordinator_core.git import run as git_run

        root = _staged_repo(tmp_path)
        self._changes(root)
        monkeypatch.setattr(facts, "INPROCESS_INDEX_BYTES_MAX", 0)
        monkeypatch.setattr(_dc_git_probe, "_git_probe_budget_spent", lambda: True)
        monkeypatch.setattr(
            git_run, "run_git", lambda *a, **k: (_ for _ in ()).throw(AssertionError("spawned"))
        )
        assert facts.staged_name_status(str(root)) is None
        assert _dc_git_probe._git_probe_last_failure.get()

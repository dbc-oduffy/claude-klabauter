"""commit_resolution: one live spawn per (index identity, HEAD); restaging, a
second index and a moved HEAD each miss the cache."""

import os
import subprocess

import pytest

from coordinator_core.hooks import commit_resolution as cr


def _git(repo, *args, env=None):
    full_env = dict(os.environ)
    full_env.pop("GIT_INDEX_FILE", None)
    full_env.update(env or {})
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        env=full_env,
    )


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.delenv("GIT_INDEX_FILE", raising=False)
    monkeypatch.delenv("GIT_WORK_TREE", raising=False)
    root = tmp_path / "r"
    root.mkdir()
    _git(root, "init", "-q")
    (root / "base.txt").write_text("base\n")
    _git(root, "add", "base.txt")
    _git(root, "commit", "-q", "-m", "base")
    monkeypatch.chdir(root)
    return root


@pytest.fixture
def spawns(monkeypatch):
    log = []
    real = cr._run_git

    def counting(args, timeout):
        log.append(list(args))
        return real(args, timeout)

    monkeypatch.setattr(cr, "_run_git", counting)
    return log


def test_second_call_with_unchanged_index_spawns_zero(repo, spawns):
    (repo / "a.txt").write_text("a\n")
    _git(repo, "add", "a.txt")
    git_dir = str(repo / ".git")
    first = cr.staged_raw(git_dir)
    second = cr.staged_raw(git_dir)
    assert first == second
    assert [(r.status, r.path) for r in first] == [("A", "a.txt")]
    assert spawns == [["diff", "--cached", "--raw", "-z", "-M"]]
    assert cr.names(git_dir) == ["a.txt"]
    assert len(spawns) == 1


def test_restaged_index_spawns_again(repo, spawns):
    git_dir = str(repo / ".git")
    (repo / "a.txt").write_text("a\n")
    _git(repo, "add", "a.txt")
    assert cr.names(git_dir) == ["a.txt"]
    (repo / "b.txt").write_text("b\n")
    _git(repo, "add", "b.txt")
    assert sorted(cr.names(git_dir)) == ["a.txt", "b.txt"]
    assert len(spawns) == 2


def test_git_index_file_yields_its_own_resolution(repo, spawns, monkeypatch):
    git_dir = str(repo / ".git")
    (repo / "a.txt").write_text("a\n")
    _git(repo, "add", "a.txt")
    assert cr.names(git_dir) == ["a.txt"]

    temp_index = str(repo / ".git" / "next-index-1.lock")
    (repo / "c.txt").write_text("c\n")
    _git(repo, "read-tree", "HEAD", env={"GIT_INDEX_FILE": temp_index})
    _git(repo, "add", "c.txt", env={"GIT_INDEX_FILE": temp_index})
    monkeypatch.setenv("GIT_INDEX_FILE", temp_index)
    assert cr.names(git_dir) == ["c.txt"]
    assert cr.names(git_dir) == ["c.txt"]
    assert len(spawns) == 2

    monkeypatch.delenv("GIT_INDEX_FILE")
    assert cr.names(git_dir) == ["a.txt"]
    assert len(spawns) == 2, "the shared index's entry survived the temp index's write"


def test_moved_head_with_unchanged_index_misses(repo, spawns):
    git_dir = str(repo / ".git")
    (repo / "a.txt").write_text("a\n")
    _git(repo, "add", "a.txt")
    assert cr.names(git_dir) == ["a.txt"]
    index_before = os.stat(repo / ".git" / "index")
    _git(repo, "commit", "-q", "-m", "a")
    # Same index content as a fresh read-tree would give, but HEAD moved.
    assert cr.names(git_dir) == []
    assert len(spawns) == 2
    assert os.stat(repo / ".git" / "index").st_mtime_ns >= index_before.st_mtime_ns


def test_head_moved_alone_changes_key(repo):
    git_dir = str(repo / ".git")
    key_a, _ = cr._cache_key(git_dir)
    (repo / "a.txt").write_text("a\n")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-q", "-m", "a")
    key_b, _ = cr._cache_key(git_dir)
    assert key_a[-1] != key_b[-1]


def test_packed_ref_head_resolves(repo):
    git_dir = str(repo / ".git")
    loose_oid = cr._head_oid(git_dir)
    _git(repo, "pack-refs", "--all")
    assert cr._head_oid(git_dir) == loose_oid
    assert len(loose_oid) in (40, 64)


def test_unresolvable_head_is_a_miss_not_a_guess(tmp_path, monkeypatch):
    monkeypatch.delenv("GIT_INDEX_FILE", raising=False)
    root = tmp_path / "u"
    root.mkdir()
    _git(root, "init", "-q")
    (root / "a.txt").write_text("a\n")
    _git(root, "add", "a.txt")
    monkeypatch.chdir(root)
    assert cr._head_oid(str(root / ".git")) is None
    assert cr._cache_key(str(root / ".git")) is None
    assert cr.names(str(root / ".git")) == ["a.txt"]
    assert not [n for n in os.listdir(root / ".git") if n.startswith(cr._CACHE_PREFIX)]


def test_corrupt_cache_falls_back_to_one_live_spawn(repo, spawns):
    git_dir = str(repo / ".git")
    (repo / "a.txt").write_text("a\n")
    _git(repo, "add", "a.txt")
    cr.names(git_dir)
    (cache,) = [n for n in os.listdir(git_dir) if n.startswith(cr._CACHE_PREFIX)]
    (repo / ".git" / cache).write_text("{not json")
    assert cr.names(git_dir) == ["a.txt"]
    assert len(spawns) == 2


def test_rename_records_keep_phantom_delete_semantics(repo):
    git_dir = str(repo / ".git")
    _git(repo, "mv", "base.txt", "moved.txt")
    recs = cr.staged_raw(git_dir)
    assert [(r.status[:1], r.orig_path, r.path) for r in recs] == [("R", "base.txt", "moved.txt")]
    assert cr.name_status(git_dir) == [("D", "base.txt"), ("A", "moved.txt")]
    assert cr.names(git_dir) == ["base.txt", "moved.txt"]
    assert cr.names_filtered(git_dir, "AMR") == ["moved.txt"]
    assert cr.names_filtered(git_dir, "D") == []


def test_names_filtered_excludes_deletions(repo):
    git_dir = str(repo / ".git")
    _git(repo, "rm", "-q", "base.txt")
    (repo / "n.txt").write_text("n\n")
    _git(repo, "add", "n.txt")
    assert cr.names_filtered(git_dir, "AMR") == ["n.txt"]
    assert cr.names_filtered(git_dir, "D") == ["base.txt"]


def test_stale_sibling_cache_for_a_gone_index_is_pruned(repo):
    git_dir = str(repo / ".git")
    gone = repo / ".git" / "next-index-9.lock"
    (repo / "a.txt").write_text("a\n")
    _git(repo, "read-tree", "HEAD", env={"GIT_INDEX_FILE": str(gone)})
    _git(repo, "add", "a.txt", env={"GIT_INDEX_FILE": str(gone)})
    os.environ["GIT_INDEX_FILE"] = str(gone)
    try:
        cr.names(git_dir)
    finally:
        del os.environ["GIT_INDEX_FILE"]
    assert len([n for n in os.listdir(git_dir) if n.startswith(cr._CACHE_PREFIX)]) == 1
    gone.unlink()
    _git(repo, "add", "a.txt")
    cr.names(git_dir)
    assert len([n for n in os.listdir(git_dir) if n.startswith(cr._CACHE_PREFIX)]) == 1


def test_toplevel_zero_spawn_for_plain_git_dir(repo, spawns):
    assert cr.toplevel(str(repo / ".git")) == os.path.realpath(repo)
    assert cr.toplevel(".git") == os.path.realpath(repo)
    assert spawns == []


def test_toplevel_one_spawn_then_cached_for_linked_worktree(repo, spawns, monkeypatch):
    wt = repo.parent / "wt"
    _git(repo, "worktree", "add", "-q", str(wt), "-b", "side")
    monkeypatch.chdir(wt)
    git_dir = os.path.join(repo, ".git", "worktrees", "wt")
    top1 = cr.toplevel(git_dir)
    top2 = cr.toplevel(git_dir)
    assert os.path.realpath(top1) == os.path.realpath(wt) == os.path.realpath(top2)
    assert spawns == [["rev-parse", "--show-toplevel"]]


def test_live_spawn_failure_raises_resolution_error(tmp_path, monkeypatch):
    monkeypatch.delenv("GIT_INDEX_FILE", raising=False)
    monkeypatch.chdir(tmp_path)
    with pytest.raises(cr.ResolutionError):
        cr.raw_text(str(tmp_path / "nope"))

"""`commit_paths` work shape is path-count independent: 0 spawns, one index write, one tree build."""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest

from coordinator_core.git import commit as commit_mod
from coordinator_core.git import index_write
from coordinator_core.git.commit import commit_paths

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_OVERHEAD_BUDGET_MS = 350


def _git(root: Path, *args: str) -> str:
    env = dict(
        os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1",
        GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t",
    )
    done = subprocess.run(
        ["git", *args], cwd=root, env=env, capture_output=True, creationflags=_NO_WINDOW, check=True
    )
    return done.stdout.decode().strip()


def _repo(tmp_path: Path, total: int) -> tuple[Path, list[str]]:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "core.autocrlf", "false")
    d = root / "state" / "handoffs"
    d.mkdir(parents=True)
    names = []
    for i in range(total):
        (d / f"baton-{i:04d}.md").write_bytes(f"---\nid: {i}\n---\nbody\n".encode())
        names.append(f"state/handoffs/baton-{i:04d}.md")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "seed")
    return root, names


def _edit(root: Path, names: list[str]) -> None:
    for n in names:
        p = root / n
        p.write_bytes(p.read_bytes() + b"owner_session: x\n")


def _counted(monkeypatch, owner, attr: str) -> list:
    calls: list = []
    real = getattr(owner, attr)
    monkeypatch.setattr(owner, attr, lambda *a, **k: calls.append(1) or real(*a, **k))
    return calls


def test_809_paths_one_tree_build_one_index_write_zero_spawns(tmp_path, monkeypatch):
    root, names = _repo(tmp_path, 809)
    _edit(root, names)
    spawns: list = []
    real_popen = subprocess.Popen
    monkeypatch.setattr(
        subprocess, "Popen", lambda *a, **k: spawns.append(a) or real_popen(*a, **k)
    )
    trees = _counted(monkeypatch, commit_mod, "_rewrite_head_spine")
    prepares = _counted(monkeypatch, index_write, "prepare_splice")
    splices = _counted(monkeypatch, index_write, "splice_index")
    parses = _counted(monkeypatch, commit_mod, "parse_index_identity")
    refs = _counted(monkeypatch, commit_mod, "cas_ref")

    read_start = time.process_time()
    for n in names:
        (root / n).read_bytes()
    read_ms = (time.process_time() - read_start) * 1000
    start = time.process_time()
    out = commit_paths(root, names, "bulk")
    elapsed_ms = (time.process_time() - start) * 1000 - read_ms

    assert out.sha
    assert spawns == []
    assert (len(trees), len(prepares), len(refs)) == (1, 1, 1)
    assert len(splices) == 0
    assert len(parses) <= 2
    assert elapsed_ms < _OVERHEAD_BUDGET_MS, (elapsed_ms, read_ms)
    monkeypatch.undo()
    assert _git(root, "status", "--porcelain", "--untracked-files=no") == ""
    assert _git(root, "rev-list", "--count", "HEAD") == "2"


def _timed_commit(root: Path, names: list[str], message: str) -> float:
    start = time.process_time()
    for n in names:
        (root / n).read_bytes()
    read_ms = (time.process_time() - start) * 1000
    start = time.process_time()
    commit_paths(root, names, message)
    return (time.process_time() - start) * 1000 - read_ms


def test_per_path_marginal_overhead_is_bounded(tmp_path):
    """Overhead beyond reading the files grows under 0.3ms per extra path: no per-path object read."""
    root, names = _repo(tmp_path, 809)
    _edit(root, names[:10])
    few_ms = _timed_commit(root, names[:10], "few")
    _edit(root, names[10:])
    many_ms = _timed_commit(root, names[10:], "many")
    assert (many_ms - few_ms) / 799 < 0.3, (few_ms, many_ms)


def test_changed_worktree_after_hash_is_not_claimed_head_equal(tmp_path, monkeypatch):
    """A path edited after its blob was hashed leaves the fingerprint fast path and keeps its edit."""
    root, names = _repo(tmp_path, 3)
    _edit(root, names)
    target = root / names[0]
    real = commit_mod._prepare_index_splice

    def edit_during_commit(*a, **k):
        target.write_bytes(target.read_bytes() + b"peer edit\n")
        return real(*a, **k)

    monkeypatch.setattr(commit_mod, "_prepare_index_splice", edit_during_commit)
    commit_paths(root, names, "race")
    monkeypatch.undo()
    assert target.read_bytes().endswith(b"peer edit\n")
    assert _git(root, "status", "--porcelain", "--untracked-files=no").strip().endswith(names[0])


def _count_worktree_reads(monkeypatch, root: Path) -> list:
    reads: list = []
    real = Path.read_bytes

    def counting(self):
        if str(self).startswith(str(root / "state")):
            reads.append(self.name)
        return real(self)

    monkeypatch.setattr(Path, "read_bytes", counting)
    return reads


def _written(root: Path, names: list[str]) -> dict:
    return {n: ((root / n).read_bytes(), (root / n).stat().st_mtime_ns) for n in names}


def test_written_content_matching_stat_skips_the_worktree_read(tmp_path, monkeypatch):
    root, names = _repo(tmp_path, 20)
    _edit(root, names)
    written = _written(root, names)
    reads = _count_worktree_reads(monkeypatch, root)
    out = commit_paths(root, names, "known", written_content=written)
    assert out.sha and reads == []
    monkeypatch.undo()
    assert _git(root, "status", "--porcelain", "--untracked-files=no") == ""


def test_written_content_with_a_moved_mtime_is_reread_and_committed_as_on_disk(tmp_path, monkeypatch):
    root, names = _repo(tmp_path, 3)
    _edit(root, names)
    written = _written(root, names)
    target = root / names[0]
    target.write_bytes(written[names[0]][0].replace(b"x", b"y"))
    reads = _count_worktree_reads(monkeypatch, root)
    commit_paths(root, names, "stale", written_content=written)
    assert reads == [Path(names[0]).name]
    monkeypatch.undo()
    assert _git(root, "show", f"HEAD:{names[0]}").endswith("owner_session: y")
    assert _git(root, "show", f"HEAD:{names[1]}").endswith("owner_session: x")


def test_written_content_with_same_size_and_restored_mtime_commits_the_declared_bytes(tmp_path, monkeypatch):
    """Accepted bound: an equal-size edit with an equal mtime is trusted as the declared content."""
    root, names = _repo(tmp_path, 3)
    _edit(root, names)
    written = _written(root, names)
    target = root / names[0]
    mtime_ns = written[names[0]][1]
    target.write_bytes(written[names[0]][0].replace(b"x", b"y"))
    os.utime(target, ns=(mtime_ns, mtime_ns))
    reads = _count_worktree_reads(monkeypatch, root)
    commit_paths(root, names, "same-tick", written_content=written)
    assert reads == []
    monkeypatch.undo()
    assert _git(root, "show", f"HEAD:{names[0]}").endswith("owner_session: x")


@pytest.mark.parametrize("declared_written", [False, True])
def test_a_directory_pathspec_is_refused_whether_or_not_content_was_declared_written(tmp_path, declared_written):
    from coordinator_core.git.commit import CommitRefused

    root, names = _repo(tmp_path, 2)
    _edit(root, names)
    written = {"state/handoffs": (b"x", 0)} if declared_written else None
    with pytest.raises(CommitRefused, match="is a directory"):
        commit_paths(root, ["state/handoffs"], "dir", written_content=written)
    assert commit_paths(root, names, "files", written_content=_written(root, names)).sha

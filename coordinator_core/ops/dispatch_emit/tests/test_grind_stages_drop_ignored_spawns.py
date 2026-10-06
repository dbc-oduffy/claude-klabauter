"""`ceremony.commit_v2 drop_ignored` against a real git repo; split from
test_grind_stages.py because these tests spawn git."""

from __future__ import annotations

import pytest

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def _drop_ignored_repo(tmp_path):
    import subprocess

    from coordinator_core.win_portability import no_console_creationflags

    def git(*args):
        subprocess.run(
            ["git", *args], cwd=str(repo), capture_output=True, check=True,
            **no_console_creationflags(),
        )

    repo = tmp_path / "r"
    repo.mkdir()
    git("init", "-q", "-b", "work/p")
    git("config", "user.email", "t@local")
    git("config", "user.name", "t")
    git("config", "commit.gpgsign", "false")
    (repo / ".gitignore").write_text("scratch/\n", encoding="utf-8", newline="\n")
    (repo / "keep.txt").write_text("a\n", encoding="utf-8", newline="\n")
    git("add", "-A")
    git("commit", "-q", "-m", "seed")
    (repo / "scratch").mkdir()
    (repo / "scratch" / "evidence.txt").write_text("e\n", encoding="utf-8", newline="\n")
    return repo


def _commit_v2(repo, **params):
    from coordinator_core.ops.ceremony import commit_v2

    return commit_v2._handler(params, repo_root=repo / ".git")


def test_drop_ignored_commits_the_real_change_and_names_the_dropped_path(tmp_path):
    repo = _drop_ignored_repo(tmp_path)
    (repo / "keep.txt").write_text("b\n", encoding="utf-8", newline="\n")
    refused = _commit_v2(repo, paths=["keep.txt", "scratch/evidence.txt"], message="fix")
    assert refused["committed"] is False
    out = _commit_v2(
        repo, paths=["keep.txt", "scratch/evidence.txt"], message="fix", drop_ignored=True
    )
    assert out["committed"] is True, out
    assert out["dropped_ignored"] == ["scratch/evidence.txt"]
    assert any("scratch/evidence.txt" in w for w in out["warnings"])


def test_drop_ignored_with_only_ignored_paths_still_fails_loudly(tmp_path):
    repo = _drop_ignored_repo(tmp_path)
    out = _commit_v2(repo, paths=["scratch/evidence.txt"], message="fix", drop_ignored=True)
    assert out["committed"] is False
    assert "scratch/evidence.txt" in out["error"]
    assert out["dropped_ignored"] == ["scratch/evidence.txt"]

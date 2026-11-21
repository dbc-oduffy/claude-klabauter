"""The commit landing loop resolves `core.autocrlf` once per call, not once per landed path."""

from pathlib import Path

from coordinator_core.git import commit

import pytest
pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

N_PATHS = 40


def _git(root: Path, *args: str) -> None:
    import os
    import subprocess

    env = dict(
        os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1",
        GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t.example", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t.example",
    )
    subprocess.run(
        ["git", "-C", str(root), *args], env=env, check=True, capture_output=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def test_commit_paths_reads_the_config_stack_once_for_many_crlf_paths(tmp_path, monkeypatch):
    from coordinator_core.git import content_hash

    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "core.autocrlf", "true")
    names = [f"f{i}.txt" for i in range(N_PATHS)]
    for name in names:
        (root / name).write_bytes(b"a\nb\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "seed")
    for name in names:
        (root / name).write_bytes(b"a\r\nb\r\nc\r\n")

    reads: list = []
    orig = content_hash._repo_autocrlf_true_uncached

    def counting(r):
        reads.append(r)
        return orig(r)

    monkeypatch.setattr(content_hash, "_repo_autocrlf_true_uncached", counting)
    commit.commit_paths(root, names, "autocrlf once")
    assert len(reads) == 1, (len(reads), N_PATHS)

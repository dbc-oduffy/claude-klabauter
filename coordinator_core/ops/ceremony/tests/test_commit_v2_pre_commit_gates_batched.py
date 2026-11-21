"""`_pre_commit_gates` reads each landed source through `read_source` at most once per landing, however many gates judge it."""

from __future__ import annotations

import os
import subprocess
from collections import Counter
from pathlib import Path

from coordinator_core.git.git_dir import resolve_git_common_dir
from coordinator_core.ops.ceremony import commit_v2
from coordinator_core.ops.ceremony.commit_source import post_commit_reader

import pytest
pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

N_PATHS = 30


def _git(root: Path, *args: str) -> None:
    env = dict(
        os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1",
        GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t.example", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t.example",
    )
    subprocess.run(
        ["git", "-C", str(root), *args], env=env, check=True, capture_output=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def _landing(tmp_path: Path) -> tuple[Path, list]:
    root = tmp_path / "repo"
    (root / "pkg").mkdir(parents=True)
    _git(root, "init", "-q", "-b", "main")
    (root / "README.md").write_text("seed\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "seed")
    rels = []
    for i in range(N_PATHS):
        (root / "pkg" / f"f{i}.py").write_text(f"v = {i}\n", encoding="utf-8")
        rels.append(f"pkg/f{i}.py")
    return root, rels


def test_each_landed_source_is_read_once_across_all_gates(tmp_path):
    root, rels = _landing(tmp_path)
    inner = post_commit_reader(root, resolve_git_common_dir(root), paths=rels, deleted_paths=[])
    reads: Counter = Counter()

    def counting(path: str):
        reads[path] += 1
        return inner(path)

    assert commit_v2._pre_commit_gates(root, rels, [], counting) is None
    assert reads, "no gate read a landed source: the probe is vacuous"
    assert max(reads.values()) == 1, reads.most_common(3)

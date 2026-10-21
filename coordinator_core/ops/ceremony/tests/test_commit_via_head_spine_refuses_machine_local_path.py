"""_commit_via_head_spine refuses an assembled entry under a machine-local prefix."""
from __future__ import annotations

import subprocess

import pytest

from coordinator_core.ops.ceremony import git_native
from coordinator_core.win_portability import no_console_creationflags

from .fixtures.real_git import real_git_repo

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def _head(root):
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(root), capture_output=True, text=True,
        check=True, **no_console_creationflags(),
    ).stdout.strip()


def _commit(root, path):
    msg = root / ".git" / "TEST_MSG"
    msg.write_text("test commit\n", encoding="utf-8")
    return git_native.commit_authored_new_file(path, "body\n", msg, root)


@pytest.mark.parametrize(
    "path",
    [
        ".coordinator-local/subagent-share/s1/x.jsonl",
        ".Coordinator-Local/Subagent-Share/s1/x.jsonl",
    ],
)
def test_a_machine_local_path_is_refused_and_nothing_lands(tmp_path, path):
    root = real_git_repo(tmp_path)
    before = _head(root)
    result = _commit(root, path)
    assert not result.ok
    assert "refused" in result.stderr
    assert "machine-local" in result.stderr
    assert path in result.stderr
    assert _head(root) == before


def test_a_sibling_path_lands(tmp_path):
    root = real_git_repo(tmp_path)
    result = _commit(root, "state/subagent-share/x.md")
    assert result.ok, result.stderr

"""An engine commit refuses a path Windows cannot check out.

`block_illegal_filename` guards only Write/Edit tool calls; an op writes
in-process and commits through `_commit_via_head_spine`, which runs no hook.
Driven through `commit_authored_new_file`, the in-process committer an op creates a file with,
against a real throwaway repo.
"""

from __future__ import annotations

import subprocess

import pytest

from coordinator_core.ops.ceremony import commit_path_legality, git_native
from coordinator_core.win_portability import no_console_creationflags

from .fixtures.real_git import real_git_repo

pytestmark = [pytest.mark.spawns_process]


def _head(root):
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(root), capture_output=True, text=True,
        check=True, **no_console_creationflags(),
    ).stdout.strip()


def _commit(root, path, content):
    msg = root / ".git" / "TEST_MSG"
    msg.write_text("test commit\n", encoding="utf-8")
    return git_native.commit_authored_new_file(path, content, msg, root)


@pytest.mark.parametrize("path", ["docs/plan: phase 2.md", "state/a:b/row.yaml", "notes/trailing."])
def test_an_illegal_path_is_refused_and_nothing_lands(tmp_path, path):
    root = real_git_repo(tmp_path)
    before = _head(root)
    result = _commit(root, path, "body\n")
    assert not result.ok
    assert "Windows cannot check out" in result.stderr
    assert path in result.stderr
    assert _head(root) == before


def test_a_legal_path_lands(tmp_path):
    root = real_git_repo(tmp_path)
    result = _commit(root, "docs/plan-phase-2.md", "body\n")
    assert result.ok, result.stderr


def test_a_deletion_is_never_refused():
    # Review: coordinator-code-reviewer — use the real deletion sentinel
    # git_native assembles, not a synthetic object(), so this tests the
    # actual integration point rather than only the predicate's contract.
    assert commit_path_legality.illegal_path_refusal({"a:b.md": git_native._ABSENT}) is None


def test_the_shared_override_key_disables_it(monkeypatch):
    monkeypatch.setenv("COORDINATOR_OVERRIDE_ILLEGAL_FILENAME", "1")
    assert commit_path_legality.illegal_path_refusal({"a:b.md": ("100644", "0" * 40)}) is None

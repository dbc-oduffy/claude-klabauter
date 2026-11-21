"""`delivered_to` is stamped `<repo-key>:<path>`, re-resolvable after a root moves."""

from __future__ import annotations

import subprocess
from pathlib import Path

from coordinator_core.ops.fleet.memo_send import _on_receiver_branch

import pytest
pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def test_on_receiver_branch_reads_the_branch_tip_tree(tmp_path: Path) -> None:
    repo = tmp_path / "r"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True,
                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    inbox = repo / "cross-repo" / "inbox"
    inbox.mkdir(parents=True)
    (inbox / "m.md").write_text("x", encoding="utf-8")
    env = ["-c", "user.name=t", "-c", "user.email=t@t"]
    for args in (["add", "."], [*env, "commit", "-q", "-m", "m"]):
        subprocess.run(["git", "-C", str(repo), *args], check=True,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))

    assert _on_receiver_branch(repo, "main", "cross-repo/inbox/m.md") is True
    assert _on_receiver_branch(repo, "main", "cross-repo/inbox/other.md") is False
    assert _on_receiver_branch(repo, "no-such-branch", "cross-repo/inbox/m.md") is False

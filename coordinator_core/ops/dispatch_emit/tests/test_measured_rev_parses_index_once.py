"""`dispatch.measured_rev` parses .git/index once per call, however many paths it measures.

Without a call scope, `_text_pin_checkin` re-parsed the whole index for every path; on a 15.8 MB
index that drove the warm pool to re-read it per path, per call, and starved the box's file cache
(2026-11-18).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.git import git_state
from coordinator_core.ops.dispatch_emit import measured_rev as mr

pytestmark = [pytest.mark.spawns_process]


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True,
                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def test_many_paths_parse_the_index_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.invalid")
    _git(repo, "config", "user.name", "t")
    (repo / ".gitattributes").write_text("* text=auto\n", encoding="utf-8")
    paths = [f"f{i}.py" for i in range(8)]
    for p in paths:
        (repo / p).write_text("x = 1\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "seed")
    for p in paths:
        (repo / p).write_text("x = 2\n", encoding="utf-8")

    parses = []
    real = git_state._parse_index_bytes
    monkeypatch.setattr(git_state, "_parse_index_bytes",
                        lambda raw, **kw: parses.append(1) or real(raw, **kw))

    out = mr._handler({"run_id": "probe-run", "paths": paths}, repo)

    assert "refused" not in (out or {}), out
    assert len(parses) <= 1, f"index parsed {len(parses)} times for {len(paths)} paths"

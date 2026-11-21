"""commit_resolution: one live spawn per (index identity, HEAD); restaging, a
second index and a moved HEAD each miss the cache."""


import pytest

from coordinator_core.hooks import commit_resolution as cr
from pathlib import Path


_WORKTREE_TOUCHING = {"diff", "status", "update-index", "add", "--refresh", "ls-files"}


def test_live_spawn_failure_raises_resolution_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GIT_INDEX_FILE", raising=False)
    monkeypatch.chdir(tmp_path)
    with pytest.raises(cr.ResolutionError):
        cr.raw_text(str(tmp_path / "nope"))

"""The review ops are common_dir-scoped, so the dispatcher hands their handlers
`<worktree>/.git`. Every plan path they join must land under the worktree, never
under `.git` (reported via coordinator-content-repo-95: record_superseding_review joined the
plan under C:/example-retrieval-repo/.git)."""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.ops import review_stamp
from coordinator_core.ops.review_mint import supersede, wave_bookkeeping


def _capture(monkeypatch, module, name):
    seen = {}

    def fake(*args, **kwargs):
        seen["root"] = kwargs.get("repo_root", args[1] if len(args) > 1 else None)
        return None if name == "check" else {}

    monkeypatch.setattr(module, name, fake)
    return seen


@pytest.mark.parametrize(
    "module,target,handler,params",
    [
        (supersede, "record_superseding_review", "_record_superseding_review_handler",
         {"plan": "p.md", "commit_range": {}, "session_id": "s"}),
        (wave_bookkeeping, "bookkeep_wave", "_bookkeep_wave_handler",
         {"session_id": "s", "plan_id": "p", "record_stem": "r"}),
        (review_stamp, "mint", "_mint_handler", {"plan": "p.md"}),
        (review_stamp, "check", "_check_handler", {"plan": "p.md"}),
    ],
)
def test_handler_passes_the_worktree_not_the_git_dir(tmp_path, monkeypatch, module, target, handler, params):
    (tmp_path / ".git").mkdir()
    seen = _capture(monkeypatch, module, target)
    getattr(module, handler)(params, repo_root=tmp_path / ".git")
    assert "root" in seen, "handler never reached the core function"
    assert Path(seen["root"]).resolve() == tmp_path.resolve()

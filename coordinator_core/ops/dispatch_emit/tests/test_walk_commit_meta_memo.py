"""`_walk_commit_meta` reads a commit once per process and never remembers an absent one."""

from __future__ import annotations

from pathlib import Path

from coordinator_core.git import commit_walk, git_objects
from coordinator_core.ops.dispatch_emit import checkpoint_stamp as cs


def test_a_present_commit_is_read_once_and_an_absent_one_is_not_remembered(monkeypatch, tmp_path):
    cs._WALK_META_CACHE.clear()
    reads: list[str] = []
    store: dict = {}

    def fake_meta(common_dir, sha):
        reads.append(sha)
        return store.get(sha)

    monkeypatch.setattr(commit_walk, "commit_meta", fake_meta)
    monkeypatch.setattr(git_objects, "_read_loose_object", lambda common_dir, sha: None)
    sha = "a" * 40

    assert cs._walk_commit_meta(tmp_path, sha) is None
    store[sha] = {"message": "m", "parents": []}
    assert cs._walk_commit_meta(tmp_path, sha) == store[sha]
    assert cs._walk_commit_meta(tmp_path, sha) == store[sha]
    assert reads == [sha, sha]
    cs._WALK_META_CACHE.clear()

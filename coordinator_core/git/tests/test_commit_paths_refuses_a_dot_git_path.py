"""A path under `.git/` never reaches a tree: commit_paths refuses it, and the session's commit offer
drops it. A remote's fsck rejects every push carrying such a tree (hasDotgit)."""

import pytest

from coordinator_core.git import commit as gcommit
from coordinator_core.git.commit import CommitRefused
from coordinator_core.ops.session import safe_commit_offer
from coordinator_core.session import claim_index

_SESSION_FILE = ".git/coordinator-sessions/s1/review-targets.txt"


def test_commit_paths_refuses_a_dot_git_component(tmp_path):
    (tmp_path / ".git").mkdir()
    with pytest.raises(CommitRefused, match="'.git' path component"):
        gcommit.commit_paths(tmp_path, [_SESSION_FILE], "m")


def test_the_session_offer_never_names_a_dot_git_path(monkeypatch):
    answer = claim_index.CommitSet(paths=[_SESSION_FILE, "state/x.yaml"], contested={}, complete=True)
    monkeypatch.setattr(claim_index, "commit_set", lambda *_a, **_k: answer)
    monkeypatch.setattr(safe_commit_offer, "live_session_ids", lambda *_a: set())
    offer = safe_commit_offer.compute_offer("s1")
    assert offer["safe_paths"] == ["state/x.yaml"]

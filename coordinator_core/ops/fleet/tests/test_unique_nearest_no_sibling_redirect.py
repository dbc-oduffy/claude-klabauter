"""An explicit receiver missing from the registry must never auto-accept a
different sibling by edit distance (cloud layout: every repo under /home/user,
Claude-klabauter unregistered, example-retrieval-repo registered)."""
from coordinator_core.ops.fleet import _memo_resolver as R

_CLOUD_REPOS = {
    "repos.content_root": "/home/user/coordinator-content-repo",
    "repos.claude_klabauter": "/root/engine-current",
    "repos.project_rag": "/home/user/example-retrieval-repo",
}


def test_unregistered_explicit_receiver_not_redirected_to_sibling():
    assert R.unique_nearest_receiver("claude-klabauter-em", _CLOUD_REPOS) is None


def test_abbreviation_still_auto_accepts():
    repos = dict(_CLOUD_REPOS, **{"repos.claude_klabauter": "/home/user/claude-klabauter"})
    assert R.unique_nearest_receiver("claude-klabauter-em", repos) == "claude-klabauter-em"


"""_git_tree_facts toplevel/show_prefix/head_ref/head_commit equal the git commands, with no spawn."""

from __future__ import annotations

import os

import pytest

from coordinator_core.bash_guards import _git_tree_facts as facts
from coordinator_core.git import repo_root

pytestmark = pytest.mark.complete

_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e",
}


@pytest.fixture(autouse=True)
def _fresh():
    facts.reset()
    repo_root.clear_memo()
    yield
    facts.reset()
    repo_root.clear_memo()


def test_non_repo_is_none(tmp_path):
    plain = tmp_path / "nothing"
    plain.mkdir()
    for fn in (facts.toplevel, facts.show_prefix, facts.head_ref, facts.head_commit):
        assert fn(str(plain)) is None

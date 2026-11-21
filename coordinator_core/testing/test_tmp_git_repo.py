"""tmp_git_repo seeds a private repo and refuses the engine repo and any .git-less root."""

from __future__ import annotations

import pytest

from coordinator_core.testing import tmp_git_repo
from coordinator_core.testing.tmp_git_repo import (
    RefusedRepoRoot,
    init_tmp_git_repo,
    tmp_git,
)

pytestmark = [pytest.mark.spawns_process]


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_init_seeds_one_commit_in_its_own_repo(tmp_path):
    root = tmp_path / "r"
    root.mkdir()
    init_tmp_git_repo(root)
    assert (root / ".git").is_dir()
    assert tmp_git(root, "rev-list", "--count", "HEAD").stdout.strip() == "1"
    init_tmp_git_repo(root)
    assert tmp_git(root, "rev-list", "--count", "HEAD").stdout.strip() == "1"


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_refuses_the_engine_repo_root():
    with pytest.raises(RefusedRepoRoot):
        tmp_git(tmp_git_repo._ENGINE_REPO, "commit", "--allow-empty", "-m", "x")
    with pytest.raises(RefusedRepoRoot):
        init_tmp_git_repo(tmp_git_repo._ENGINE_REPO)


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_refuses_a_root_without_its_own_git(tmp_path):
    with pytest.raises(RefusedRepoRoot):
        tmp_git(tmp_path, "commit", "--allow-empty", "-m", "x")


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_repo_under_an_enclosing_repo_stays_private(tmp_path):
    outer = tmp_path / "outer"
    outer.mkdir()
    init_tmp_git_repo(outer)
    inner = outer / "inner"
    inner.mkdir()
    init_tmp_git_repo(inner)
    assert tmp_git(inner, "rev-list", "--count", "HEAD").stdout.strip() == "1"
    assert tmp_git(outer, "rev-list", "--count", "HEAD").stdout.strip() == "1"

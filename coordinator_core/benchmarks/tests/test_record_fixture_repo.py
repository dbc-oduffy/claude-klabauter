"""`record_fixture_repo` builds a record-only repo in the OS temp dir: no clone, no alternates."""

from __future__ import annotations

import subprocess
import tempfile
from contextlib import ExitStack
from pathlib import Path

import pytest

from coordinator_core.benchmarks import land_run_leg_budget_profile as prof
from coordinator_core.benchmarks.isolated_clone import record_fixture_repo

pytestmark = pytest.mark.spawns_process

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _git(root: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=str(root), check=True, capture_output=True, creationflags=NO_WINDOW).stdout.decode()


def _source(tmp_path: Path) -> Path:
    src = tmp_path / "src"
    (src / "docs").mkdir(parents=True)
    (src / "docs" / "d.md").write_text("doc\n", encoding="utf-8")
    (src / "other.py").write_text("X = 1\n", encoding="utf-8")
    return src


def test_fixture_holds_only_the_given_paths_with_no_alternates_outside_scratch(tmp_path):
    src = _source(tmp_path)
    with record_fixture_repo(src, ["docs/d.md", "absent.md"], prefix="rfr-test", label="t") as repo:
        assert not (repo / ".git" / "objects" / "info" / "alternates").exists()
        assert (src / "scratch" / "benchmark-clones") not in repo.parents
        assert Path(tempfile.gettempdir()).resolve() in repo.resolve().parents
        assert _git(repo, "ls-files").split() == ["docs/d.md"]
        assert _git(repo, "rev-list", "--count", "HEAD").strip() == "1"
        assert not (repo / "other.py").exists()
        kept = repo
    assert not kept.exists()
    assert not (src / "scratch").exists()


def test_empty_path_list_still_commits_a_base(tmp_path):
    with record_fixture_repo(_source(tmp_path), [], prefix="rfr-test", label="t") as repo:
        assert _git(repo, "rev-parse", "HEAD").strip()


def test_fixture_is_removed_on_error(tmp_path):
    kept = None
    with pytest.raises(RuntimeError):
        with record_fixture_repo(_source(tmp_path), [], prefix="rfr-test", label="t") as repo:
            kept = repo
            raise RuntimeError("boom")
    assert kept is not None and not kept.exists()


def test_enter_record_fixture_points_params_at_the_fixture_head(tmp_path):
    src = _source(tmp_path)
    params = {"doc_paths": ["docs/d.md"], "run_base_sha": "b" * 40, "inline_review": {"prep": {"run_base_sha": "b" * 40}}}
    with ExitStack() as stack:
        repo = prof.enter_record_fixture(stack, src, params, prefix="rfr-test", label="t")
        head = _git(repo, "rev-parse", "HEAD").strip()
        assert params["run_base_sha"] == head == params["inline_review"]["prep"]["run_base_sha"]
        assert _git(repo, "config", "user.name").strip() == "t"

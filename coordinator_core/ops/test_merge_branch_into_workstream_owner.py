"""Merge-owner recording by `branch.merge_into_workstream` stage/abort, in tmp_path repos."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.git import merge_owner
from coordinator_core.git.git_dir import resolve_git_dir
from coordinator_core.ops import merge_branch_into_workstream as mbw
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

RUN = "ask-test-run"


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-q", "-b", "main")
    _git(r, "config", "user.email", "t@example.com")
    _git(r, "config", "user.name", "T")
    (r / "f.txt").write_text("base\n")
    _git(r, "add", ".")
    _git(r, "commit", "-q", "-m", "init")
    _git(r, "checkout", "-q", "-b", "other")
    (r / "f.txt").write_text("other\n")
    _git(r, "commit", "-q", "-am", "other")
    _git(r, "checkout", "-q", "main")
    (r / "f.txt").write_text("main\n")
    _git(r, "commit", "-q", "-am", "main")
    (r / "state" / "warp" / RUN).mkdir(parents=True)
    return r


def _merge_head(repo: Path) -> Path:
    return resolve_git_dir(repo) / "MERGE_HEAD"


def test_stage_with_run_id_records_owner(repo: Path) -> None:
    reply = mbw.merge_branch_into_workstream("other", "r", repo, stage=True, run_id=RUN)
    assert _merge_head(repo).exists()
    assert merge_owner.owner(repo) == RUN
    assert reply["owner_run_id"] == RUN


def test_stage_without_run_id_records_nothing(repo: Path) -> None:
    reply = mbw.merge_branch_into_workstream("other", "r", repo, stage=True)
    assert _merge_head(repo).exists()
    assert merge_owner.owner(repo) is None
    assert "owner_run_id" not in reply


def test_abort_clears_owner(repo: Path) -> None:
    mbw.merge_branch_into_workstream("other", "r", repo, stage=True, run_id=RUN)
    mbw.merge_branch_into_workstream("", "r", repo, abort=True)
    assert not (resolve_git_dir(repo) / merge_owner.OWNER_REL).exists()


@pytest.mark.parametrize(
    "kwargs",
    [
        {"stage": True, "run_id": 5},
        {"stage": True, "run_id": ""},
        {"stage": True, "run_id": ".."},
        {"stage": True, "run_id": "../warp"},
        {"stage": True, "run_id": "C:"},
        {"stage": True, "run_id": "no-such-run"},
        {"stage": False, "run_id": RUN},
    ],
)
def test_bad_run_id_raises_before_any_merge(repo: Path, kwargs: dict) -> None:
    with pytest.raises(ValueError):
        mbw.merge_branch_into_workstream("other", "r", repo, **kwargs)
    assert not _merge_head(repo).exists()

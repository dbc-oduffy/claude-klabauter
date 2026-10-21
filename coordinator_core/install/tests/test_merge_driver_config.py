"""Tests for coordinator_core.install.merge_driver_config: set, idempotence,
no clobber, non-DoE skip, dry-run."""

from __future__ import annotations

import pytest

from coordinator_core.git.run import run_git
from coordinator_core.install import merge_driver_config as mdc

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

REQUIRED = "python3 coordinator/bin/merge-rule-class-register.py %O %A %B"


def _git(repo, *args):
    return run_git(list(args), cwd=str(repo))


def _repo(tmp_path, *, doe):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    if doe:
        script = repo.joinpath(*mdc.DRIVER_RELPATH)
        script.parent.mkdir(parents=True)
        script.write_text("# driver\n", encoding="utf-8", newline="\n")
    return repo


def _get(repo):
    return _git(repo, "config", "--local", "--get", mdc.KEY).stdout.strip() or None


def test_wanted_is_the_literal_requested_command():
    assert mdc.WANTED == REQUIRED


def test_sets_when_absent(tmp_path):
    repo = _repo(tmp_path, doe=True)
    lines = mdc.apply(repo)
    assert lines[0].startswith("set     "), lines
    assert _get(repo) == REQUIRED


def test_second_run_is_already_set(tmp_path):
    repo = _repo(tmp_path, doe=True)
    mdc.apply(repo)
    lines = mdc.apply(repo)
    assert lines[0].startswith("ok      ") and "already set" in lines[0], lines
    assert _get(repo) == REQUIRED


def test_differing_value_reported_and_untouched(tmp_path):
    repo = _repo(tmp_path, doe=True)
    _git(repo, "config", "--local", mdc.KEY, "custom %A")
    lines = mdc.apply(repo)
    assert lines[0].startswith("left    "), lines
    assert _get(repo) == "custom %A"


def test_non_doe_repo_not_given_key(tmp_path):
    repo = _repo(tmp_path, doe=False)
    lines = mdc.apply(repo)
    assert lines[0].startswith("skip    "), lines
    assert _get(repo) is None


def test_unreadable_config_is_skipped_not_overwritten(tmp_path):
    repo = _repo(tmp_path, doe=True)
    (repo / ".git" / "config").write_text("[broken\n", encoding="utf-8")
    lines = mdc.apply(repo)
    assert lines[0].startswith("skip    "), lines


def test_dry_run_writes_nothing(tmp_path):
    repo = _repo(tmp_path, doe=True)
    lines = mdc.apply(repo, dry_run=True)
    assert lines[0].startswith("would   "), lines
    assert _get(repo) is None

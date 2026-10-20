"""`unittest discover -s <dir>` inside a provisioned target is a scoped run, not a whole-suite one."""

from __future__ import annotations

import json

import pytest

from coordinator_core.bash_guards import check_test_suite_invocation as guard
from coordinator_core.ops.sibling_repo_provision_contract import MARKER_NAME


def _repo(path, marked: bool):
    (path / ".git").mkdir(parents=True)
    (path / "tests").mkdir()
    if marked:
        (path / ".git" / MARKER_NAME).write_text(json.dumps({"name": path.name}), encoding="utf-8")
    return path


@pytest.fixture
def world(tmp_path, monkeypatch):
    this = _repo(tmp_path / "claude-klabauter", marked=False)
    marked = _repo(tmp_path / "vault", marked=True)
    unmarked = _repo(tmp_path / "other", marked=False)
    monkeypatch.setattr(guard, "resolve_git_root", lambda cwd: str(this))
    monkeypatch.setattr(guard, "_configured_test_cmds", lambda root: [])
    return this, marked, unmarked


def _classify(cmd, cwd):
    return guard.classify_command(cmd, cwd=str(cwd))


@pytest.mark.parametrize("py", ["python", "python3", "py"])
def test_discover_in_marked_target_cwd_is_scoped(world, py):
    _, marked, _ = world
    assert _classify(f"{py} -m unittest discover -s tests", marked) == []


@pytest.mark.parametrize("py", ["python", "python3", "py"])
def test_discover_in_unmarked_sibling_cwd_is_refused(world, py):
    _, _, unmarked = world
    assert _classify(f"{py} -m unittest discover -s tests", unmarked) != []


def test_discover_in_this_repo_is_refused(world):
    this, _, _ = world
    assert _classify("python3 -m unittest discover -s tests", this) != []


def test_start_directory_into_marked_sibling_from_this_repo_is_scoped(world):
    this, _, _ = world
    assert _classify("python3 -m unittest discover -s ../vault/tests", this) == []
    assert _classify("python3 -m unittest discover --start-directory ../vault/tests", this) == []


def test_start_directory_into_unmarked_sibling_is_refused(world):
    this, _, _ = world
    assert _classify("python3 -m unittest discover -s ../other/tests", this) != []


def test_bare_discover_is_refused_even_in_marked_target(world):
    _, marked, _ = world
    assert _classify("python3 -m unittest discover", marked) != []


def test_nose2_discover_in_marked_target_is_refused_as_today(world):
    _, marked, _ = world
    assert _classify("python3 -m nose2 discover -s tests", marked) != []


def test_start_directory_escaping_marked_target_is_refused(world):
    _, marked, _ = world
    assert _classify("python3 -m unittest discover -s ../other/tests", marked) != []

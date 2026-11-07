"""publish._main refuses, before any row syncs, when publisher code differs from the published sha."""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.complete

_PUBLISH = Path(__file__).resolve().parents[1] / "publish.py"
_FILES = (
    "coordinator/bin/publish.py",
    "coordinator/bin/lib/helper.py",
    "coordinator/lib/percolate/targets.py",
    "coordinator_core/percolate/engine.py",
)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *args],
        check=True, capture_output=True, text=True,
    ).stdout.strip()


@pytest.fixture()
def pub():
    spec = importlib.util.spec_from_file_location("publish_dirty_code_under_test", _PUBLISH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def repo(tmp_path):
    root = tmp_path / "repo"
    for rel in _FILES:
        f = root / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("x = 1\n")
    root.mkdir(exist_ok=True)
    _git(root, "init", "-q")
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "init")
    return root


def test_clean_tree_has_no_dirty_paths(pub, repo):
    assert pub._dirty_publisher_paths(repo, _git(repo, "rev-parse", "HEAD")) == []


@pytest.mark.parametrize("rel", _FILES)
def test_dirty_publisher_file_is_named(pub, repo, rel):
    (repo / rel).write_text("x = 2\n")
    assert pub._dirty_publisher_paths(repo, _git(repo, "rev-parse", "HEAD")) == [rel]


def test_path_absent_at_sha_is_handled(pub, repo):
    base = _git(repo, "rev-parse", "HEAD")
    (repo / "coordinator/bin/lib/new.py").write_text("y = 1\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "add")
    assert pub._dirty_publisher_paths(repo, base) == ["coordinator/bin/lib/new.py"]


def test_untracked_publisher_file_is_named(pub, repo):
    (repo / "coordinator/bin/lib/fresh.py").write_text("z = 1\n")
    assert pub._dirty_publisher_paths(repo, "HEAD") == ["coordinator/bin/lib/fresh.py"]


def test_non_publisher_dirt_is_ignored(pub, repo):
    (repo / "README.md").write_text("hi\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "doc")
    (repo / "README.md").write_text("changed\n")
    assert pub._dirty_publisher_paths(repo, "HEAD") == []


def test_main_refuses_dirty_publisher_before_any_sync(pub, repo, monkeypatch, capsys):
    (repo / "coordinator/bin/publish.py").write_text("x = 2\n")
    monkeypatch.setattr(pub, "_REPO_ROOT", repo)
    monkeypatch.setattr(pub, "_BOOTSTRAP_DONE", False)
    monkeypatch.setattr(pub, "_bootstrap_engine", lambda: pytest.fail("reached bootstrap"))
    assert pub._main([]) == 1
    err = capsys.readouterr().err
    assert "coordinator/bin/publish.py" in err
    assert "commit them, or run from a clean export" in err


def test_main_refuses_against_explicit_commit(pub, repo, monkeypatch, capsys):
    sha = _git(repo, "rev-parse", "HEAD")
    (repo / "coordinator_core/percolate/engine.py").write_text("x = 3\n")
    monkeypatch.setattr(pub, "_REPO_ROOT", repo)
    monkeypatch.setattr(pub, "_BOOTSTRAP_DONE", False)
    monkeypatch.setattr(pub, "_bootstrap_engine", lambda: pytest.fail("reached bootstrap"))
    assert pub._main(["--commit", sha]) == 1
    assert "coordinator_core/percolate/engine.py" in capsys.readouterr().err


def test_main_passes_the_gate_on_clean_tree(pub, repo, monkeypatch):
    class _Reached(Exception):
        pass

    def _stop():
        raise _Reached

    monkeypatch.setattr(pub, "_REPO_ROOT", repo)
    monkeypatch.setattr(pub, "_BOOTSTRAP_DONE", False)
    monkeypatch.setattr(pub, "_bootstrap_engine", _stop)
    with pytest.raises(_Reached):
        pub._main([])

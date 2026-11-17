"""publish._main runs from a commit clone, never refusing, when publisher code differs from the published sha."""
from __future__ import annotations

import importlib.util
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from ._git_template import copy_repo, template_repo

pytestmark = pytest.mark.complete

_PUBLISH = Path(__file__).resolve().parents[1] / "publish.py"
_FILES = (
    "coordinator/bin/publish.py",
    "coordinator/bin/lib/helper.py",
    "coordinator/lib/percolate/targets.py",
    "coordinator_core/percolate/engine.py",
)
_STUB = (
    "import sys\n"
    "print('ran-from=' + __file__, file=sys.stderr)\n"
    "print('argv=' + '|'.join(sys.argv[1:]), file=sys.stderr)\n"
    "sys.exit(7)\n"
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


def _build(root: Path, *, stub: bool) -> None:
    for rel in _FILES:
        f = root / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("x = 1\n")
    _git(root, "init", "-q")
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "init")
    if stub:
        (root / "coordinator/bin/publish.py").write_text(_STUB)
        _git(root, "add", ".")
        _git(root, "commit", "-qm", "stub")


@pytest.fixture(scope="session")
def base_template(tmp_path_factory):
    return template_repo(tmp_path_factory, "publish-dirty:base", lambda p: _build(p, stub=False))


@pytest.fixture(scope="session")
def stub_template(tmp_path_factory):
    return template_repo(tmp_path_factory, "publish-dirty:stub", lambda p: _build(p, stub=True))


@pytest.fixture(scope="session")
def base_sha(base_template):
    return _git(base_template, "rev-parse", "HEAD")


@pytest.fixture(scope="session")
def stub_sha(stub_template):
    return _git(stub_template, "rev-parse", "HEAD")


@pytest.fixture()
def repo(tmp_path, base_template):
    return copy_repo(base_template, tmp_path / "repo")


@pytest.fixture()
def stub_repo(tmp_path, stub_template):
    return copy_repo(stub_template, tmp_path / "repo")


@pytest.fixture()
def isolated_runtime(tmp_path, monkeypatch):
    """Clone cache under tmp_path; the in-process entry run's sys.path/sys.modules edits are undone."""
    cache = tmp_path / "tmp"
    cache.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(cache))
    saved_path, saved_modules = list(sys.path), dict(sys.modules)
    yield cache
    sys.path[:] = saved_path
    sys.modules.clear()
    sys.modules.update(saved_modules)


def _prep(pub, repo, monkeypatch):
    monkeypatch.setattr(pub, "_REPO_ROOT", repo)
    monkeypatch.setattr(pub, "_BOOTSTRAP_DONE", False)
    monkeypatch.setattr(pub, "_bootstrap_engine", lambda: pytest.fail("reached bootstrap"))


def test_clean_tree_has_no_dirty_paths(pub, repo, base_sha):
    assert pub._dirty_publisher_paths(repo, base_sha) == []


@pytest.mark.parametrize("rel", _FILES)
def test_dirty_publisher_file_is_named(pub, repo, base_sha, rel):
    (repo / rel).write_text("x = 2\n")
    assert pub._dirty_publisher_paths(repo, base_sha) == [rel]


def test_path_absent_at_sha_is_handled(pub, repo, base_sha):
    (repo / "coordinator/bin/lib/new.py").write_text("y = 1\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "add")
    assert pub._dirty_publisher_paths(repo, base_sha) == ["coordinator/bin/lib/new.py"]


def test_untracked_publisher_file_is_named(pub, repo):
    (repo / "coordinator/bin/lib/fresh.py").write_text("z = 1\n")
    assert pub._dirty_publisher_paths(repo, "HEAD") == ["coordinator/bin/lib/fresh.py"]


def test_non_publisher_dirt_is_ignored(pub, repo):
    (repo / "README.md").write_text("hi\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "doc")
    (repo / "README.md").write_text("changed\n")
    assert pub._dirty_publisher_paths(repo, "HEAD") == []


def test_dirty_publisher_delegates_to_a_commit_clone_without_refusing(
    pub, stub_repo, stub_sha, isolated_runtime, monkeypatch, capsys
):
    repo, sha = stub_repo, stub_sha
    (repo / "coordinator/bin/publish.py").write_text("raise RuntimeError('dirty executed')\n")
    _prep(pub, repo, monkeypatch)
    assert pub._main(["--dry-run"]) == 7
    err = capsys.readouterr().err
    assert "publisher code differs" not in err
    assert "dirty executed" not in err
    ran = next(line for line in err.splitlines() if line.startswith("ran-from="))
    ran_path = Path(ran[len("ran-from="):])
    assert ran_path.is_relative_to(isolated_runtime)
    assert ran_path.parents[2].name == sha and ran_path.parents[3].name == "claude-klabauter-commit-clone"
    assert "argv=--dry-run" in err
    assert (repo / "coordinator/bin/publish.py").read_text() == "raise RuntimeError('dirty executed')\n"


def test_dirty_publisher_against_explicit_commit_delegates_with_the_commit_argv(
    pub, stub_repo, stub_sha, isolated_runtime, monkeypatch, capsys
):
    repo, sha = stub_repo, stub_sha
    (repo / "coordinator_core/percolate/engine.py").write_text("x = 3\n")
    _prep(pub, repo, monkeypatch)
    assert pub._main(["--commit", sha]) == 7
    err = capsys.readouterr().err
    assert f"argv=--commit|{sha}" in err
    assert "refused" not in err


def test_failed_materialise_refuses_and_runs_no_live_publisher_code(pub, stub_repo, monkeypatch, capsys):
    repo = stub_repo
    (repo / "coordinator/bin/publish.py").write_text("raise RuntimeError('dirty executed')\n")
    _prep(pub, repo, monkeypatch)
    cc = pub._commit_clone_module()

    def _boom(*_a, **_k):
        raise cc.CommitCloneError("injected; Retry: python commit_clone.py materialise")

    monkeypatch.setattr(cc, "materialise_commit_clone", _boom)
    monkeypatch.setattr(cc, "run_entry_from_clone", lambda *a, **k: pytest.fail("ran a clone"))
    assert pub._main(["--dry-run"]) == 1
    err = capsys.readouterr().err
    assert "publish: refused" in err and "Retry:" in err
    assert "dirty executed" not in err


def test_a_commit_clone_is_not_delegated_again(pub, stub_repo, stub_sha, isolated_runtime, monkeypatch):
    clone = pub._commit_clone_module().materialise_commit_clone(stub_repo, stub_sha, isolated_runtime / "c")
    (clone / "coordinator/bin/lib/helper.py").write_text("x = 2\n")

    class _Reached(Exception):
        pass

    def _stop():
        raise _Reached

    monkeypatch.setattr(pub, "_REPO_ROOT", clone)
    monkeypatch.setattr(pub, "_BOOTSTRAP_DONE", False)
    monkeypatch.setattr(pub, "_bootstrap_engine", _stop)
    with pytest.raises(_Reached):
        pub._main([])


def test_source_repo_is_the_live_repo_from_a_clone_and_the_root_otherwise(
    pub, repo, base_sha, isolated_runtime, monkeypatch
):
    monkeypatch.setattr(pub, "_REPO_ROOT", repo)
    assert pub._source_repo() == repo
    clone = pub._commit_clone_module().materialise_commit_clone(repo, base_sha, isolated_runtime / "c")
    monkeypatch.setattr(pub, "_REPO_ROOT", clone)
    assert pub._source_repo() == repo


def test_unresolvable_explicit_commit_defers_to_the_target_source_check(pub, repo, monkeypatch):
    # A ref claude-klabauter lacks may name a commit of the target's source repo, so the entry gate must not
    # refuse it; `_resolve_target_source_ref` refuses once rows load (pinned in
    # test_percolate_round_named_source_commit.py).
    class _Reached(Exception):
        pass

    def _stop():
        raise _Reached

    monkeypatch.setattr(pub, "_REPO_ROOT", repo)
    monkeypatch.setattr(pub, "_BOOTSTRAP_DONE", False)
    monkeypatch.setattr(pub, "_bootstrap_engine", _stop)
    with pytest.raises(_Reached):
        pub._main(["--commit", "no-such-ref"])


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


def test_the_refusal_text_is_gone_from_the_driver():
    assert "publisher code differs" not in _PUBLISH.read_text(encoding="utf-8")

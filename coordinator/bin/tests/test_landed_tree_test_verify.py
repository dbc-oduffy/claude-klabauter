"""landed-tree-test-verify on tmp_path git repos: names and tests are judged from the committed tree."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_CLI = Path(__file__).resolve().parents[1] / "landed-tree-test-verify.py"
_GIT = ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false"]
_FLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0)

HEAD_FILES = {
    "pkg/__init__.py": "",
    "pkg/idx.py": "def add_run():\n    return 1\n",
    "pkg/mod.py": "from pkg import idx\n\n\ndef run():\n    return idx.add_run()\n",
    "pkg/tests/__init__.py": "",
    "pkg/tests/test_mod.py": "from pkg import mod\n\n\ndef test_run():\n    assert mod.run() == 1\n",
}
DIRTY_IDX = "def add_run():\n    return 1\n\n\ndef record_claim():\n    return 2\n\n\nCONST = 3\n"


def _git(repo: Path, *args: str) -> None:
    subprocess.run([*_GIT, "-C", str(repo), *args], check=True, capture_output=True, creationflags=_FLAGS)


def _write(repo: Path, files: dict) -> None:
    for rel, text in files.items():
        f = repo / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text, encoding="utf-8", newline="\n")


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _write(root, HEAD_FILES)
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "head")
    return root


def _verify(repo: Path, *paths: str):
    return subprocess.run(
        [sys.executable, str(_CLI), "--repo", str(repo), "--paths", *paths, "--workers", "1"],
        capture_output=True, text=True, creationflags=_FLAGS,
    )


def test_clean_tree_passes_and_leaves_no_scratch(repo: Path):
    _write(repo, {"pkg/mod.py": HEAD_FILES["pkg/mod.py"] + "\n# touched\n"})
    res = _verify(repo, "pkg/mod.py")
    assert res.returncode == 0, res.stdout + res.stderr
    assert "PASS" in res.stdout
    assert not (repo / "scratch" / "landed-tree-verify").exists()


def test_attribute_call_on_uncommitted_function_is_refused(repo: Path):
    _write(repo, {
        "pkg/idx.py": DIRTY_IDX,
        "pkg/caller.py": "def f():\n    from pkg import idx\n    idx.record_claim()\n",
    })
    res = _verify(repo, "pkg/caller.py")
    assert res.returncode == 1, res.stdout + res.stderr
    assert "pkg.idx.record_claim" in res.stdout


def test_touched_files_test_importing_uncommitted_name_is_refused(repo: Path):
    _git(repo, "rm", "-q", "--cached", "pkg/tests/test_mod.py")
    _write(repo, {"pkg/idx.py": DIRTY_IDX, "pkg/mod.py": HEAD_FILES["pkg/mod.py"] + "\n# touched\n"})
    _write(repo, {"pkg/tests/test_mod.py": "from pkg.idx import CONST\n\n\ndef test_c():\n    assert CONST\n"})
    _git(repo, "add", "pkg/tests/test_mod.py")
    _git(repo, "commit", "-qm", "test names CONST")
    res = _verify(repo, "pkg/mod.py")
    assert res.returncode == 1, res.stdout + res.stderr
    assert "pkg.idx.CONST" in res.stdout


def test_test_that_only_passes_with_worktree_dirt_fails_from_the_committed_tree(repo: Path):
    _write(repo, {
        "pkg/tests/test_mod.py": (
            "import pkg.idx as idx\n\n\ndef test_helper():\n    name = 'helper'\n    assert getattr(idx, name)() == 7\n"
        ),
    })
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "test wants helper")
    _write(repo, {"pkg/idx.py": HEAD_FILES["pkg/idx.py"] + "\n\ndef helper():\n    return 7\n"})
    _write(repo, {"pkg/mod.py": HEAD_FILES["pkg/mod.py"] + "\n# touched\n"})
    res = _verify(repo, "pkg/mod.py")
    assert res.returncode == 1, res.stdout + res.stderr
    assert "tests fail in the landed tree" in res.stdout


def test_landing_the_dependency_too_passes_the_same_test(repo: Path):
    _write(repo, {
        "pkg/tests/test_mod.py": (
            "import pkg.idx as idx\n\n\ndef test_helper():\n    name = 'helper'\n    assert getattr(idx, name)() == 7\n"
        ),
    })
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "test wants helper")
    _write(repo, {"pkg/idx.py": HEAD_FILES["pkg/idx.py"] + "\n\ndef helper():\n    return 7\n"})
    res = _verify(repo, "pkg/idx.py", "pkg/mod.py")
    assert res.returncode == 0, res.stdout + res.stderr


def test_module_without_covering_tests_passes_on_name_check_alone(repo: Path):
    _write(repo, {"pkg/lonely.py": "X = 1\n"})
    res = _verify(repo, "pkg/lonely.py")
    assert res.returncode == 0, res.stdout + res.stderr
    assert "no covering tests" in res.stdout


def test_not_a_git_repo_could_not_run(tmp_path: Path):
    res = _verify(tmp_path, "x.py")
    assert res.returncode == 2
    assert "allowed" in res.stdout

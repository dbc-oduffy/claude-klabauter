"""Tests for the reverify scope selector and the pre_existing split."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.reverify_scope import select_tests, split_pre_existing


pytestmark = pytest.mark.spawns_process


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=root, capture_output=True, text=True, check=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    ).stdout.strip()


def _write(root: Path, rel: str, text: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-q")
    _write(tmp_path, "pyproject.toml", '[tool.pytest.ini_options]\ntestpaths = ["pkg"]\n')
    _write(tmp_path, "pkg/__init__.py", "")
    _write(tmp_path, "pkg/alpha.py", "X = 1\n")
    _write(tmp_path, "pkg/beta.py", "Y = 1\n")
    _write(tmp_path, "pkg/tests/__init__.py", "")
    _write(tmp_path, "pkg/tests/test_alpha.py", "def test_a():\n    pass\n")
    _write(tmp_path, "pkg/tests/test_uses_beta.py", "from pkg import beta\n\ndef test_b():\n    assert beta.Y\n")
    _write(tmp_path, "pkg/tests/test_other.py", "def test_o():\n    pass\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "base")
    return tmp_path


def _commit(root: Path) -> None:
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "change")


def test_only_test_file_changed_selects_that_file_alone(repo: Path) -> None:
    base = _git(repo, "rev-parse", "HEAD")
    _write(repo, "pkg/tests/test_other.py", "def test_o():\n    assert True\n")
    _commit(repo)
    out = select_tests(repo, base)
    assert out["tests"] == ["pkg/tests/test_other.py"]
    assert out["changed"] == ["pkg/tests/test_other.py"]
    assert out["fully_mapped"] is True


def test_changed_source_selects_mapped_test_not_unrelated(repo: Path) -> None:
    base = _git(repo, "rev-parse", "HEAD")
    _write(repo, "pkg/alpha.py", "X = 2\n")
    _commit(repo)
    out = select_tests(repo, base)
    assert out["tests"] == ["pkg/tests/test_alpha.py"]
    assert out["fully_mapped"] is True


def test_unmapped_source_selects_importing_test_via_closure(repo: Path) -> None:
    base = _git(repo, "rev-parse", "HEAD")
    _write(repo, "pkg/beta.py", "Y = 2\n")
    _commit(repo)
    out = select_tests(repo, base)
    assert out["tests"] == ["pkg/tests/test_uses_beta.py"]
    assert out["fully_mapped"] is True


def test_source_with_no_covering_test_is_not_fully_mapped(repo: Path) -> None:
    base = _git(repo, "rev-parse", "HEAD")
    _write(repo, "pkg/gamma.py", "Z = 1\n")
    _commit(repo)
    out = select_tests(repo, base)
    assert out["tests"] == []
    assert out["fully_mapped"] is False


def test_deleted_file_is_dropped(repo: Path) -> None:
    base = _git(repo, "rev-parse", "HEAD")
    (repo / "pkg/tests/test_other.py").unlink()
    _commit(repo)
    out = select_tests(repo, base)
    assert out["changed"] == []
    assert out["tests"] == []


def test_unknown_base_raises(repo: Path) -> None:
    with pytest.raises(ValueError):
        select_tests(repo, None)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        select_tests(repo, "deadbeef" * 5)


def test_pre_existing_red_is_not_charged() -> None:
    out = split_pre_existing(["a::t1", "b::t2"], ["b::t2", "z::t9"])
    assert out == {"failing": ["a::t1"], "pre_existing": ["b::t2"]}


def test_split_with_no_base_failures_charges_all() -> None:
    assert split_pre_existing(["a::t1"], []) == {"failing": ["a::t1"], "pre_existing": []}

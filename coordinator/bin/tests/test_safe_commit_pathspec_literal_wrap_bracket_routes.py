
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

_BIN_DIR = Path(__file__).resolve().parent.parent

pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]

spec = importlib.util.spec_from_file_location(
    "safe_commit_bracket_route_under_test", _BIN_DIR / "coordinator-safe-commit.py"
)
assert spec is not None and spec.loader is not None
safe_commit = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = safe_commit
spec.loader.exec_module(safe_commit)


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "r"
    route_dir = root / "app" / "[id]"
    decoy_dir = root / "app" / "i"
    route_dir.mkdir(parents=True)
    decoy_dir.mkdir(parents=True)
    _git(root.parent, "init", "-q", str(root))
    _git(root, "config", "user.email", "t@example.invalid")
    _git(root, "config", "user.name", "t")
    (route_dir / "route.ts").write_text("export const GET = () => {};\n", encoding="utf-8")
    (decoy_dir / "route.ts").write_text("decoy\n", encoding="utf-8")
    _git(root, "add", "--", "app/[id]/route.ts", "app/i/route.ts")
    _git(root, "commit", "-q", "-m", "seed")
    return root


def test_git_ls_files_pathspec_matches_only_the_literal_bracket_route(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    monkeypatch.chdir(root)

    matched = safe_commit._git_ls_files_pathspec("app/[id]/route.ts")

    assert matched == ["app/[id]/route.ts"]


def test_validate_pathspec_accepts_a_bracket_route(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    monkeypatch.chdir(root)

    assert safe_commit._validate_pathspec("app/[id]/route.ts") is True


def test_first_invalid_pathspec_accepts_a_batch_with_a_bracket_route(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    monkeypatch.chdir(root)

    result = safe_commit._first_invalid_pathspec(
        ["app/[id]/route.ts", "app/[id]/route.ts"]
    )

    assert result is None

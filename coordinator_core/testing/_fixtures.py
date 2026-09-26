
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import pytest


@dataclass(frozen=True)
class FixtureTree:
    """A materialized synthetic repo tree: one file per family + a decoy venv.

    `family_files` maps each of the four family keys (see `collect.FAMILY_GLOBS`)
    to the path of its one representative fixture file. `decoy_venv_test` is a
    `test_*.py` file planted inside `decoy_venv_dir` — a valid `py-native` match
    by basename alone, present specifically to prove venv-exclusion (DEC-2)
    prunes it before it is ever collected.
    """

    repo_root: Path
    family_files: dict[str, Path]
    decoy_venv_dir: Path
    decoy_venv_test: Path


def _write(path: Path, content: str = "") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, newline="\n")
    return path


FixtureTreeBuilder = Callable[..., FixtureTree]


@pytest.fixture
def fixture_tree(tmp_path: Path) -> FixtureTreeBuilder:

    def build(
        root: Path | None = None,
        venv_dirname: str = ".venv",
        extra_files: tuple[tuple[str, str], ...] = (),
    ) -> FixtureTree:
        repo_root = root if root is not None else (tmp_path / "repo")
        repo_root.mkdir(parents=True, exist_ok=True)

        family_files: dict[str, Path] = {
            "js-prefix": _write(repo_root / "test-example.js", "// js prefix suite\n"),
            "js-suffix": _write(repo_root / "example.test.js", "// js suffix suite\n"),
            "py-native": _write(
                repo_root / "test_example.py",
                "def test_ok():\n    assert True\n",
            ),
            "py-nonnative": _write(repo_root / "example.test.py", "print('ok')\n"),
        }

        decoy_venv_dir = repo_root / venv_dirname / "lib" / "site-packages" / "somepkg"
        decoy_venv_test = _write(
            decoy_venv_dir / "test_decoy.py",
            "def test_decoy():\n    assert True\n",
        )

        for relpath, content in extra_files:
            _write(repo_root / relpath, content)

        return FixtureTree(
            repo_root=repo_root,
            family_files=family_files,
            decoy_venv_dir=decoy_venv_dir,
            decoy_venv_test=decoy_venv_test,
        )

    return build

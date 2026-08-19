"""
coordinator_core.testing._fixtures — shared synthetic-fixture-tree factory (plain module).

Purpose: one canonical `fixture_tree` pytest factory fixture, consumed by every
test module under `coordinator_core/testing/` (this chunk's `test_collect.py`,
and the sibling chunks' `test_run.py` / `test_full_runner.py`) AND, cross-package,
by the C5 op's tests under `coordinator_core/ops/tests/`.

Lives in a PLAIN module (not a `conftest.py`) so it can be pulled in via
`pytest_plugins = ("coordinator_core.testing._fixtures",)` from another package's
tests without pytest double-registering it: a `conftest.py` requested as a
`pytest_plugins` entry that is ALSO auto-loaded as a local conftest raises
"Plugin already registered under a different name". `coordinator_core.testing.conftest`
re-exports `fixture_tree` from here so in-package tests keep the local-conftest path.

Port source: none — net-new (DR-059 harness authoring).
Spec backlink: pln-claude-klabauter-python-full-test-runner-f8ca5a § C1 (conftest.py)

Negative-spec:
    - Does NOT write any fixture file under `coordinator_core/` on disk — every
      fixture file is materialized under pytest's `tmp_path` at RUNTIME only
      (Finding 9). Do not "helpfully" cache the built tree into the package
      tree for speed/reuse across runs — a committed `test_*.py` fixture here
      (especially a failing one) would be auto-collected by claude-klabauter's own
      pytest config (`testpaths = ["coordinator_core"]`,
      `python_files = ["test_*.py"]`) and permanently red claude-klabauter's real suite.
    - Does NOT assert anything itself — this module is fixture infrastructure
      only; assertions belong in the consuming test modules.
"""

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
    """Factory fixture. Call the returned `build(...)` to materialize a tree.

    build(
        root: Path | None = None,            # defaults to tmp_path / "repo"
        venv_dirname: str = ".venv",          # or "site-packages" / ".coordinator-venv"
        extra_files: tuple[tuple[str, str], ...] = (),  # (relpath, content) pairs
    ) -> FixtureTree
    """

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

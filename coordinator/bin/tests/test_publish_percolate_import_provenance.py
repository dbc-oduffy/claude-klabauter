"""coordinator/bin/tests/test_publish_percolate_import_provenance.py — AC9.

`_import_claude_klabauter_percolate` (coordinator/bin/publish.py) imports its whole
percolate-transform surface from the LIVE claude-klabauter working tree: not just
`coordinator_core/percolate/`, but also `frontmatter/schema_validate.py`,
`ops/percolate_run.py` (where `run_percolate` -- the callable that actually
rewrites pinned payload -- lives), `ops/percolate_identity_check.py`, and
`diagnostics/contained_run.py`. An uncommitted edit to any of these changes
published bytes with no commit attesting what produced them.

`_assert_percolate_transform_set_clean` closes this: dirty-check the REAL
import set (`_PERCOLATE_TRANSFORM_SET_PATHS`) before `_import_claude_klabauter_
percolate` dispatches a single import, and refuse loud, naming the exact
dirty paths, rather than silently importing uncommitted transform code.

Negative-spec: a dirty-check scoped to `coordinator_core/percolate/` ALONE
would pass while `ops/percolate_run.py` (outside that directory) carries an
uncommitted edit -- AC9's load-bearing correction. `test_widened_set_catches_
percolate_run_edit` below pins exactly this gap.

Run: python -m pytest coordinator/bin/tests/test_publish_percolate_import_provenance.py -q
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.cadence,
    pytest.mark.spawns_process,
]

_BIN_DIR = Path(__file__).resolve().parent.parent
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=str(root),
        capture_output=True,
        check=True,
        creationflags=_NO_WINDOW,
    )


def _init_engine_root(root: Path) -> None:
    """Seeds a minimal fake engine checkout carrying every path in
    `_PERCOLATE_TRANSFORM_SET_PATHS`, committed clean at HEAD."""
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.email", "publish-percolate-import-provenance-test@claude-klabauter.test")
    _git(root, "config", "user.name", "Publish Percolate Import Provenance Test")
    _git(root, "config", "commit.gpgsign", "false")

    files = [
        Path("coordinator_core") / "percolate" / "engine.py",
        Path("coordinator_core") / "percolate" / "guards.py",
        Path("coordinator_core") / "frontmatter" / "schema_validate.py",
        Path("coordinator_core") / "ops" / "percolate_run.py",
        Path("coordinator_core") / "ops" / "percolate_identity_check.py",
        Path("coordinator_core") / "diagnostics" / "contained_run.py",
    ]
    for rel in files:
        dest = root / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text("# placeholder\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "chore: seed fake engine transform set")


def _load_publish_module():
    spec = importlib.util.spec_from_file_location(
        "publish_percolate_import_provenance_under_test", _BIN_DIR / "publish.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


publish = _load_publish_module()


def test_clean_transform_set_does_not_raise(tmp_path: Path) -> None:
    root = tmp_path / "engine"
    root.mkdir()
    _init_engine_root(root)

    publish._assert_percolate_transform_set_clean(str(root))


def test_dirty_edit_inside_percolate_dir_refuses_and_names_the_path(tmp_path: Path) -> None:
    root = tmp_path / "engine"
    root.mkdir()
    _init_engine_root(root)

    edited = root / "coordinator_core" / "percolate" / "engine.py"
    edited.write_text("# uncommitted transform edit\n", encoding="utf-8")

    with pytest.raises(publish.EngineUnavailableError) as excinfo:
        publish._assert_percolate_transform_set_clean(str(root))

    message = str(excinfo.value)
    assert "coordinator_core/percolate/engine.py" in message


def test_widened_set_catches_percolate_run_edit(tmp_path: Path) -> None:
    """AC9's load-bearing correction: `ops/percolate_run.py` sits OUTSIDE
    `coordinator_core/percolate/`, yet carries `run_percolate` -- the
    callable that actually rewrites pinned payload. A dirty-check scoped to
    `percolate/` alone would read this working tree as clean; the widened
    `_PERCOLATE_TRANSFORM_SET_PATHS` must not."""
    root = tmp_path / "engine"
    root.mkdir()
    _init_engine_root(root)

    edited = root / "coordinator_core" / "ops" / "percolate_run.py"
    edited.write_text("# uncommitted run_percolate edit\n", encoding="utf-8")

    with pytest.raises(publish.EngineUnavailableError) as excinfo:
        publish._assert_percolate_transform_set_clean(str(root))

    message = str(excinfo.value)
    assert "coordinator_core/ops/percolate_run.py" in message


def test_dirty_edit_outside_transform_set_does_not_refuse(tmp_path: Path) -> None:
    """An uncommitted edit outside the covered import set (e.g. an unrelated
    file elsewhere in the engine checkout) must not trip the refusal --
    scoped, not whole-tree."""
    root = tmp_path / "engine"
    root.mkdir()
    _init_engine_root(root)

    unrelated = root / "coordinator_core" / "unrelated_module.py"
    unrelated.parent.mkdir(parents=True, exist_ok=True)
    unrelated.write_text("# not in the transform set\n", encoding="utf-8")

    publish._assert_percolate_transform_set_clean(str(root))


def test_multiple_dirty_paths_are_all_named(tmp_path: Path) -> None:
    root = tmp_path / "engine"
    root.mkdir()
    _init_engine_root(root)

    (root / "coordinator_core" / "percolate" / "guards.py").write_text(
        "# uncommitted guards edit\n", encoding="utf-8"
    )
    (root / "coordinator_core" / "frontmatter" / "schema_validate.py").write_text(
        "# uncommitted schema_validate edit\n", encoding="utf-8"
    )

    with pytest.raises(publish.EngineUnavailableError) as excinfo:
        publish._assert_percolate_transform_set_clean(str(root))

    message = str(excinfo.value)
    assert "coordinator_core/percolate/guards.py" in message
    assert "coordinator_core/frontmatter/schema_validate.py" in message


def test_transform_set_paths_exist_in_this_checkout() -> None:
    """Finding 5, s3-sweep-and-dirty review: `git status -- <path>` returns
    `rc=0` with no output for a pathspec that doesn't exist on disk -- a path
    in `_PERCOLATE_TRANSFORM_SET_PATHS` renamed upstream without updating
    the constant would silently probe as clean rather than surfacing
    "nothing to check here." This repo IS the engine root
    `_assert_percolate_transform_set_clean` runs against in production, so
    pin the constant against the real checkout directly rather than the
    per-test fake `_init_engine_root` (which seeds a copy of the constant's
    own path list and so cannot catch this drift)."""
    repo_root = _BIN_DIR.parent.parent
    missing = [
        rel for rel in publish._PERCOLATE_TRANSFORM_SET_PATHS if not (repo_root / rel).exists()
    ]
    assert missing == [], (
        f"_PERCOLATE_TRANSFORM_SET_PATHS entries absent from {repo_root}: {missing} -- "
        "an absent path silently reads as clean instead of raising"
    )


def test_unresolvable_engine_root_fails_closed(tmp_path: Path) -> None:
    """A git probe that cannot run (no repo at `engine_root`) must never be
    read as clean -- same fail-closed posture as every other AC15 leg."""
    not_a_repo = tmp_path / "not-a-repo"
    not_a_repo.mkdir()

    with pytest.raises(publish.EngineUnavailableError):
        publish._assert_percolate_transform_set_clean(str(not_a_repo))

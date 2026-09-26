
from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path
from typing import Any, Dict

import pytest

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_BIN_DIR = Path(__file__).resolve().parent.parent
_NO_WINDOW: Dict[str, Any] = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "percolate_round_pathspec_spawn_budget", _BIN_DIR / "percolate-round.py"
    )
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


_mod = _load_module()


def _git_run(args, cwd):
    subprocess.run(args, cwd=str(cwd), capture_output=True, text=True, check=True, **_NO_WINDOW)


def _seed_repo(repo_root: Path, declared_count: int) -> list:
    """A committed HEAD plus `declared_count` UNTRACKED declared paths.

    Untracked is the shape that exercises the add side's whole union: absent
    from `head_tree`, invisible to `git diff HEAD` (which never reports
    untracked files), so every one of them is named NEW and reaches
    `_filter_commit_pathspec`'s `check-ignore` leg.
    """
    repo_root.mkdir(parents=True, exist_ok=True)
    _git_run(["git", "init", "-q"], repo_root)
    (repo_root / "seed.txt").write_text("seed\n")
    _git_run(["git", "add", "-A"], repo_root)
    _git_run(
        ["git", "-c", "user.email=a@b.c", "-c", "user.name=a", "commit", "-q", "-m", "seed"],
        repo_root,
    )
    declared = []
    for index in range(declared_count):
        rel = f"payload/f{index:04d}.py"
        path = repo_root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"# {index}\n")
        declared.append(rel)
    return declared


def _count_spawns(manifest, repo_root: Path) -> list:
    argvs = []
    real_run = _mod._run

    def _counting_run(args, **kwargs):
        argvs.append(list(args))
        return real_run(args, **kwargs)

    setattr(_mod, "_run", _counting_run)
    try:
        _mod._pathspec_from_manifest(manifest, str(repo_root))[0]
    finally:
        setattr(_mod, "_run", real_run)
    return argvs


def _manifest(declared):
    return _mod._RoundManifest(
        round_id="spawn-budget-fixture",
        added_or_updated=frozenset(declared),
        removed=frozenset(),
        declared_payload=frozenset(declared),
        published_dest_dirs=frozenset({"payload"}),
    )


def test_head_baseline_is_two_processes_not_one_per_path(tmp_path):
    repo_root = tmp_path / "dest"
    declared = _seed_repo(repo_root, 40)
    argvs = _count_spawns(_manifest(declared), repo_root)

    ls_tree = [a for a in argvs if "ls-tree" in a]
    diff = [a for a in argvs if "diff" in a]
    assert len(ls_tree) == 1, f"expected one ls-tree, got {ls_tree!r}"
    assert len(diff) == 1, f"expected one diff HEAD, got {diff!r}"
    assert not any(
        "ls-files" in a and any(rel in a for rel in declared) for a in argvs
    ), "a per-path `git ls-files` reappeared on the add side"


def test_spawn_count_does_not_grow_with_the_declared_payload(tmp_path):
    small_root = tmp_path / "small"
    large_root = tmp_path / "large"
    small = _count_spawns(_manifest(_seed_repo(small_root, 10)), small_root)
    large = _count_spawns(_manifest(_seed_repo(large_root, 400)), large_root)

    assert len(small) == len(large), (
        f"spawn count moved with path count: 10 paths -> {len(small)}, "
        f"400 paths -> {len(large)}\nsmall={small!r}\nlarge={large!r}"
    )

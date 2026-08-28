"""test_percolate_round_pathspec_spawn_budget -- pins AC4 of the superseded
plan docs/plans/2026-08-26-a-refused-round-strands-its-payload-forever.md,
carried to dlv-open-the-percolate-removal-side-without-65ff4e: the dest-HEAD
baseline `_pathspec_from_manifest` reads costs a FIXED number of processes per
round, never one per path.

AC4 was measured once by a standalone probe and never pinned, so nothing
stopped a later edit from reintroducing the per-path `git` spawn the P1 row
(state/bug-backlog/2026-08-25-compute-scope-costs-219-391ms-on-the-comm-
7b3e91d4c2a6.yaml) calls extinct. A measurement that is not a test decays into
a claim; this file is the test.

The invariant asserted is amplification, not a magic constant: the same
derivation over 10 declared paths and over 400 must spend the SAME number of
processes. A hardcoded total would break on any legitimately added probe and
teach the next reader to bump the number rather than ask why it moved.

Negative-spec: this file does not assert wall-clock or CPU time (§ CLAUDE.md --
process time and spawn count, never wall clock, and a per-test timing budget is
a flake generator on a box running 50 peers), does not exercise the removal
side's scoping (§ test_percolate_round_removal_side_scoping.py), and does not
test `_filter_commit_pathspec`'s three filters (§
test_percolate_round_commit_pathspec.py). No test here runs a real percolate
round or touches a live publish mirror.

Run: python -m pytest coordinator/bin/tests/test_percolate_round_pathspec_spawn_budget.py -q
"""

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
    """Every `_run` argv the derivation spends, in order.

    Wraps rather than stubs: the real `git` still answers, so the count is
    what a round actually spends and not what a fake makes convenient.

    Restores `_run` in a `finally` rather than leaning on `monkeypatch`,
    because the comparison test measures TWICE in one test body: a patch
    still installed on the second measurement makes the second counter
    delegate to the first, and every later spawn lands in BOTH lists. That
    reads as "spawn count moved with path count" -- this file's own failure
    message, pointing at a bug in this file.

    A future test added to this file must not mix
    `monkeypatch.setattr(_mod, "_run", ...)` with this helper: monkeypatch
    undoes its patch at test teardown, while this helper restores inline,
    so interleaving the two patterns would leave `_run` patched (or
    restored) at a point neither pattern expects.
    """
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
    """AC4's literal claim: the HEAD baseline is exactly two spawns.

    Covers only the HEAD-baseline leg (ls-tree + diff HEAD), not the whole
    `_pathspec_from_manifest` derivation -- once `seen` is non-empty a third
    spawn (the batched check-ignore) follows, bringing the total to three.
    The sibling amplification test below covers that total.
    """
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
    """The amplification invariant, which is the one that decays silently.

    Ten paths and four hundred must cost the same processes. If this fails
    with the counts differing by roughly the path-count ratio, a per-path
    spawn is back; if they differ by one or two, a batched probe was added
    that chunks its argv -- read the argv lists in the failure before
    adjusting anything.
    """
    small_root = tmp_path / "small"
    large_root = tmp_path / "large"
    small = _count_spawns(_manifest(_seed_repo(small_root, 10)), small_root)
    large = _count_spawns(_manifest(_seed_repo(large_root, 400)), large_root)

    assert len(small) == len(large), (
        f"spawn count moved with path count: 10 paths -> {len(small)}, "
        f"400 paths -> {len(large)}\nsmall={small!r}\nlarge={large!r}"
    )

"""Process-time bar for the amplification scan's blob-sha cache: a warm unchanged-tree rescan and a
one-edit rescan each cost under 500ms process time (DR-344 brightline) and spawn nothing.

Cadence tier: one cold populate of the live scope costs about 19s.
"""

from __future__ import annotations

import pathlib
import shutil
import sys

import pytest

from coordinator_core.benchmarks.process_time import in_process_time_ms
from coordinator_core.tests._amp_scan_incremental import scan_incremental
from coordinator_core.tests.test_no_unbatched_per_item_git_spawn import (
    _GATE_SCOPE_ROOTS,
    _REPO_ROOT,
    _gate_scope_paths,
)

pytestmark = pytest.mark.cadence

BAR_MS = 500.0

_SPAWN_EVENTS = frozenset(
    {"subprocess.Popen", "os.system", "os.spawn", "os.posix_spawn", "os.startfile"}
)

#: A mid-graph module that other scanned modules import from.
_EDIT_TARGET = "coordinator_core/atomic_replace.py"


class _SpawnCounter:
    """sys.addaudithook cannot be removed, so the hook stays installed and counts only while
    `active`. A Popen.__init__ patch would miss os.system and os.spawn*."""

    def __init__(self) -> None:
        self.active = False
        self.count = 0
        sys.addaudithook(self._hook)

    def _hook(self, event: str, _args: tuple) -> None:
        if self.active and event in _SPAWN_EVENTS:
            self.count += 1

    def __enter__(self) -> "_SpawnCounter":
        self.count = 0
        self.active = True
        return self

    def __exit__(self, *_exc: object) -> None:
        self.active = False


_SPAWNS = _SpawnCounter()


def _copy_scoped_sources(dest: pathlib.Path) -> tuple[pathlib.Path, ...]:
    """Copies the non-test sources of the gate's scope roots under `dest`, preserving the
    repo-relative layout."""
    ignore = shutil.ignore_patterns("tests", "test_*", "__pycache__", "*.pyc")
    roots = []
    for root in _GATE_SCOPE_ROOTS:
        shutil.copytree(_REPO_ROOT / root, dest / root, ignore=ignore)
        roots.append(dest / root)
    return tuple(roots)


def test_warm_unchanged_tree_rescan_under_bar(tmp_path: pathlib.Path) -> None:
    roots = _gate_scope_paths()
    cache_dir = tmp_path / "cache"
    scan_incremental(roots, cache_dir)

    with _SPAWNS as spawns:
        result = {}

        def warm() -> None:
            result["stats"] = scan_incremental(roots, cache_dir)[1]

        timing = in_process_time_ms(warm)

    assert result["stats"].tree_hit, "warm rescan of an unchanged tree must be an L0 hit"
    assert spawns.count == 0, f"warm rescan spawned {spawns.count} process(es)"
    assert timing["process_time_ms"] < BAR_MS, timing


def test_one_edit_rescan_under_bar(tmp_path: pathlib.Path) -> None:
    roots = _copy_scoped_sources(tmp_path / "tree")
    cache_dir = tmp_path / "cache"
    target = tmp_path / "tree" / _EDIT_TARGET
    original = target.read_bytes()
    variants = (original + b"\n# edit-a\n", original + b"\n# edit-bb\n")
    scan_incremental(roots, cache_dir)

    state = {"flip": 0, "stats": []}

    def edit_and_rescan() -> None:
        state["flip"] ^= 1
        target.write_bytes(variants[state["flip"]])
        state["stats"].append(scan_incremental(roots, cache_dir)[1])

    with _SPAWNS as spawns:
        timing = in_process_time_ms(edit_and_rescan)

    assert state["stats"], "the timed callable never ran"
    assert not any(s.tree_hit for s in state["stats"]), "every edited rescan must be an L0 miss"
    assert spawns.count == 0, f"one-edit rescan spawned {spawns.count} process(es)"
    assert timing["process_time_ms"] < BAR_MS, timing

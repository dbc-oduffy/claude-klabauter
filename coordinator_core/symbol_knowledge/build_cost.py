"""coordinator_core.symbol_knowledge.build_cost -- what a code-intel op costs, in process time,
spawns and tokens.

Purpose: ``measure`` runs a callable and returns its ``BuildCost``; ``full_tree_reextract`` is the
reference row every incremental op is compared against (re-parse every tracked ``.py`` file at a sha).

Negative-spec:
  - Process time is CPU time (user+system) across this process and its reaped children, never wall
    clock: wall clock measures peer load.
  - Spawns are process creations seen through ``subprocess.Popen`` (which ``subprocess.run`` and
    ``check_output`` go through, so each counts once), ``os.posix_spawn``, ``os.fork`` and
    ``os.system`` while the counter is active; it does not see a spawn made by a child of a child.
  - Makes no model call; ``tokens_in``/``tokens_out`` are passed in by the caller that spent them.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any, Callable, List, Tuple, Union

from coordinator_core.symbol_knowledge.contract import BuildCost

PathLike = Union[str, Path]

FULL_TREE_OP = "baseline.full_tree_reextract"


def cpu_seconds() -> float:
    """User+system CPU seconds of this process plus its reaped children."""
    t = os.times()
    return t.user + t.system + t.children_user + t.children_system


class SpawnCounter:
    """Counts process creations while active (context manager)."""

    def __init__(self) -> None:
        self.count = 0
        self._undo: "List[Callable[[], None]]" = []

    def _wrap(self, owner: Any, name: str) -> None:
        original = getattr(owner, name, None)
        if original is None:
            return

        def counted(*args: Any, **kwargs: Any) -> Any:
            self.count += 1
            return original(*args, **kwargs)

        setattr(owner, name, counted)
        self._undo.append(lambda: setattr(owner, name, original))

    def __enter__(self) -> "SpawnCounter":
        for owner, name in (
            (subprocess, "Popen"),
            (os, "posix_spawn"),
            (os, "fork"),
            (os, "system"),
        ):
            self._wrap(owner, name)
        return self

    def __exit__(self, *exc: Any) -> None:
        for undo in reversed(self._undo):
            undo()


def measure(
    op: str,
    fn: "Callable[[], Any]",
    *,
    tokens_in: int = 0,
    tokens_out: int = 0,
    files_reextracted: int = 0,
) -> "Tuple[Any, BuildCost]":
    """Run *fn* once; return ``(its result, BuildCost)``. An exception from *fn* propagates and no
    cost is recorded."""
    with SpawnCounter() as spawns:
        before = cpu_seconds()
        result = fn()
        elapsed = cpu_seconds() - before
    return result, BuildCost(
        op=op,
        process_ms=round(elapsed * 1000, 3),
        spawns=spawns.count,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        files_reextracted=files_reextracted,
    )


def tracked_py_files(root: PathLike, sha: str) -> "List[str]":
    """Repo-relative ``.py`` paths in the tree of *sha* (one ``git ls-tree`` spawn)."""
    done = subprocess.run(
        ["git", "-C", str(root), "ls-tree", "-r", "--name-only", sha],
        check=True, capture_output=True, text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return [p for p in done.stdout.splitlines() if p.endswith(".py")]


def full_tree_reextract(root: PathLike, sha: str) -> "Tuple[BuildCost, int]":
    """Re-parse every ``.py`` file the tree of *sha* lists that exists in the working tree at
    *root*; returns ``(BuildCost, files_listed_but_absent)``. The reference an incremental op is
    held against."""
    from coordinator_core.cartography.symbols import build_symbols

    root = Path(root)
    holder: "dict[str, int]" = {"present": 0, "absent": 0}

    def run() -> None:
        listed = tracked_py_files(root, sha)
        present = [p for p in listed if (root / p).is_file()]
        holder["present"] = len(present)
        holder["absent"] = len(listed) - len(present)
        build_symbols(root, present)

    _, cost = measure(FULL_TREE_OP, run)
    return BuildCost(
        op=cost.op,
        process_ms=cost.process_ms,
        spawns=cost.spawns,
        tokens_in=0,
        tokens_out=0,
        files_reextracted=holder["present"],
    ), holder["absent"]

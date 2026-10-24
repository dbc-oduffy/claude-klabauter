"""Write baseline: the bytes at each declared write path before any executor ran.

Invariant: the baseline is taken at stage, before any executor. A path whose bytes still equal
it was not written by this run, whatever HEAD says. Spawn-free; stdlib plus ``kept_work`` only.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable, Mapping, Optional

from coordinator_core.ops.dispatch_emit.kept_work import fingerprint

NO_CHANGE_REFUSAL = "no-change"

#: Commit refusals that reproduce under an unchanged refire.
NON_REFIRE_REFUSALS = ("unreviewed", NO_CHANGE_REFUSAL)

#: Reply keys of the no-change refusal.
MEANT_TO_LAND_KEY = "meant_to_land"
DIRTY_BEFORE_RUN_KEY = "dirty_before_run"


def capture(root: Path, paths: Iterable[str]) -> dict[str, Optional[str]]:
    """Map each repo-relative path to its fingerprint; None means absent."""
    return {p: fingerprint(root, p) for p in paths}


def unwritten(root: Path, baseline: Mapping[str, Optional[str]], paths: Iterable[str]) -> list[str]:
    """Paths (declared order, deduped) whose current fingerprint equals the baseline's.
    A path missing from ``baseline`` is never unwritten: unknown is not unchanged."""
    out: list[str] = []
    seen: set[str] = set()
    for p in paths:
        if p in seen:
            continue
        seen.add(p)
        if p in baseline and fingerprint(root, p) == baseline[p]:
            out.append(p)
    return out


def pre_existing_dirt(
    root: Path,
    baseline: Mapping[str, Optional[str]],
    paths: Iterable[str],
    own_written: Iterable[str] = (),
) -> list[str]:
    """Claimed paths that are foreign dirt: present in the tree, unchanged since ``baseline``, and
    not in ``own_written`` (paths the run's earlier passes or checkpoint commits wrote). An absent
    path is never dirt; ``unwritten`` still counts it, for the no-change refusal."""
    own = set(own_written)
    return [p for p in unwritten(root, baseline, paths) if p not in own and fingerprint(root, p) is not None]

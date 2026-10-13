"""
coordinator_core.ops.fleet._decision_citations — live-surface citation scan for decision records.

Purpose: answer "which live surfaces still cite these decision records?" with ONE batched
`git grep -n -w -E 'DR-(a|b|…)' -- <live pathspecs>` spawn, parsed in-process. Shared by
`fleet.delete_superseded_decisions` (the on-demand sweeper) and `coordinator-doc-new
--supersedes` (the authoring-time deleter), which imports it lazily.

Leaf invariant: stdlib + `coordinator_core.win_portability` only. It must not import
`fleet._common`, `ipc` or `session`, so a CLI can load it without the engine.

Live pathspecs are `coordinator_core`, `coordinator`, `scripts`, `docs/decisions`,
`docs/reference`, `docs/wiki`, `CLAUDE.md`; archives, state, plans, research and
problems are history and are not searched.

Exemptions: a hit on X is ignored in X's own file, in X's successor's file, or on a line
that also names X's successor id.
"""

from __future__ import annotations

import logging
import re
import subprocess
from pathlib import Path
from typing import Dict, List, NamedTuple, Optional, Sequence, Tuple

from coordinator_core.win_portability import no_console_creationflags

_LOG = logging.getLogger(__name__)
_LOG.addHandler(logging.NullHandler())

_LIVE_SURFACE_PATHSPECS: Tuple[str, ...] = (
    "coordinator_core",
    "coordinator",
    "scripts",
    "docs/decisions",
    "docs/reference",
    "docs/wiki",
    "CLAUDE.md",
)

_DR_ID_RE = re.compile(r"^DR-(\d+)-")
_DR_ID_ANY_RE = re.compile(r"DR-(\d+)")
_ID_HIT_RE = re.compile(r"\bDR-(\d+)\b")


class CitationTarget(NamedTuple):
    rel: str
    dr_number: int
    successor: Optional[str]


def _dr_number(filename: str) -> Optional[int]:
    m = _DR_ID_RE.match(filename)
    return int(m.group(1)) if m else None


def live_citations(
    worktree_root: Path, targets: Sequence[CitationTarget],
) -> Dict[str, List[str]]:
    """Map target `rel` -> ["path:line", ...] for every live-surface hit that strands it.

    Spawns nothing when `targets` is empty. A git grep failure (rc not 0/1) warns and
    returns {} — callers treat that as "no citations found".
    """
    if not targets:
        return {}

    pattern = "DR-(" + "|".join(str(t.dr_number) for t in targets) + ")"
    argv = ["git", "grep", "-n", "-w", "-E", pattern, "--"] + list(_LIVE_SURFACE_PATHSPECS)
    proc = subprocess.run(
        argv,
        cwd=str(worktree_root),
        capture_output=True,
        **no_console_creationflags(),
    )
    if proc.returncode not in (0, 1):
        _LOG.warning(
            "decision citation scan: git grep failed (rc=%s): %s",
            proc.returncode, proc.stderr.decode(errors="replace").strip(),
        )
        return {}

    by_number = {str(t.dr_number): t for t in targets}
    succ_by_number: Dict[str, Optional[int]] = {}
    for key, t in by_number.items():
        m = _DR_ID_ANY_RE.search(t.successor) if t.successor else None
        succ_by_number[key] = int(m.group(1)) if m else None
    hits: Dict[str, List[str]] = {}

    for line in proc.stdout.decode(errors="replace").splitlines():
        if not line:
            continue
        parts = line.split(":", 2)
        if len(parts) < 3:
            continue
        hit_path, hit_lineno, content = parts[0], parts[1], parts[2]

        for mid in set(_ID_HIT_RE.findall(content)):
            target = by_number.get(mid)
            if target is None:
                continue
            if hit_path == target.rel or Path(hit_path).name == Path(target.rel).name:
                continue
            succ_num = succ_by_number[mid]
            if succ_num is not None and _dr_number(Path(hit_path).name) == succ_num:
                continue
            if succ_num is not None and f"DR-{succ_num}" in content:
                continue
            hits.setdefault(target.rel, []).append(f"{hit_path}:{hit_lineno}")

    return hits

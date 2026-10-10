"""Session anchor and descendant census for guard-heavy-command-admission.

Stdlib only, spawn-free: every process fact comes from the injected ProcessPrimitives snapshot.
One snapshot per call, no cache; callers that need both the anchor and the census pass one
snapshot to both.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Collection, Dict, List, Mapping, Optional, Sequence

from coordinator_core.bash_guards._heavy_admission_contract import ProcessPrimitives, ProcRow

HEAVY_IMAGES = frozenset(
    {"tsc", "node", "next", "vite", "cargo", "rustc", "pytest", "python", "python3", "pnpm",
     "npm", "npx", "vitest", "unrealeditor", "unrealeditor-cmd", "unrealbuildtool", "dotnet",
     "ubt", "runuat"}
)
SHELL_IMAGES = frozenset({"bash", "sh", "zsh", "pwsh", "powershell", "cmd"})
_MAX_HOPS = 64


@dataclass(frozen=True)
class SessionCensus:
    """heavy and shells are live descendants (and attributed orphans for heavy) of the anchor."""

    anchor: ProcRow
    heavy: Sequence[ProcRow]
    shells: Sequence[ProcRow]


def _stem(name: str) -> str:
    low = name.lower()
    return low[:-4] if low.endswith(".exe") else low


def _is_claude(name: str) -> bool:
    return _stem(name).startswith("claude")


def _payload_pid(payload: Mapping[str, Any]) -> Optional[int]:
    raw = payload.get("pid")
    if isinstance(raw, bool):
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def resolve_anchor(
    payload: Mapping[str, Any],
    primitives: ProcessPrimitives,
    snapshot: Optional[Sequence[ProcRow]] = None,
) -> Optional[ProcRow]:
    """Nearest claude-named ancestor of payload["pid"], or None (the guard then denies).

    None on: no usable pid, unreadable snapshot, a missing hop, a hop whose parent is not older
    than its child (pid reuse), a cycle, or no claude ancestor.
    """
    pid = _payload_pid(payload)
    if pid is None:
        return None
    rows = snapshot if snapshot is not None else primitives.snapshot()
    if rows is None:
        return None
    by_pid: Dict[int, ProcRow] = {r.pid: r for r in rows}
    node = by_pid.get(pid)
    for _ in range(_MAX_HOPS):
        if node is None:
            return None
        if _is_claude(node.name):
            return node
        parent = by_pid.get(node.ppid)
        if parent is None or parent.pid == node.pid or parent.ctime > node.ctime:
            return None
        node = parent
    return None


def session_census(
    anchor: ProcRow,
    primitives: ProcessPrimitives,
    snapshot: Optional[Sequence[ProcRow]] = None,
    exclude_pids: Collection[int] = (),
) -> Optional[SessionCensus]:
    """Live heavy and shell descendants of anchor; None when the process table is unreadable.

    Descent stops at a nested claude. A heavy-image row whose ppid is absent from the snapshot
    and whose ctime is after the anchor's is attributed to the session. A child older than its
    parent is a reused pid and is not descended. exclude_pids drops the caller's own wrapper
    chain from the counts.
    """
    rows = snapshot if snapshot is not None else primitives.snapshot()
    if rows is None:
        return None
    present = {r.pid for r in rows}
    children: Dict[int, List[ProcRow]] = {}
    for r in rows:
        children.setdefault(r.ppid, []).append(r)

    skip = set(exclude_pids)
    seen = {anchor.pid}
    heavy: List[ProcRow] = []
    shells: List[ProcRow] = []

    def classify(row: ProcRow) -> None:
        if row.pid in skip:
            return
        stem = _stem(row.name)
        if stem in SHELL_IMAGES:
            shells.append(row)
        elif stem in HEAVY_IMAGES:
            heavy.append(row)

    stack = [anchor]
    while stack:
        cur = stack.pop()
        for child in children.get(cur.pid, ()):
            if child.pid in seen or child.ctime < cur.ctime:
                continue
            seen.add(child.pid)
            if _is_claude(child.name):
                continue
            classify(child)
            stack.append(child)

    for r in rows:
        if r.pid in seen or r.ppid in present or r.ctime <= anchor.ctime:
            continue
        if _stem(r.name) in HEAVY_IMAGES and r.pid not in skip:
            seen.add(r.pid)
            heavy.append(r)

    return SessionCensus(anchor=anchor, heavy=tuple(heavy), shells=tuple(shells))

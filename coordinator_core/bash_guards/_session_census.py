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
# The harness runs Bash/PowerShell tool commands under one of these as a direct child of claude.
# MCP and language servers are direct children too, but never under these (Windows wraps a
# stdio MCP server in cmd.exe, which is why cmd is not a tool shell).
TOOL_SHELL_IMAGES = frozenset({"bash", "sh", "zsh", "pwsh", "powershell"})
_MAX_HOPS = 64


@dataclass(frozen=True)
class SessionCensus:
    """shells are the anchor's live tool shells; heavy is the heavy-image processes under them."""

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
    """The session's tool shells and the heavy processes under them; None when the process table
    is unreadable.

    Only the anchor's direct tool-shell children (TOOL_SHELL_IMAGES) are descended, so MCP and
    language servers, which the harness starts as other direct children, are never counted.
    shells is those tool shells; heavy is every heavy-image process below them. Descent stops at
    a nested claude. A child older than its parent is a reused pid and is not descended.
    exclude_pids drops the caller's own wrapper chain from the counts.

    TRAP: a process whose parent has exited cannot be attributed to a session on Windows, so it is
    not counted. Counting every such orphan on the box charged other sessions' leftovers to this one.
    """
    rows = snapshot if snapshot is not None else primitives.snapshot()
    if rows is None:
        return None
    children: Dict[int, List[ProcRow]] = {}
    for r in rows:
        children.setdefault(r.ppid, []).append(r)

    skip = set(exclude_pids)
    heavy: List[ProcRow] = []
    shells: List[ProcRow] = []
    tool_shells = [
        c for c in children.get(anchor.pid, ())
        if c.ctime >= anchor.ctime and _stem(c.name) in TOOL_SHELL_IMAGES
    ]
    seen = {anchor.pid}
    stack: List[ProcRow] = []
    for shell in tool_shells:
        seen.add(shell.pid)
        if shell.pid not in skip:
            shells.append(shell)
        stack.append(shell)
    while stack:
        cur = stack.pop()
        for child in children.get(cur.pid, ()):
            if child.pid in seen or child.ctime < cur.ctime:
                continue
            seen.add(child.pid)
            if _is_claude(child.name):
                continue
            if child.pid not in skip and _stem(child.name) in HEAVY_IMAGES:
                heavy.append(child)
            stack.append(child)

    return SessionCensus(anchor=anchor, heavy=tuple(heavy), shells=tuple(shells))

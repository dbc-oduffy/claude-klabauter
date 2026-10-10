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


def heavy_roots(
    heavy: Sequence[ProcRow], rows: Sequence[ProcRow], anchor_pid: Optional[int] = None
) -> List[ProcRow]:
    """The heavy rows with no heavy ancestor: one per command, so `pnpm -> tsx -> node` is one.

    Ancestry is walked through rows by ppid and ends at anchor_pid (whatever launched the session
    is not part of it), at a missing parent, or at an older-than-child parent (pid reuse).
    """
    heavy_pids = {r.pid for r in heavy}
    by_pid = {r.pid: r for r in rows}
    roots: List[ProcRow] = []
    for row in heavy:
        node, nested = row, False
        for _ in range(_MAX_HOPS):
            parent = by_pid.get(node.ppid)
            if parent is None or parent.pid == node.pid or parent.ctime > node.ctime:
                break
            if parent.pid == anchor_pid:
                break
            if parent.pid in heavy_pids:
                nested = True
                break
            node = parent
        if not nested:
            roots.append(row)
    return roots


def _children(rows: Sequence[ProcRow]) -> Dict[int, List[ProcRow]]:
    out: Dict[int, List[ProcRow]] = {}
    for r in rows:
        out.setdefault(r.ppid, []).append(r)
    return out


def _subtree(root: ProcRow, children: Mapping[int, List[ProcRow]]) -> List[ProcRow]:
    """root and its live descendants, never descending into a reused pid or a nested claude."""
    out = [root]
    stack = [root]
    seen = {root.pid}
    while stack:
        cur = stack.pop()
        for child in children.get(cur.pid, ()):
            if child.pid in seen or child.ctime < cur.ctime or _is_claude(child.name):
                continue
            seen.add(child.pid)
            out.append(child)
            stack.append(child)
    return out


@dataclass(frozen=True)
class OrphanTree:
    """A live process tree whose root's parent has exited: what a Git Bash fork stub or a
    detached launch leaves behind. stems is every image stem in the tree."""

    root: ProcRow
    stems: frozenset
    heavy: Sequence[ProcRow]


def _is_orphan_root(row: ProcRow, by_pid: Mapping[int, ProcRow]) -> bool:
    """A non-claude row whose parent has exited, or whose ppid now names a younger (reused) pid."""
    if _is_claude(row.name):
        return False
    parent = by_pid.get(row.ppid)
    return parent is None or parent.ctime > row.ctime


def orphan_trees(rows: Sequence[ProcRow], since_ctime: int) -> List[OrphanTree]:
    """Orphan trees whose root was created at or after since_ctime and that hold a heavy image.

    TRAP: on Windows a Git Bash command's real process tree is orphaned from launch (its fork-stub
    parent exits at once), so ppid descent from the session never reaches it.
    """
    by_pid = {r.pid: r for r in rows}
    children = _children(rows)
    out: List[OrphanTree] = []
    for r in rows:
        if r.ctime < since_ctime or not _is_orphan_root(r, by_pid):
            continue
        tree = _subtree(r, children)
        heavy = tuple(p for p in tree if _stem(p.name) in HEAVY_IMAGES)
        if heavy:
            out.append(OrphanTree(r, frozenset(_stem(p.name) for p in tree), heavy))
    return out


def claimed_heavy(rows: Sequence[ProcRow], roots: Collection[tuple]) -> List[ProcRow]:
    """One row per live tree rooted at a (pid, ctime) key: its earliest heavy-image process.

    One per tree because a lease is one command; an `npx tsc` tree holds two node processes.
    """
    if not roots:
        return []
    children = _children(rows)
    out: List[ProcRow] = []
    for r in rows:
        if (r.pid, r.ctime) in roots:
            heavy = [p for p in _subtree(r, children) if _stem(p.name) in HEAVY_IMAGES]
            if heavy:
                out.append(min(heavy, key=lambda p: p.ctime))
    return out

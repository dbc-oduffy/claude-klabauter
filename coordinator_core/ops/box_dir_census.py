"""box.dir_census: which engine-written dir is cruft, and whose.

One row per dir under the engine's known writer roots (configured scratch_root, the repo's
`.coordinator-local`, its `state/warp`, the warm dir, the fleet temp root) -- never a drive walk.
Sizes come from a process-lifetime cache keyed on (path, dir mtime) and capped at `_MAX_ENTRY_AGE_S`: an unchanged mtime with a
finished walk inside that age answers with no filesystem walk at all; a changed or unfinished dir is measured by
`claude_home_census.measure` inside the call's budget and resumed on the next call. A size the
budget did not finish is reported `size_stale: true`, never walked past the budget.
Invariant: `live` is `owner_mark.owner_alive` over the dir's `.owner.json`; no mark or an unknown
answer is `None`, and a consumer keeps such a dir.
"""
from __future__ import annotations

import os
import time
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional

from coordinator_core.ipc import register_op, CallerFacingValidationError
from coordinator_core.ops.claude_home_census import Walk, measure

DEFAULT_BUDGET_MS = 50
MAX_BUDGET_MS = 400

_BOUNDS = {"budget_ms": (1, MAX_BUDGET_MS), "limit": (1, 10**9)}
_SCHEMA = {
    "type": "object",
    "properties": {
        "budget_ms": {"type": "integer", "minimum": 1, "maximum": MAX_BUDGET_MS},
        "limit": {"type": "integer", "minimum": 1},
    },
    "additionalProperties": False,
}


@dataclass(frozen=True)
class Root:
    """A census root: rows are the dirs `depth` levels below `path`. `owner_from_path` reads the
    owning repo off the first path component below the root; otherwise `repo` names it."""

    label: str
    path: Path
    depth: int = 1
    repo: str = ""
    owner_from_path: bool = False


@dataclass
class _Entry:
    mtime_ns: int
    walk: Walk
    mark: Optional[dict] = None
    born: float = field(default_factory=time.monotonic)


#: path -> size state and owner mark, both valid for `mtime_ns`. Pruned to the latest enumeration.
_CACHE: Dict[str, _Entry] = {}
#: Serializes cache reads and pruning: concurrent op calls share `_CACHE` in one process.
_CACHE_LOCK = threading.Lock()

#: A dir's own mtime does not move when a file deeper in it grows, so a finished size is trusted this
#: long and then re-walked.
_MAX_ENTRY_AGE_S = 300.0

#: Smallest walk slice worth starting on one dir; a thinner share finds nothing before its deadline.
_MIN_SLICE_S = 0.005


def _scandir_dirs(path: Path) -> List["os.DirEntry[str]"]:
    try:
        with os.scandir(path) as it:
            return [e for e in it if _is_real_dir(e)]
    except OSError:
        return []


def _is_real_dir(entry: "os.DirEntry[str]") -> bool:
    try:
        return entry.is_dir(follow_symlinks=False) and not entry.is_symlink()
    except OSError:
        return False


def _enumerate(root: Root) -> List[tuple]:
    """(path, mtime_ns, first component below the root) per dir at the root's depth; symlinks and
    junctions are never entered. TRAP: the mtime is an lstat of the dir itself; the copy in the parent's
    listing is updated lazily on NTFS and would read an unchanged dir as moved, or a moved one as unchanged."""
    level = [(root.path, "")]
    for _ in range(root.depth):
        nxt: List[tuple] = []
        for parent, first in level:
            for entry in _scandir_dirs(parent):
                nxt.append((Path(entry.path), first or entry.name))
        level = nxt
    out = []
    for path, first in level:
        try:
            out.append((path, os.lstat(path).st_mtime_ns, first))
        except OSError:
            continue
    return out


def _refresh_sizes(dirs: List[tuple], budget_s: float, read_mark: Callable[[Path], Optional[dict]]) -> Dict[str, _Entry]:
    """State per dir path. A hit (same mtime, finished walk) costs nothing; a new or moved dir re-reads its
    mark; unfinished walks share `budget_s` in enumeration order, resumed by the next call."""
    states: Dict[str, _Entry] = {}
    todo: List[_Entry] = []
    keys: List[str] = []
    for path, mtime_ns, _first in dirs:
        key = str(path)
        entry = _CACHE.get(key)
        if (
            entry is None
            or entry.mtime_ns != mtime_ns
            or time.monotonic() - entry.born > _MAX_ENTRY_AGE_S
        ):
            entry = _Entry(mtime_ns, Walk(), read_mark(path))
            _CACHE[key] = entry
        states[key] = entry
        if not entry.walk.complete:
            todo.append(entry)
            keys.append(key)
    if todo:
        deadline = time.monotonic() + budget_s
        slice_s = max(budget_s / len(todo), _MIN_SLICE_S)
        # Unstarted dirs first: a big tree resumed every call must not starve the small ones.
        order = sorted(zip(keys, todo), key=lambda kv: kv[1].walk.started)
        for key, entry in order:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            measure(key, min(slice_s, remaining), resume=entry.walk)
    return states


def _owner(mark: Optional[dict], root: Root, first: str) -> str:
    if mark and mark.get("repo"):
        return os.path.basename(os.path.normpath(str(mark["repo"]))) or root.repo
    return first if root.owner_from_path else root.repo


def _keep_predicate(owner_mark) -> Callable[[Path], bool]:
    """`owner_mark.is_kept` semantics at one realpath per distinct parent dir, not one per member per row."""
    normed = [owner_mark._norm(m) for paths in owner_mark.keep_paths().values() for m in paths]
    members = {str(m) for m in normed}
    above = {str(p) for m in normed for p in m.parents}
    parents: Dict[Path, str] = {}

    def kept(path: Path) -> bool:
        real_parent = parents.get(path.parent)
        if real_parent is None:
            real_parent = parents[path.parent] = str(owner_mark._norm(path.parent))
        target = os.path.join(real_parent, os.path.normcase(path.name))
        if target in members or target in above:
            return True
        while True:
            target, tail = os.path.split(target)
            if not tail or not target:
                return False
            if target in members:
                return True

    return kept


def census(
    roots: List[Root],
    budget_s: float = DEFAULT_BUDGET_MS / 1000,
    now: Optional[float] = None,
    keep: Optional[Callable[[Path], bool]] = None,
    alive: Optional[Callable[[Optional[dict]], Optional[bool]]] = None,
) -> dict:
    """Rows `{path, root, owner_repo, owner_kind, owner_id, age_s, size_bytes, size_stale, live, kept}`
    over `roots`, largest first. `keep` and `alive` default to the keep-set from one
    `owner_mark.keep_paths()` read and `owner_mark.owner_alive`; a fault resolving the keep-set
    leaves `kept` None. A dir's mark is cached with its size, so it is re-read only when the mtime moves."""
    from coordinator_core import owner_mark

    if alive is None:
        alive = owner_mark.owner_alive
    if keep is None:
        try:
            keep = _keep_predicate(owner_mark)
        except Exception:  # noqa: BLE001 -- unknown keep-set reports kept=None, never False
            keep = None
    stamp = time.time() if now is None else now

    dirs: List[tuple] = []
    owners: Dict[str, tuple] = {}
    for root in roots:
        for path, mtime_ns, first in _enumerate(root):
            dirs.append((path, mtime_ns, first))
            owners[str(path)] = (root, first)
    with _CACHE_LOCK:
        states = _refresh_sizes(dirs, budget_s, owner_mark.read_mark)
        for stale_key in [k for k in _CACHE if k not in states]:
            del _CACHE[stale_key]

    rows: List[dict] = []
    for path, mtime_ns, _first in dirs:
        key = str(path)
        root, first = owners[key]
        mark = states[key].mark
        walk = states[key].walk
        rows.append({
            "path": key,
            "root": root.label,
            "owner_repo": _owner(mark, root, first),
            "owner_kind": mark["owner_kind"] if mark else None,
            "owner_id": mark["owner_id"] if mark else None,
            "age_s": max(0.0, stamp - mtime_ns / 1e9),
            "size_bytes": walk.bytes,
            "size_stale": not walk.complete,
            "live": alive(mark) if mark else None,
            "kept": keep(path) if keep else None,
        })
    rows.sort(key=lambda r: (-int(r["size_bytes"]), str(r["path"])))
    return {
        "rows": rows,
        "stale_rows": sum(1 for r in rows if r["size_stale"]),
        "total_bytes": sum(int(r["size_bytes"]) for r in rows),
        "roots": [{"label": r.label, "path": str(r.path)} for r in roots],
    }


def _repo_root(repo_root: Optional[Path]) -> Optional[Path]:
    if repo_root:
        return Path(repo_root)
    here = Path.cwd()
    for parent in (here, *here.parents):
        if (parent / ".git").exists():
            return parent
    return None


def default_roots(repo_root: Optional[Path] = None) -> List[Root]:
    """The engine's known writer roots; each unresolvable root is omitted, none is guessed."""
    from coordinator_core import machine_resolver, temp_layout

    roots: List[Root] = []
    repo = _repo_root(repo_root)
    repo_name = repo.name if repo else ""
    try:
        configured = machine_resolver.registry_get("paths.scratch_root")
    except Exception:  # noqa: BLE001
        configured = None
    if configured:
        roots.append(Root("scratch_root", Path(configured), depth=2, owner_from_path=True))
    if repo:
        roots.append(Root("coordinator-local", repo / ".coordinator-local", depth=2, repo=repo_name))
        roots.append(Root("warp", repo / "state" / "warp", repo=repo_name))
    try:
        from coordinator_core.warm.door_credential import warm_dir

        roots.append(Root("warm", warm_dir(), repo="warm"))
    except Exception:  # noqa: BLE001
        pass
    try:
        roots.append(Root("fleet-temp", temp_layout.fleet_temp_root(), repo="_fleet"))
    except Exception:  # noqa: BLE001
        pass
    return roots


@register_op("box.dir_census", params_schema=_SCHEMA)
def dir_census(params: object, repo_root: Optional[Path] = None) -> dict:
    """Per-dir owner repo, age, size and liveness over the engine's writer roots.

    Optional `budget_ms` (default 50, max 400) bounds the size refresh of dirs whose mtime moved;
    `limit` keeps the largest N rows. A size the budget left unfinished reads `size_stale: true` and
    continues on the next call. Reads only; deletes nothing.
    """
    p = params if isinstance(params, dict) else {}
    unknown = set(p) - set(_BOUNDS)
    if unknown:
        raise CallerFacingValidationError(f"box.dir_census: unknown fields {sorted(unknown)}")
    for name, (low, high) in _BOUNDS.items():
        if name in p:
            v = p[name]
            if isinstance(v, bool) or not isinstance(v, int) or v < low or v > high:
                raise CallerFacingValidationError(f"box.dir_census: {name} must be an integer in {low}..{high}")
    result = census(default_roots(repo_root), budget_s=p.get("budget_ms", DEFAULT_BUDGET_MS) / 1000)
    if "limit" in p:
        result["rows"] = result["rows"][: p["limit"]]
    return result

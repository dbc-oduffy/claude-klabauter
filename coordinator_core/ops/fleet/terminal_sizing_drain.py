"""The terminal-sizing drain: one scan of `state/sizings`, one move plan, one commit.

`scan_sizings` reads every sizing head once; `plan_moves` applies the refusal rails (forward plan
FK, roadmap stubs, archive date, destination collision) from those scalars with the plan and citer
lookups built once; `drain` retains dirty candidates through one index walk and lands the rest
through `land_moves`: one plumbing commit and one batched index restore.

Commit-path spike (701 moves, process time incl. children): `archive_and_commit` ~625-720 ms, of
which ~280 ms was six chunked `git restore --staged` spawns (argv-capped absolute paths) and ~80 ms
the Deliverable-Id artifact tier reading one file per path. `land_moves` splices the index once
(`update-index --index-info -z`, HEAD's own blobs on stdin; ~31 ms child CPU for 701 moves), probes
destination collisions from one `os.listdir` per archive month, and passes no paths to the trailer
ladder past `_TRAILER_PATHS_MAX`. Archive leg after: ~190-235 ms, combined ~310-470 ms.

Margin spike (archive leg ~220-280 ms before): the head scan was the largest in-process item
(~50 ms: a MULTILINE alternation regex swept every 4 KiB head). `_read_scalars` now `str.find`s
`\\n<key>:` and matches only the value (scan 13 ms; same scalars as the regex over 7836 live
sizing/plan files), and the collision probe and archive-month join work on strings, not Paths.
Left: ~45-60 ms of renames and the one `update-index` spawn (~30 ms child CPU, which beat an
in-process `splice_index` at 300 ms because it hashes every spliced file). Archive leg now
~140-200 ms, combined ~265-360 ms over 16 runs.

Spike note (baseline at 793f147e7c, `terminal_sizing_drain_budget.py --baseline`, seeded 701
movable / 194 forward-plan-not-terminal / 2016 run records; process time, never wall clock, 15.6 ms
clock granularity; cProfile on the same corpus):
  fleet.archive_terminal_sizings  ~1400-1560 ms, 6 spawns (preview then act-with-ids).
      Preview ~0.65 s: ~8100 file opens, `parse_frontmatter_status`/`parse_frontmatter_field`
      (`dag._read_meta` front-matter parse, one per field per record) plus `_resolve_plan_fk` stat
      probes. Act: the single `git status --porcelain` over 701 pathspecs
      (`_dirty_sizing_relpaths`) ~0.58 s of descendant-plus-wait CPU, then the per-id re-verify
      re-parses every record again, then `archive_and_commit`.
  promote_hand_shipped_sizings.run  ~380-420 ms seeded (1310 ms measured live, whose run dirs and
      sizings are larger and cold), 0 spawns, 0 promoted. ~6000 file opens: `run_records`
      reads `submission.json`, parses `commit-request.txt` (`declared_writes`, 2016 parses) and
      stats `reconciled.json` per run, then `sizing_scalar` re-opens and regex-scans the whole
      sizing for `status` once per run though 895 sizings back them.
Both are file reads repeated per record; `SizingScan` reads each sizing head once, and the dirty
status plus the commit are the only spawns the rebuild may keep. The dirty status takes the single
directory pathspec `state/sizings` rather than one pathspec per candidate.

Never-infer (DR-293): nothing here writes or infers a `status:`.
"""

from __future__ import annotations

import asyncio
import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, Iterable, List, Mapping, Optional, Set, Tuple

from coordinator_core.lifecycle_constants import (
    HANDOFF_TERMINAL_DEPLOYMENT,
    PLAN_TERMINAL_STATUS,
    SIZING_TERMINAL_STATUS,
)
from coordinator_core.dag import invalidate_git_history_cache
from coordinator_core.git.git_state import head_sha, read_tree_spine
from coordinator_core.ops.ceremony.git_native import (
    REASON_WORKTREE_DIRTY,
    _ABSENT,
    _commit_via_head_spine,
    dirty_relpaths_from_porcelain,
)
from coordinator_core.ops.fleet import _index_resync_drain
from coordinator_core.session import core as session_core
from coordinator_core.session import record_homes
from coordinator_core.session import scope as session_scope
from coordinator_core.temp_layout import managed_tempdir
from coordinator_core.win_portability import no_console_creationflags
from coordinator_core.ops.fleet._common import (
    Move,
    _REASON_DEST_CONFLICT,
    _cleanup_created_dirs,
    _is_identical_duplicate,
    _make_git_env,
    _message_with_hookless_trailers,
    _mkdir_and_track_created,
    _resync_main_index_for_moves,
    parse_frontmatter_field,
    parse_frontmatter_status,
    rel_id,
)

_HEAD_BYTES = 4096
_SIZINGS_REL = Path(record_homes.home_dir("", "sizings")).as_posix()
_KEY_NEEDLES = tuple((key, f"\n{key}:") for key in ("status", "plan", "deliverable_id", "route", "xl_exit"))
_VALUE_RE = re.compile(r"[ \t]*(?:\"([^\"\n]*)\"|'([^'\n]*)'|([^\s#'\"][^\s#]*))")
_NULL = {"", "null", "~"}
# Keys a refusal rail reads: a rail key past the head must not read as absent (fail-open).
_RAIL_KEYS = (b"status:", b"plan:", b"route:")

REASON_FORWARD_PLAN_NOT_TERMINAL = "forward-plan-not-terminal"
REASON_ROADMAP_STUBS_NOT_TERMINAL = "roadmap-stubs-not-terminal"
REASON_ALREADY_ARCHIVED = "already-archived"

_DATE_PREFIX_RE = re.compile(r"^(\d{4}-\d{2})-\d{2}-")
_UNSAFE_NAME_CHARS_RE = re.compile(r"[^A-Za-z0-9._-]")
_ARCHIVE_PLANS_SUBDIRS = (("archive", "specs"), ("archive", "plans"))
_CITER_SEARCH_SUBDIRS = (
    ("docs", "plans"),
    tuple(Path(record_homes.home_dir("", "handoffs")).parts),
    ("archive", "specs"),
    ("archive", "handoffs"),
)
_ROUTE_ROADMAP = "roadmap"
_ROUTE_PM_DECISION = "pm-decision"
_XL_EXIT_ROADMAP = "roadmap"


@dataclass(frozen=True)
class SizingRecord:
    """Head-of-file scalars of one sizing; absent or null keys read as ""."""

    rel: str
    status: str
    plan: str
    deliverable_id: str
    route: str
    xl_exit: str
    title: str = ""

    @property
    def terminal(self) -> bool:
        return self.status in SIZING_TERMINAL_STATUS


@dataclass
class SizingScan:
    """Result of one directory pass over `state/sizings`: `records` by repo-relative path."""

    repo_root: Path
    records: Dict[str, SizingRecord] = field(default_factory=dict)

    def terminal(self) -> List[SizingRecord]:
        return [r for r in self.records.values() if r.terminal]


def _read_scalars(path: str) -> Optional[Dict[str, str]]:
    """The first-occurrence top-level scalars of one YAML document, or None when unreadable.

    Reads `_HEAD_BYTES`, and the rest of the file only when `status:`, `plan:` or `route:` is not
    in that head (25 of 2338 live sizings put `status:` past 4 KiB). A fenced document is cut at its closing fence so a body
    line never answers for the frontmatter."""
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0))
    except OSError:
        return None
    try:
        head = os.read(fd, _HEAD_BYTES)
        if len(head) == _HEAD_BYTES and any(
            not (head.startswith(key) or b"\n" + key in head) for key in _RAIL_KEYS
        ):
            chunks = [head]
            while True:
                chunk = os.read(fd, 1 << 20)
                if not chunk:
                    break
                chunks.append(chunk)
            head = b"".join(chunks)
    except OSError:
        return None
    finally:
        os.close(fd)
    text = head.decode("utf-8", errors="replace")
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            text = text[:end]
    text = "\n" + text
    values: Dict[str, str] = {}
    for key, needle in _KEY_NEEDLES:
        at = text.find(needle)
        while at != -1:
            match = _VALUE_RE.match(text, at + len(needle))
            if match is not None:
                raw = next((g for g in match.groups() if g is not None), "").strip()
                values[key] = "" if raw in _NULL else raw
                break
            at = text.find(needle, at + 1)
    at = text.find("\ntitle:")
    if at != -1:
        stop = text.find("\n", at + 1)
        line = text[at + 7 : stop if stop != -1 else len(text)]
        values["title"] = line.lstrip(" \t").split(" #", 1)[0].strip().strip("\"'")
    return values


def read_record(repo_root: Path, rel: str) -> Optional[SizingRecord]:
    """The `SizingRecord` of one sizing at repo-relative `rel`, or None when it cannot be read."""
    return _record_at(str(Path(repo_root) / rel), rel)


def _record_at(path: str, rel: str) -> Optional[SizingRecord]:
    values = _read_scalars(path)
    if values is None:
        return None
    return SizingRecord(
        rel=rel,
        status=values.get("status", ""),
        plan=values.get("plan", ""),
        deliverable_id=values.get("deliverable_id", ""),
        route=values.get("route", ""),
        xl_exit=values.get("xl_exit", ""),
        title=values.get("title", ""),
    )


def scan_sizings(repo_root: Path) -> SizingScan:
    """One `os.scandir` pass over `state/sizings/*.yaml`, reading each file's head once. A missing
    directory or unreadable file yields no record; no YAML parse and no git."""
    root = Path(repo_root)
    scan = SizingScan(repo_root=root)
    try:
        entries = sorted(os.scandir(record_homes.home_dir(str(root), "sizings")), key=lambda e: e.name)
    except OSError:
        return scan
    for entry in entries:
        if not entry.name.endswith(".yaml") or not entry.is_file():
            continue
        rel = f"{_SIZINGS_REL}/{entry.name}"
        record = _record_at(entry.path, rel)
        if record is not None:
            scan.records[rel] = record
    return scan


# ---------------------------------------------------------------------------
# Refusal rails
# ---------------------------------------------------------------------------


def derive_yyyy_mm(fname: str) -> Optional[str]:
    """`2026-08-13-foo.yaml` -> `2026-08`; None when the filename carries no date prefix."""
    m = _DATE_PREFIX_RE.match(fname)
    return m.group(1) if m else None


class PlanResolver:
    """Resolves a sizing's `plan:` FK live-then-archive and reads the plan's status, each once.

    The FK is archive-agnostic: a shipped plan moves to `archive/specs/<month>/` and nothing
    rewrites the citation. Live always wins; the archive index (one walk per archive root) is
    built on the first live miss; an ambiguous multi-match resolves like no match."""

    def __init__(self, root: Path) -> None:
        self._root = Path(root)
        self._archive: Optional[Dict[Tuple[str, ...], Dict[str, List[Path]]]] = None
        self._resolved: Dict[str, Optional[Path]] = {}
        self._status: Dict[Path, str] = {}

    def _archive_index(self) -> Dict[Tuple[str, ...], Dict[str, List[Path]]]:
        if self._archive is None:
            self._archive = {}
            for subdir in _ARCHIVE_PLANS_SUBDIRS:
                by_name: Dict[str, List[Path]] = {}
                for dirpath, _dirs, files in os.walk(self._root.joinpath(*subdir)):
                    for name in files:
                        by_name.setdefault(name, []).append(Path(dirpath) / name)
                self._archive[subdir] = by_name
        return self._archive

    def resolve(self, plan_fk: str) -> Optional[Path]:
        if plan_fk in self._resolved:
            return self._resolved[plan_fk]
        found = self._resolve_uncached(plan_fk)
        self._resolved[plan_fk] = found
        return found

    def _resolve_uncached(self, plan_fk: str) -> Optional[Path]:
        direct = self._root / plan_fk
        if direct.is_file():
            return direct
        basename = os.path.basename(plan_fk.replace("\\", "/"))
        if not basename:
            return None
        bare = self._root / "docs" / "plans" / basename
        if bare.is_file():
            return bare
        for by_name in self._archive_index().values():
            matches = sorted(by_name.get(basename, ()))
            if len(matches) == 1:
                return matches[0]
        return None

    def refusal(self, plan_fk: str) -> Optional[str]:
        plan_path = self.resolve(plan_fk)
        if plan_path is None:
            return (
                f"{REASON_FORWARD_PLAN_NOT_TERMINAL}: plan FK {plan_fk!r} "
                f"could not be resolved to a file, live or archived"
            )
        if plan_path not in self._status:
            values = _read_scalars(str(plan_path))
            self._status[plan_path] = (values or {}).get("status", "")
        status = self._status[plan_path]
        if status not in PLAN_TERMINAL_STATUS:
            return (
                f"{REASON_FORWARD_PLAN_NOT_TERMINAL}: plan {plan_fk!r} status is "
                f"{status!r}, not terminal"
            )
        return None


def _is_roadmap_exit(route: str, xl_exit: str) -> bool:
    if route == _ROUTE_ROADMAP:
        return True
    return route == _ROUTE_PM_DECISION and xl_exit == _XL_EXIT_ROADMAP


def _citer_is_terminal(path: Path) -> bool:
    """A handoff citer is terminal by `deployment_state`; a plan citer by `status`."""
    if "handoffs" in path.parts:
        deployment_state = (parse_frontmatter_field(path, "deployment_state") or "").strip().lower()
        return deployment_state in HANDOFF_TERMINAL_DEPLOYMENT
    return parse_frontmatter_status(path) in PLAN_TERMINAL_STATUS


def _build_citer_index(worktree_root: Path) -> Dict[str, List[Path]]:
    """One walk of `_CITER_SEARCH_SUBDIRS`, grouped by cited sizing basename. The byte prefilter
    skips the YAML parse of every file that never names `sizing_object`."""
    index: Dict[str, List[Path]] = {}
    for subdir in _CITER_SEARCH_SUBDIRS:
        root = worktree_root.joinpath(*subdir)
        if not root.is_dir():
            continue
        for candidate in sorted(root.rglob("*.md")):
            try:
                raw = candidate.read_bytes()
            except OSError:
                continue
            if b"sizing_object" not in raw:
                continue
            cited = parse_frontmatter_field(candidate, "sizing_object")
            if not cited:
                continue
            basename = os.path.basename(cited.replace("\\", "/"))
            index.setdefault(basename, []).append(candidate)
    return index


class LazyCiterIndex:
    """`_build_citer_index`'s result, built on first lookup: only a roadmap-exit sizing asks."""

    def __init__(self, worktree_root: Path) -> None:
        self._root = worktree_root
        self._index: Optional[Dict[str, List[Path]]] = None

    def get(self, key: str, default: List[Path]) -> List[Path]:
        if self._index is None:
            self._index = _build_citer_index(self._root)
        return self._index.get(key, default)


def _roadmap_refusal(root: Path, record: SizingRecord, citer_index: Mapping[str, Any]) -> Optional[str]:
    """A roadmap-exit sizing stays until every stub/plan citing it is terminal."""
    if not _is_roadmap_exit(record.route, record.xl_exit):
        return None
    basename = os.path.basename(record.rel)
    non_terminal = [
        rel_id(c, root) for c in citer_index.get(basename, []) if not _citer_is_terminal(c)
    ]
    if non_terminal:
        return f"{REASON_ROADMAP_STUBS_NOT_TERMINAL}: {', '.join(sorted(non_terminal))}"
    return None


def suffixed_dst(dst: Path, deliverable_id: str) -> Optional[Path]:
    """`dst` with the deliverable_id appended to the stem, or None when it is unusable."""
    safe = _UNSAFE_NAME_CHARS_RE.sub("_", deliverable_id.strip())
    if not safe:
        return None
    return dst.with_name(f"{dst.stem}--{safe}{dst.suffix}")


class DirNames:
    """Names present in each directory, listed once: a destination-collision probe per sizing
    answered from one `os.listdir` per archive month instead of one `stat` per sizing."""

    def __init__(self) -> None:
        self._names: Dict[str, Set[str]] = {}
        self._months: Dict[Tuple[str, str], Path] = {}

    def has(self, path: Path) -> bool:
        parent, name = os.path.split(str(path))
        return self._has(parent, name)

    def has_in(self, directory: Path, name: str) -> bool:
        return self._has(str(directory), name)

    def _has(self, parent: str, name: str) -> bool:
        names = self._names.get(parent)
        if names is None:
            try:
                names = set(os.listdir(parent))
            except OSError:
                names = set()
            self._names[parent] = names
        return name in names

    def month_dir(self, root: Path, yyyy_mm: str) -> Path:
        """`<root>/archive/sizings/<yyyy_mm>`, joined once per month."""
        key = (str(root), yyyy_mm)
        found = self._months.get(key)
        if found is None:
            found = self._months[key] = root / "archive" / "sizings" / yyyy_mm
        return found


def plan_one(
    root: Path,
    record: SizingRecord,
    plans: PlanResolver,
    citer_index: Mapping[str, Any],
    dir_names: Optional[DirNames] = None,
) -> Tuple[Optional[Move], Optional[str]]:
    """The refusal rails for one terminal sizing: forward-plan FK, roadmap stubs, archive date,
    destination collision. `(Move, None)` when it may move, `(None, reason)` when a rail refuses."""
    if record.plan:
        refusal = plans.refusal(record.plan)
        if refusal is not None:
            return None, refusal

    roadmap = _roadmap_refusal(root, record, citer_index)
    if roadmap is not None:
        return None, roadmap

    name = os.path.basename(record.rel)
    yyyy_mm = derive_yyyy_mm(name)
    if yyyy_mm is None:
        return None, f"cannot-derive-date: filename {name!r} has no YYYY-MM-DD prefix"

    src = root / record.rel
    month = dir_names.month_dir(root, yyyy_mm) if dir_names is not None else root / "archive" / "sizings" / yyyy_mm
    dst = month / name
    force = False
    if dir_names is not None:
        names = dir_names

        def exists(path: Path) -> bool:
            return names.has_in(month, path.name)
    else:
        exists = Path.exists
    if exists(dst):
        if not _is_identical_duplicate(src, dst):
            # A different file occupies the destination: land beside it under a
            # deliverable_id-suffixed name, never clobber, refuse when none is usable.
            alt = suffixed_dst(dst, record.deliverable_id) if record.deliverable_id else None
            if alt is None:
                return None, _REASON_DEST_CONFLICT
            dst = alt
            if exists(dst):
                if not _is_identical_duplicate(src, dst):
                    return None, _REASON_DEST_CONFLICT
                force = True
        else:
            force = True
    return Move(src=src, dst=dst, candidate_id=record.rel, force=force), None


def plan_moves(
    scan: SizingScan,
    citer_index: Mapping[str, Any],
    plans: Optional[PlanResolver] = None,
) -> Tuple[List[Move], List[dict]]:
    """`(moves, skipped)`: a `Move` per movable terminal sizing and a `{id, reason}` per refused
    one (forward-plan-not-terminal, roadmap-stubs-not-terminal, cannot-derive-date, collision).
    `citer_index` maps cited sizing basename to citer paths, consulted only for roadmap-exit
    candidates. Pure: reads, never writes."""
    resolver = plans if plans is not None else PlanResolver(scan.repo_root)
    moves: List[Move] = []
    skipped: List[dict] = []
    dir_names = DirNames()
    for record in scan.terminal():
        move, reason = plan_one(scan.repo_root, record, resolver, citer_index, dir_names)
        if move is None:
            skipped.append({"id": record.rel, "reason": reason})
        else:
            moves.append(move)
    return moves, skipped


# ---------------------------------------------------------------------------
# Drain
# ---------------------------------------------------------------------------

Commit = Callable[[Path, List[Move], str], Awaitable[Tuple[List[dict], List[dict]]]]

# Past this many moves the artifact tier of the Deliverable-Id ladder reads one file per path to
# learn the artifacts diverge; a bulk drain's paths always do, so the ladder runs without them.
_TRAILER_PATHS_MAX = 16


def _revert_moves(moves: List[Move], created: List[Path]) -> None:
    for m in moves:
        if m.dst.exists() and not m.src.exists():
            try:
                m.dst.rename(m.src)
            except OSError:
                pass
    _cleanup_created_dirs(created)


async def _restore_index_once(root: Path, records: bytes) -> bool:
    """One `git update-index --index-info -z` over the whole batch: NUL-terminated
    `<mode> <sha>\\t<path>` records on stdin (mode 0 drops the entry), so no pathspec matching
    runs and the Windows command-line cap never chunks it. False on any failure; the caller then
    runs the retrying, chunked resync."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "git", "update-index", "-z", "--index-info",
            cwd=str(root),
            env=_make_git_env(),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            **no_console_creationflags(),
        )
        await proc.communicate(records)
    except OSError:
        return False
    return proc.returncode == 0


async def land_moves(root: Path, moves: List[Move], subject: str) -> Tuple[List[dict], List[dict]]:
    """Rename every `Move` and land them as ONE commit with one git spawn: the main index is
    restored from HEAD for the moved paths. `(acted, failed)` as `archive_and_commit` returns them.

    The tree-delta is built from HEAD's own blob for each src (a sizing move is a pure rename),
    so no worktree byte is hashed to build the commit and an untracked src fails individually
    before anything is renamed. A failed commit reverses every rename. If the one index restore
    fails (lock held), the retrying, chunked resync that `archive_and_commit` uses finishes the job."""
    root_s = str(root)
    cut = len(root_s) + 1

    def rel(path: Path) -> str:
        return str(path)[cut:].replace("\\", "/")

    old_head = head_sha(root)
    if old_head is None:
        reason = "index-init-failed: HEAD has no resolvable commit"
        return [], [{"id": m.candidate_id, "reason": reason} for m in moves]

    src_rel = {m.candidate_id: rel(m.src) for m in moves}
    dst_rel = {m.candidate_id: rel(m.dst) for m in moves}
    spine = read_tree_spine(root, list(src_rel.values()) + list(dst_rel.values()))
    if spine is None:
        reason = "spine-unresolvable: could not read HEAD's tree spine"
        return [], [{"id": m.candidate_id, "reason": reason} for m in moves]

    failed: List[dict] = []
    assembled: Dict[str, Any] = {}
    todo: List[Move] = []
    for m in moves:
        parent, _, leaf = src_rel[m.candidate_id].rpartition("/")
        entry = spine.get(parent, {}).get(leaf)
        if entry is None:
            failed.append({"id": m.candidate_id, "reason": "untracked-at-head: src has no HEAD tree entry"})
            continue
        assembled[dst_rel[m.candidate_id]] = entry
        assembled[src_rel[m.candidate_id]] = _ABSENT
        todo.append(m)
    if not todo:
        return [], failed

    created: List[Path] = []
    dir_names = DirNames()
    for parent in sorted({m.dst.parent for m in todo}):
        created.extend(_mkdir_and_track_created(parent))
    renamed: List[Move] = []
    for m in todo:
        if not m.force and dir_names.has(m.dst):
            failed.append({"id": m.candidate_id, "reason": "dst-exists: refusing overwrite (force=False)"})
            assembled.pop(dst_rel[m.candidate_id], None)
            assembled.pop(src_rel[m.candidate_id], None)
            continue
        try:
            os.replace(str(m.src), str(m.dst))
        except OSError as exc:
            failed.append({"id": m.candidate_id, "reason": f"replace-failed: {exc}"})
            assembled.pop(dst_rel[m.candidate_id], None)
            assembled.pop(src_rel[m.candidate_id], None)
            continue
        renamed.append(m)
    if not renamed:
        _cleanup_created_dirs(created)
        return [], failed

    message = _message_with_hookless_trailers(
        subject, root, [str(m.dst) for m in renamed] if len(renamed) <= _TRAILER_PATHS_MAX else None
    )
    msg_fd, msg_path = tempfile.mkstemp(prefix="fleet-git-msg-", dir=managed_tempdir(root))
    try:
        with os.fdopen(msg_fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(message)
        landed = _commit_via_head_spine(
            root, assembled, old_head, msg_path, create_missing_dirs=True, caller="terminal_sizing_drain"
        )
    except Exception:
        _revert_moves(renamed, created)
        raise
    finally:
        try:
            os.unlink(msg_path)
        except OSError:
            pass
    if landed is None or not landed.ok:
        err = "spine-commit-preconditions-failed: nothing was committed" if landed is None else (
            landed.stderr.strip() or "spine-commit-failed"
        )
        _revert_moves(renamed, created)
        return [], failed + [{"id": m.candidate_id, "reason": f"commit-failed: {err}"} for m in renamed]

    invalidate_git_history_cache()
    acted = [{"id": m.candidate_id, "archived": True} for m in renamed]
    zero = "0" * len(assembled[dst_rel[renamed[0].candidate_id]][1])
    records = "".join(
        f"0 {zero}\t{src_rel[m.candidate_id]}\0"
        f"{assembled[dst_rel[m.candidate_id]][0]:o} {assembled[dst_rel[m.candidate_id]][1]}\t{dst_rel[m.candidate_id]}\0"
        for m in renamed
    )
    if not await _restore_index_once(root, records.encode("utf-8")):
        await _resync_main_index_for_moves(
            renamed,
            {a["id"]: a for a in acted},
            worktree_root=root,
            env=_make_git_env(),
            committed_blobs={m.candidate_id: assembled[dst_rel[m.candidate_id]][1] for m in renamed},
        )
    try:
        await _index_resync_drain.drain_pending_resyncs(root)
    except Exception:  # a drain fault never fails the landed commit
        pass
    session_scope.release_committed_claims_in_process_or_retain(
        root,
        (p for m in renamed for p in (src_rel[m.candidate_id], dst_rel[m.candidate_id])),
        session_core.resolve_session_id(root_s),
        "terminal_sizing_drain",
    )
    return acted, failed


def dirty_among(root: Path, rels: Iterable[str]) -> Set[str]:
    """The subset of `rels` carrying uncommitted changes, on both the index and worktree axes.
    Answered spawn-free from one index walk when the repo is within its verified preconditions;
    otherwise one `git status` over `state/sizings`, where a failed status fails closed (every
    rel dirty)."""
    wanted = set(rels)
    in_process: Optional[Set[str]] = None
    if (root / ".git").exists():
        from coordinator_core.ops.fleet.archive_terminal_handoffs import _dirty_relpaths_in_process

        try:
            in_process = _dirty_relpaths_in_process(root, sorted(wanted))
        except Exception:  # a decline: ask git instead, never a narrower answer
            in_process = None
    if in_process is not None:
        return in_process & wanted
    dirty = dirty_relpaths_from_porcelain(root, [_SIZINGS_REL], caller="terminal_sizing_drain")
    # The directory pathspec itself in `dirty` is the fail-closed sentinel (git failed).
    # The in-process arm reports an untracked path dirty; whether this arm does depends on
    # porcelain's `??` handling, and the commit's git-mv refuses an untracked path either way.
    if _SIZINGS_REL in dirty:
        return wanted
    return dirty & wanted


def _select(root: Path, candidate_ids: List[str]) -> Tuple[SizingScan, List[dict], List[dict]]:
    """A scan over only the named ids, plus the `failed` and `skipped` entries their re-verify
    produced: path-traversal fails, a missing source is already-archived, a non-terminal status
    is terminality-drift."""
    scan = SizingScan(repo_root=root)
    failed: List[dict] = []
    skipped: List[dict] = []
    sizings_dir = Path(record_homes.home_dir(str(root), "sizings")).resolve()
    for cid in candidate_ids:
        path = (root / cid).resolve()
        if not path.is_relative_to(sizings_dir):
            failed.append({"id": cid, "reason": "path-traversal: candidate_id escapes state/sizings/"})
            continue
        if not path.exists():
            skipped.append({"id": cid, "reason": REASON_ALREADY_ARCHIVED})
            continue
        record = read_record(root, cid)
        if record is None or not record.terminal:
            status = record.status if record is not None else None
            skipped.append({"id": cid, "reason": f"terminality-drift: status is now {status!r}"})
            continue
        scan.records[cid] = record
    return scan, failed, skipped


async def drain_async(
    repo_root: Path,
    *,
    dry_run: bool = False,
    candidate_ids: Optional[List[str]] = None,
    commit: Optional[Commit] = None,
    dirty: Optional[Callable[[Path, List[str]], Set[str]]] = None,
) -> Dict[str, Any]:
    """Scan, plan, and (unless `dry_run`) archive every movable terminal sizing in one commit.

    `candidate_ids` restricts the drain to those sizings, re-verified at act time; omitted, every
    terminal sizing is a candidate. `commit` defaults to `archive_and_commit` and `dirty` to
    `dirty_among`; a caller passes its own to be patchable. Returns
    `{candidates, acted, skipped, failed, scan_skipped}`; `scan_skipped` is the refusal census
    (rail refusals only). `dry_run=True` mutates nothing and spawns nothing."""
    root = Path(repo_root)
    failed: List[dict] = []
    skipped: List[dict] = []
    if candidate_ids is None:
        scan = scan_sizings(root)
    else:
        scan, failed, skipped = _select(root, list(candidate_ids))

    citer_index = LazyCiterIndex(root)
    moves, scan_skipped = plan_moves(scan, citer_index)
    candidates = [
        {
            "id": m.candidate_id,
            "title": scan.records[m.candidate_id].title or Path(m.candidate_id).stem,
            "status": scan.records[m.candidate_id].status,
            "family": "sizing",
            "terminal_since": None,
            "note": None,
        }
        for m in moves
    ]
    result: Dict[str, Any] = {
        "candidates": candidates,
        "acted": [],
        "skipped": skipped,
        "failed": failed,
        "scan_skipped": scan_skipped,
    }
    if dry_run:
        return result

    skipped.extend(scan_skipped)
    if moves:
        retained = (dirty or dirty_among)(root, [m.candidate_id for m in moves])
        for m in moves:
            if m.candidate_id in retained:
                skipped.append({"id": m.candidate_id, "reason": REASON_WORKTREE_DIRTY})
        moves = [m for m in moves if m.candidate_id not in retained]
    if not moves:
        return result

    committer = commit if commit is not None else land_moves
    subject = f"fleet: archive {len(moves)} terminal sizing-object(s) [fleet.archive_terminal_sizings]"
    raw_acted, raw_failed = await committer(root, moves, subject)
    acted_ids = {a["id"] for a in raw_acted}
    failed_by_id = {f["id"]: f["reason"] for f in raw_failed}
    for m in moves:
        cid = m.candidate_id
        if cid in failed_by_id:
            failed.append({"id": cid, "reason": failed_by_id[cid]})
        elif cid in acted_ids:
            result["acted"].append({"id": cid, "archived": True})
        else:
            failed.append({"id": cid, "reason": "unexpected: absent from acted and failed"})
    return result


def drain(repo_root: Path, *, dry_run: bool = False) -> Dict[str, Any]:
    """Synchronous `drain_async` for callers outside a running event loop."""
    return asyncio.run(drain_async(repo_root, dry_run=dry_run))

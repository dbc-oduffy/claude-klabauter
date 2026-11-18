"""Pure planner for the canonical handoff layout.

Canonical: live batons at `state/handoffs/YYYY-MM/<file>.md`, terminal batons
(`HANDOFF_TERMINAL_DEPLOYMENT`) at `archive/handoffs/YYYY-MM/<file>.md`, month key = the
filename's leading date in both trees. `plan_layout` reads the tree and returns the moves that
reach that layout; it never mutates, never spawns a process, and an applied plan re-plans empty.

Negative-spec: an undated name is never given a destination (both `shard_path` and
`handoff_archive_dest` would emit a flat path); a live baton carrying `claimed_by` is never moved;
a terminal baton moves even when claimed; a destination that exists is never overwritten.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import NamedTuple

from coordinator_core.lifecycle_constants import HANDOFF_TERMINAL_DEPLOYMENT
from coordinator_core.session import output_tree_layout as _layout

LIVE_TREE = "state/handoffs"
ARCHIVE_TREE = "archive/handoffs"

_HEAD_BYTES = 16384
_EMPTY_FIELD_VALUES = frozenset({"", "null", "~", "none", "[]", "''", '""'})


class Move(NamedTuple):
    """One planned rename; paths are repo-relative POSIX."""

    src: str
    dst: str
    reason: str


class Skip(NamedTuple):
    """A path the plan leaves untouched and why."""

    path: str
    reason: str


@dataclass(frozen=True)
class LayoutPlan:
    """The moves, refusals and emptied directories that reach the canonical layout.

    `moves` and `skipped` are sorted by path; `remove_dirs` are directories that are empty once
    every move has been applied; `unclassified_dirs` are untouched and need a human look.
    """

    moves: tuple[Move, ...]
    skipped: tuple[Skip, ...]
    unclassified_dirs: tuple[str, ...]
    remove_dirs: tuple[str, ...]

    @property
    def counts(self) -> dict[str, int]:
        """Totals keyed `moves`, `skipped`, `unclassified_dirs`, `remove_dirs`, `move:<reason>`, `skip:<reason>`."""
        out: dict[str, int] = {
            "moves": len(self.moves),
            "skipped": len(self.skipped),
            "unclassified_dirs": len(self.unclassified_dirs),
            "remove_dirs": len(self.remove_dirs),
        }
        for m in self.moves:
            out[f"move:{m.reason}"] = out.get(f"move:{m.reason}", 0) + 1
        for s in self.skipped:
            out[f"skip:{s.reason}"] = out.get(f"skip:{s.reason}", 0) + 1
        return out


def _month_of(name: str) -> str | None:
    """Leading-date month of `name` when it is a real month (01-12), else None."""
    month = _layout._leading_date_month(name)
    if month is None or not 1 <= int(month[5:7]) <= 12 or not 1900 <= int(month[:4]) <= 2100:
        return None
    return month


def _scan(path: str) -> list[os.DirEntry]:
    try:
        with os.scandir(path) as it:
            return sorted(it, key=lambda e: e.name)
    except OSError:
        return []


def _head_fields(path: str) -> tuple[str, bool]:
    """`(deployment_state lowercased, claimed)` from one frontmatter-head read."""
    from coordinator_core.frontmatter.primitives import read_fm_field_unquoted, split_frontmatter

    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            text = fh.read(_HEAD_BYTES)
            split = split_frontmatter(text)
            if split is None and text.lstrip().startswith("---"):
                text += fh.read()
                split = split_frontmatter(text)
    except OSError:
        return "", False
    if split is None:
        return "", False
    state = (read_fm_field_unquoted(split.fm_text, "deployment_state") or "").strip().lower()
    claimed = (read_fm_field_unquoted(split.fm_text, "claimed_by") or "").strip().lower()
    return state, claimed not in _EMPTY_FIELD_VALUES


class _Planner:
    def __init__(self, repo_root: str) -> None:
        from coordinator_core.ops.fleet._common import handoff_archive_dest
        from pathlib import Path

        self.root = repo_root
        self._root_path = Path(repo_root)
        self._archive_dest = handoff_archive_dest
        self.moves: list[Move] = []
        self.skipped: list[Skip] = []
        self.unclassified: list[str] = []
        self.remove_dirs: list[str] = []
        self._targets: set[str] = set()

    def _archive_rel(self, name: str) -> str:
        dest = self._archive_dest(self._root_path, self._root_path / name)
        return dest.relative_to(self._root_path).as_posix()

    def _emit(self, src: str, dst: str, reason: str) -> bool:
        if src == dst:
            return False
        if dst in self._targets or os.path.lexists(os.path.join(self.root, dst)):
            self.skipped.append(Skip(src, "collision"))
            return False
        self._targets.add(dst)
        self.moves.append(Move(src, dst, reason))
        return True

    def _route(self, src: str, name: str, prefix: str, *, archive_only: bool = False) -> bool:
        """Plan the move of baton file `src` (named `name`) to its canonical home; True when a move was emitted."""
        if _month_of(name) is None:
            self.skipped.append(Skip(src, "undated"))
            return False
        if archive_only:
            return self._emit(src, self._archive_rel(name), f"{prefix}-terminal")
        state, claimed = _head_fields(os.path.join(self.root, src))
        if state in HANDOFF_TERMINAL_DEPLOYMENT:
            return self._emit(src, self._archive_rel(name), f"{prefix}-terminal")
        if claimed:
            self.skipped.append(Skip(src, "claimed"))
            return False
        return self._emit(src, _layout.shard_path(LIVE_TREE, name), f"{prefix}-live")

    def live_tree(self) -> None:
        base = os.path.join(self.root, *LIVE_TREE.split("/"))
        for entry in _scan(base):
            rel = f"{LIVE_TREE}/{entry.name}"
            if entry.is_dir(follow_symlinks=False):
                if _layout.is_month_dir(entry.name):
                    self._month_dir(rel)
                else:
                    self._stray_dir(rel, entry.name)
            elif entry.name.endswith(".md") and not _layout.is_exempt(entry.name):
                self._route(rel, entry.name, "flat")

    def _month_dir(self, rel: str) -> None:
        month = rel.rsplit("/", 1)[-1]
        for entry in _scan(os.path.join(self.root, *rel.split("/"))):
            child = f"{rel}/{entry.name}"
            if entry.is_dir(follow_symlinks=False):
                self.unclassified.append(child)
            elif entry.name.endswith(".md") and not _layout.is_exempt(entry.name):
                state, claimed = _head_fields(entry.path)
                if state in HANDOFF_TERMINAL_DEPLOYMENT:
                    if _month_of(entry.name) is None:
                        self.skipped.append(Skip(child, "undated"))
                    else:
                        self._emit(child, self._archive_rel(entry.name), "shard-terminal")
                elif _month_of(entry.name) is None:
                    self.skipped.append(Skip(child, "undated"))
                elif _month_of(entry.name) != month and not claimed:
                    self._emit(child, _layout.shard_path(LIVE_TREE, entry.name), "shard-misfiled")

    def _stray_dir(self, rel: str, name: str) -> None:
        entries = _scan(os.path.join(self.root, *rel.split("/")))
        if name == ".archive":
            before = len(self.moves)
            for entry in entries:
                if entry.is_file(follow_symlinks=False) and entry.name.endswith(".md"):
                    self._route(f"{rel}/{entry.name}", entry.name, "dotarchive", archive_only=True)
            handled = len(self.moves) - before
            # an entry set fully moved (or empty) leaves the dir removable
            if handled == len(entries):
                self.remove_dirs.append(rel)
            else:
                self.unclassified.append(rel)
            return
        batons = [e for e in entries if e.is_file(follow_symlinks=False) and e.name.endswith(".md")]
        if len(entries) == 1 and len(batons) == 1:
            if self._route(f"{rel}/{batons[0].name}", batons[0].name, "dir"):
                self.remove_dirs.append(rel)
            return
        if not entries:
            self.remove_dirs.append(rel)
            return
        self.unclassified.append(rel)

    def archive_tree(self) -> None:
        base = os.path.join(self.root, *ARCHIVE_TREE.split("/"))
        for entry in _scan(base):
            rel = f"{ARCHIVE_TREE}/{entry.name}"
            if entry.is_dir(follow_symlinks=False):
                if not _layout.is_month_dir(entry.name):
                    self.unclassified.append(rel)
            elif entry.name.endswith(".md") and not _layout.is_exempt(entry.name):
                self._route(rel, entry.name, "archive-flat", archive_only=True)


def plan_layout(repo_root: str) -> LayoutPlan:
    """Plan the moves that bring `repo_root`'s handoff trees to the canonical layout.

    Reads directory listings and one frontmatter head per baton; mutates nothing. An absent tree
    plans nothing. Output is deterministic (sorted) and a plan applied in full re-plans empty.
    """
    p = _Planner(str(repo_root))
    p.live_tree()
    p.archive_tree()
    return LayoutPlan(
        moves=tuple(sorted(p.moves)),
        skipped=tuple(sorted(p.skipped)),
        unclassified_dirs=tuple(sorted(p.unclassified)),
        remove_dirs=tuple(sorted(p.remove_dirs)),
    )

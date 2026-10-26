"""Chooses the bytes a grind landing commits for each path from its owning rows' recorded images.

``select`` takes ``{path: [row ids in row order]}`` and reports drops per row (``{path: {row_id: reason}}``).
Each fix row records a ``pre`` and a ``post`` blob per locked file (``grind-row image``). Every blob is
first normalised to checkin form, then the owners' pre->post deltas are folded in row order over the path's
base (HEAD's bytes; for a path absent from HEAD, the first owner's pre): an owner whose ``pre`` equals the
running bytes lands ``post``, any other lands its delta applied onto them (``own_delta_merge``), and an
owner whose delta conflicts drops alone. Without images the worktree bytes land only when no live peer
holds the path. Zero spawns: one HeadView over the landing paths plus in-process blob reads and writes.

Residual: a peer edit made inside a row's own pre-to-post window is indistinguishable from the row's edit
and lands with it; the import-closure gate is the backstop for that class.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Mapping, Optional, Sequence

from coordinator_core.git import commit
from coordinator_core.git.git_dir import resolve_git_common_dir
from coordinator_core.git.git_objects import read_object, write_object
from coordinator_core.git.own_delta_merge import apply_own_delta
from coordinator_core.ops.dispatch_emit import grind_commit_contract as contract


@dataclass
class Selection:
    """``landed_text(rel)``: decoded bytes this commit lands for ``rel`` (None when undecodable or absent).
    ``supplied_blobs``: ``{path: sha}`` for paths whose landed bytes are not the worktree bytes.
    ``dropped``: ``{path: {row_id: DROP_* reason}}``; a path whose landed bytes are HEAD's carries every owner."""

    landed_text: Callable[[str], Optional[str]]
    supplied_blobs: Dict[str, str] = field(default_factory=dict)
    dropped: Dict[str, Dict[str, str]] = field(default_factory=dict)

    def dropped_for(self, row_id: str, path: str) -> Optional[str]:
        """The ``DROP_*`` reason ``row_id`` lost ``path`` for, or None when its edit of ``path`` lands."""
        return self.dropped.get(path, {}).get(row_id)


_BAD = object()


def _load_images(root: Path, run_id: str, row_id: str):
    """``{phase: {path: sha|None}}``; None when no record file exists; ``_BAD`` when it is unreadable or malformed."""
    f = root / contract.row_images_rel(run_id, row_id) / contract.IMAGE_RECORD_FILE
    if not f.exists():
        return None
    try:
        rec = json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return _BAD
    if not isinstance(rec, dict):
        return _BAD
    out: Dict[str, Dict[str, Optional[str]]] = {}
    for phase in contract.IMAGE_PHASES:
        part = rec.get(phase)
        if not isinstance(part, dict) or not all(
            isinstance(k, str) and (v is None or isinstance(v, str)) for k, v in part.items()
        ):
            return _BAD
        out[phase] = part
    return out


def _live_peer_held(root: Path, paths: List[str]) -> set:
    if not paths:
        return set()
    from coordinator_core.ops.ceremony.commit_v2 import _peer_claim_warnings

    holds: list = []
    _peer_claim_warnings(root, paths, peer_holds=holds)
    return {p for p, _sid in holds}


def select(main_root, run_id: str, owners: Mapping[str, Sequence[str]]) -> Selection:
    """Select the landed bytes of each ``{path: [declaring row ids in row order]}``; see the module docstring.
    Owners fold in the given order; a drop costs only the owner it names, except a path no owner could
    land, which carries every owner and lands HEAD's bytes."""
    from coordinator_core.authoring_leaks import HeadView

    root = Path(main_root)
    head = HeadView(root, list(owners))
    common = resolve_git_common_dir(root)
    texts: Dict[str, Optional[bytes]] = {}
    supplied: Dict[str, str] = {}
    per_row: Dict[str, Dict[str, str]] = {}
    unimaged: List[str] = []
    records: Dict[str, object] = {}

    def worktree(path: str) -> Optional[bytes]:
        try:
            return (root / path).read_bytes()
        except OSError:
            return None

    def blob(sha: Optional[str]) -> Optional[bytes]:
        if not sha:
            return None
        got = read_object(common, sha)
        return got[1] if got is not None and got[0] == "blob" else None

    def image(path: str, sha: Optional[str]) -> Optional[bytes]:
        # Trap: raw CRLF images against an LF HEAD differ on every line, so every delta would collide.
        raw = blob(sha) if sha is not None else b""
        if raw is None or sha is None:
            return raw
        return commit.checkin_form(root, path, raw)

    def record(row_id: str):
        if row_id not in records:
            records[row_id] = _load_images(root, run_id, row_id)
        return records[row_id]

    def drop(path: str, row_id: str, reason: str) -> None:
        per_row.setdefault(path, {})[row_id] = reason

    for path, rows in owners.items():
        imaged = [
            r for r in rows
            if isinstance(record(r), dict) and path in record(r)["pre"]
        ]
        if not imaged and not any(record(r) is _BAD for r in rows):
            unimaged.append(path)
            continue
        if not imaged:
            for r in rows:
                drop(path, r, contract.DROP_UNATTRIBUTED_SHARED)
            continue

        in_head = head.entry(path) is not None
        first_pre = record(imaged[0])["pre"][path]
        if in_head:
            current = head.head_bytes(path) or b""
        else:
            current = image(path, first_pre)
            if current is None:
                current = b""
            if first_pre is not None and _live_peer_held(root, [path]):
                for r in rows:
                    drop(path, r, contract.DROP_UNATTRIBUTED_SHARED)
                continue

        landed_any = False
        deleted = False
        for r in rows:
            rec = record(r)
            if r not in imaged:
                drop(path, r, contract.DROP_UNATTRIBUTED_SHARED)
                continue
            if path not in rec["post"]:
                drop(path, r, contract.DROP_UNATTRIBUTED_SHARED)
                continue
            pre_sha, post_sha = rec["pre"][path], rec["post"][path]
            if post_sha is None:
                if len(rows) == 1 and worktree(path) is None:
                    deleted = True
                else:
                    drop(path, r, contract.DROP_UNATTRIBUTED_SHARED)
                continue
            pre, post = image(path, pre_sha), image(path, post_sha)
            if pre is None or post is None:
                drop(path, r, contract.DROP_UNATTRIBUTED_SHARED)
                continue
            nxt = post if pre == current else apply_own_delta(pre, post, current)
            if nxt is None:
                drop(path, r, contract.DROP_PEER_HUNKS_CONFLICT)
                continue
            current = nxt
            landed_any = True

        if deleted:
            texts[path] = None
        elif landed_any:
            texts[path] = current
            wt = worktree(path)
            wt_form = None if wt is None else (commit.checkin_form(root, path, wt) or wt)
            if wt_form != current:
                supplied[path] = write_object(common, b"blob", current)

    held = _live_peer_held(root, unimaged)
    for path in unimaged:
        if path in held:
            for r in owners[path]:
                drop(path, r, contract.DROP_UNATTRIBUTED_SHARED)

    def landed_text(rel: str) -> Optional[str]:
        all_owners_dropped = rel in per_row and len(per_row[rel]) == len(owners[rel])
        data = texts[rel] if rel in texts else (head.head_bytes(rel) if all_owners_dropped else worktree(rel))
        if data is None:
            return None
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError:
            return None

    return Selection(landed_text=landed_text, supplied_blobs=supplied, dropped=per_row)

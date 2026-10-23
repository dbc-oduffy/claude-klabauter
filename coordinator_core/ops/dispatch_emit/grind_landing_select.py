"""Chooses the bytes a grind landing commits for each path from the owning row's recorded images.

Each fix row records a ``pre`` and a ``post`` blob per locked file (``grind-row image``). A path whose
``pre`` equals HEAD lands ``post``; a path whose ``pre`` differs lands the row's pre->post delta applied
onto HEAD (``own_delta_merge``), or is dropped on conflict. Without images the worktree bytes land only
when no live peer holds the path. Zero spawns: one HeadView over the landing paths plus in-process blob
reads and writes.

Residual: a peer edit made inside a row's own pre-to-post window is indistinguishable from the row's edit
and lands with it; the import-closure gate is the backstop for that class.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Mapping, Optional

from coordinator_core.git.git_dir import resolve_git_common_dir
from coordinator_core.git.git_objects import read_object, write_object
from coordinator_core.git.own_delta_merge import apply_own_delta
from coordinator_core.ops.dispatch_emit import grind_commit_contract as contract


@dataclass
class Selection:
    """``landed_text(rel)``: decoded bytes this commit lands for ``rel`` (None when undecodable or absent).
    ``supplied_blobs``: ``{path: sha}`` for paths whose landed bytes are not the worktree bytes.
    ``dropped``: ``{path: DROP_* reason}`` for paths that must not land."""

    landed_text: Callable[[str], Optional[str]]
    supplied_blobs: Dict[str, str] = field(default_factory=dict)
    dropped: Dict[str, str] = field(default_factory=dict)


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


def select(main_root, run_id: str, owners: Mapping[str, str]) -> Selection:
    """Select the landed bytes of each ``{path: owning row id}``; see the module docstring."""
    from coordinator_core.authoring_leaks import HeadView

    root = Path(main_root)
    head = HeadView(root, list(owners))
    common = resolve_git_common_dir(root)
    texts: Dict[str, Optional[bytes]] = {}
    supplied: Dict[str, str] = {}
    dropped: Dict[str, str] = {}
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

    for path, row_id in owners.items():
        if row_id not in records:
            records[row_id] = _load_images(root, run_id, row_id)
        rec = records[row_id]
        if rec is _BAD:
            dropped[path] = contract.DROP_UNATTRIBUTED_SHARED
            continue
        if rec is None or (path not in rec["pre"] and path not in rec["post"]) or (
            path in rec["post"] and path not in rec["pre"]
        ):
            unimaged.append(path)
            continue
        if path not in rec["post"]:
            dropped[path] = contract.DROP_UNATTRIBUTED_SHARED
            continue
        pre_sha, post_sha = rec["pre"][path], rec["post"][path]
        if post_sha is None:
            if worktree(path) is not None:
                dropped[path] = contract.DROP_UNATTRIBUTED_SHARED
            else:
                texts[path] = None
            continue
        post = blob(post_sha)
        pre = blob(pre_sha) if pre_sha is not None else b""
        if post is None or pre is None:
            dropped[path] = contract.DROP_UNATTRIBUTED_SHARED
            continue
        entry = head.entry(path)
        head_sha = entry[1] if entry is not None else None
        if (pre_sha or None) == head_sha:
            landed = post
        else:
            base = head.head_bytes(path) or b""
            landed = apply_own_delta(pre, post, base)
            if landed is None:
                dropped[path] = contract.DROP_PEER_HUNKS_CONFLICT
                continue
        texts[path] = landed
        if landed != worktree(path):
            supplied[path] = write_object(common, b"blob", landed)

    held = _live_peer_held(root, unimaged)
    for path in unimaged:
        if path in held:
            dropped[path] = contract.DROP_UNATTRIBUTED_SHARED

    def landed_text(rel: str) -> Optional[str]:
        data = texts[rel] if rel in texts else (head.head_bytes(rel) if rel in dropped else worktree(rel))
        if data is None:
            return None
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError:
            return None

    return Selection(landed_text=landed_text, supplied_blobs=supplied, dropped=dropped)

"""Halt record of the rebuilt landing op: at any halt, the base blob and the run blob of every declared path
are written to the run record's `touched-blobs.json` (`stranded_sibling.write`, the shape the halt record
already carries), so the owning EM can hand-land by scoped pathspec without losing a hunk.

Guard 2 of the contract: a halt that cannot record every declared path is refused as `HALT_RECORD_INCOMPLETE`
rather than returned as a halt. Zero git spawns: HEAD's tree and the worktree bytes are read in process.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Optional

from coordinator_core.ops.dispatch_emit import land_blobs_contract as contract


def _norm(path: str) -> str:
    text = str(path).replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    return text


def _recorded_blobs(record_dir: Path) -> dict:
    """`{path: blob sha}` from the run record's `kept.json`; empty when absent or unreadable."""
    try:
        kept = json.loads((record_dir / "kept.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    out: dict = {}
    for chunk in (kept.get("chunks") or {}).values() if isinstance(kept, dict) else ():
        for path, sha in ((chunk or {}).get("paths") or {}).items():
            if isinstance(sha, str) and sha:
                out[_norm(path)] = sha
    return out


def _worktree_blob(root: Path, rel: str) -> tuple:
    """`(sha, readable)`: the git blob sha of the worktree file, `(None, True)` when it is absent."""
    target = root / rel
    try:
        if target.is_symlink() or not target.is_file():
            return (None, True) if not target.exists() and not target.is_symlink() else (None, False)
        data = target.read_bytes()
    except OSError:
        return None, False
    return hashlib.sha1(b"blob %d\x00" % len(data) + data).hexdigest(), True


def build_halt_record(root: Path, declared: list, record_dir: Path) -> tuple:
    """`({path: {base, run}}, unrecorded)`: base blob (HEAD's tree entry, `None` when HEAD lacks the path) and run blob
    (the run's `kept.json` blob, else the worktree file's; `None` for a declared deletion) per declared path.
    `unrecorded` lists the paths either blob of which could not be read."""
    from coordinator_core.git import git_state

    paths = sorted({_norm(p) for p in declared})
    spine = git_state.read_tree_spine(root, paths) if paths else {"": {}}
    kept = _recorded_blobs(record_dir)
    entries: dict = {}
    unrecorded: list = []
    for rel in paths:
        d, _, name = rel.rpartition("/")
        if spine is None:
            unrecorded.append(rel)
            continue
        head_entry = spine.get(d, {}).get(name)
        run_sha, readable = _worktree_blob(root, rel)
        if not readable:
            unrecorded.append(rel)
            continue
        entries[rel] = {"base": head_entry[1] if head_entry else None, "run": kept.get(rel, run_sha)}
    return entries, unrecorded


def write_halt_record(root: Path, run_id: str, declared: list, record_dir: Optional[Path] = None) -> dict:
    """Write the halt's `{path: {base, run}}` through `stranded_sibling.write` (`touched-blobs.json` under the
    run's record dir; the one record shape the stranded-sibling guards read). Returns `{recorded: True, path,
    paths}`, or the `HALT_RECORD_INCOMPLETE` refusal naming `unrecorded_paths` when any declared path could
    not be recorded; an incomplete record is never left on disk."""
    from coordinator_core.ops.dispatch_emit import stranded_sibling
    from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT

    record_dir = record_dir or (root / RECORD_DIR_ROOT / run_id)
    entries, unrecorded = build_halt_record(root, declared, record_dir)
    if not unrecorded:
        target = stranded_sibling.write(record_dir, entries)
        if target is not None:
            return {"recorded": True, "path": target.as_posix(), "paths": sorted(entries)}
        (record_dir / stranded_sibling.TOUCHED_FILE).unlink(missing_ok=True)
        unrecorded = sorted(entries)
    return {"committed": False, "sha": None,
            "error": "halt record incomplete: base and run blobs not written for every declared path",
            "refused": contract.HALT_RECORD_INCOMPLETE, "unrecorded_paths": sorted(unrecorded)}


def halt_with_record(root: Path, run_id: str, declared: list, halt_reply: dict,
                     record_dir: Optional[Path] = None) -> dict:
    """`halt_reply` plus `halt_record` (the record path) once the record is written, else the
    `HALT_RECORD_INCOMPLETE` refusal in its place. No caller in `land_blobs.py` yet: `land_blobs` has no
    halt path, so guard 2 is unenforced until one routes through here."""
    result = write_halt_record(root, run_id, declared, record_dir)
    if not result.get("recorded"):
        return result
    return {**halt_reply, "halt_record": result["path"]}

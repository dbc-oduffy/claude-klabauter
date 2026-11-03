"""Retry reuse and orphan reap for a multi-ask parent minted but never started.

The fan-out writes `scratch/warp/ask-digest/<digest>.json` (`{parent_id, minted_at}`) beside the parent
record; a resubmission of the same asks finds it here (`find_reusable`) instead of minting a second
parent, and `reap_orphans` closes indexed parents that aged out unstarted: run records get an
`ABANDONED_AT_MINTED` halt marker and the scratch footprint is deleted; state/warp and state/sizings files
are never deleted. Both read the digest index only, never `scratch/warp` or `state/warp` as a corpus;
every touched path is named from the parent record's run ids. "Never started" is `halted_run._is_shell` and no live run token, the conjunction
`dispatch.emit abandon_run` uses for its minted-only close. Stdlib only: no git, no subprocess.

`python -m coordinator_core.ops.dispatch_emit.minted_parent --reap <parent_id>` reaps one named parent
under the same predicate (for orphans minted before the index existed).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any, Mapping, NamedTuple, Optional, Sequence

from coordinator_core.ops._path_guard import safe_id
from coordinator_core.ops.dispatch_emit import ask_fanout_contract as contract
from coordinator_core.ops.dispatch_emit import abandoned_minted_close, halted_run, live_run_index, run_token
from coordinator_core.ops.dispatch_emit.ask_contract import RUN_DIR_ROOT, record_dir
from coordinator_core.ops.dispatch_emit.fan_launcher import launcher_rel
from coordinator_core.ops.fleet._common import main_worktree_root
from coordinator_core.ops.warp_run_deliverable import ABANDONED_AT_MINTED, MINTED_STALE_SECONDS

REUSE_TTL_S = MINTED_STALE_SECONDS
REAP_PER_CALL = 8
DIGEST_DIR_REL = f"{RUN_DIR_ROOT}/ask-digest"


class Reusable(NamedTuple):
    """A minted-not-started parent a resubmission may launch again: `ready` is `(run_id, slice_sizing)` per ready run.
    `launcher` is None when the earlier attempt minted the slices but wrote no launcher; the caller writes one."""

    parent_id: str
    launcher: Optional[Path]
    ready: tuple[tuple[str, str], ...]


def asks_digest(fan_params: Mapping[str, Any]) -> str:
    """sha256 of the fan params that define a submission; the same asks from the same session give one digest."""
    basis = {
        "asks": fan_params.get("asks"),
        "join": fan_params.get("join"),
        "session_id": fan_params.get("session_id") or "",
        "sender": fan_params.get("sender"),
        "confirm_split": bool(fan_params.get("confirm_split", False)),
    }
    text = json.dumps(basis, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def digest_rel(digest: str) -> str:
    """Repo-relative POSIX path of the index entry for `digest`."""
    return f"{DIGEST_DIR_REL}/{digest}.json"


def _read_json(path: Path) -> Optional[dict]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _parent(wt: Path, parent_id: str) -> Optional[contract.ParentRecord]:
    if not safe_id(parent_id):
        return None
    data = _read_json(wt / contract.parent_record_rel(parent_id))
    if data is None:
        return None
    try:
        return contract.ParentRecord.from_json(data)
    except (KeyError, TypeError, ValueError):
        return None


def _never_started(wt: Path, run_id: str, abandoned_counts: bool) -> bool:
    """`halted_run._is_shell` and no live token (the `abandon_run` minted-only conjunction); with
    `abandoned_counts` a run the stale-minted reaper already halted `ABANDONED_AT_MINTED` also qualifies."""
    rdir = record_dir(wt, run_id)
    if run_token.is_live(wt, run_id):
        return False
    if halted_run._is_shell(rdir):
        return True
    if not abandoned_counts:
        return False
    marker = halted_run._read(rdir)
    return marker is not None and marker.get("halted") == ABANDONED_AT_MINTED


def _started_run(wt: Path, record: contract.ParentRecord, abandoned_counts: bool = False) -> Optional[str]:
    """The first run of `record` that left `minted` (or is unsafe to name), else None."""
    for run in record.runs:
        if not safe_id(run.run_id) or not _never_started(wt, run.run_id, abandoned_counts):
            return run.run_id
    return None


def _entry(path: Path) -> Optional[tuple[str, float]]:
    data = _read_json(path)
    if data is None:
        return None
    parent_id, minted_at = data.get("parent_id"), data.get("minted_at")
    if not isinstance(parent_id, str) or isinstance(minted_at, bool) or not isinstance(minted_at, (int, float)):
        return None
    return parent_id, float(minted_at)


def find_reusable(root: Any, digest: str, now: float) -> Optional[Reusable]:
    """The parent a submission with `digest` may reuse, else None: a fresh index entry, its parent record, and
    every run still minted-not-started. The launcher is optional (see `Reusable`)."""
    wt = main_worktree_root(Path(root))
    entry = _entry(wt / digest_rel(digest))
    if entry is None or now - entry[1] >= REUSE_TTL_S:
        return None
    parent_id = entry[0]
    record = _parent(wt, parent_id)
    launcher = wt / launcher_rel(parent_id)
    if record is None or not record.runs or _started_run(wt, record) is not None:
        return None
    ready = tuple((r.run_id, r.slice_sizing) for r in record.runs if r.status == contract.STATUS_READY)
    return Reusable(parent_id, launcher if launcher.is_file() else None, ready) if ready else None


def _remove(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path, ignore_errors=True)
    else:
        path.unlink(missing_ok=True)


def reap_parent(root: Any, parent_id: str) -> bool:
    """Close a minted-not-started parent: each still-shell run record gets an `ABANDONED_AT_MINTED` halt
    marker, then the parent's scratch footprint is deleted. Nothing under state/warp or state/sizings is ever
    deleted. False (nothing changed) when the record is unreadable, any run left `minted` or a marker write
    fails. Every path is named from the record's run ids, never discovered by listing."""
    wt = main_worktree_root(Path(root))
    record = _parent(wt, parent_id)
    if record is None or _started_run(wt, record, abandoned_counts=True) is not None:
        return False
    for run in record.runs:
        rdir = record_dir(wt, run.run_id)
        if not halted_run._is_shell(rdir):
            continue
        try:
            halted_run.write_halt_marker(
                rdir,
                halted=ABANDONED_AT_MINTED,
                sizing_rel=None,
                reason=f"abandoned: parent {parent_id} minted and never started; reaped",
            )
        except OSError:
            return False
    for run in record.runs:
        scratch_run = wt / RUN_DIR_ROOT / run.run_id
        if scratch_run != record_dir(wt, run.run_id):
            _remove(scratch_run)
        script = wt / RUN_DIR_ROOT / f"{run.run_id}.workflow.mjs"
        _remove(script)
        _remove(script.with_name(script.name + ".emitted.json"))
        live_run_index.remove(wt, run.run_id)
    launcher = wt / launcher_rel(parent_id)
    _remove(launcher)
    _remove(launcher.with_name(launcher.name + ".emitted.json"))
    _remove(wt / RUN_DIR_ROOT / parent_id)
    return True


def reap_orphans(root: Any, now: float, limit: int = REAP_PER_CALL) -> list[str]:
    """Reap up to `limit` index entries older than `REUSE_TTL_S`: a minted-not-started parent loses its files,
    a started or unreadable one only its entry. Then closes dead `abandoned_at_minted` markers with the rest of
    `limit` (`abandoned_minted_close.close_abandoned`). Returns the parent ids whose files were deleted."""
    wt = main_worktree_root(Path(root))
    index = wt / DIGEST_DIR_REL
    try:
        names = sorted(e.name for e in os.scandir(index) if e.name.endswith(".json"))
    except OSError:
        names = []
    reaped: list[str] = []
    just_halted: set[str] = set()
    handled = 0
    for name in names:
        if handled >= limit:
            break
        path = index / name
        entry = _entry(path)
        try:
            minted_at = entry[1] if entry else path.stat().st_mtime
        except OSError:
            continue
        if now - minted_at < REUSE_TTL_S:
            continue
        handled += 1
        record = _parent(wt, entry[0]) if entry is not None else None
        if entry is not None and reap_parent(wt, entry[0]):
            reaped.append(entry[0])
            if record is not None:
                just_halted.update(r.run_id for r in record.runs)
        path.unlink(missing_ok=True)
    if limit > handled:
        try:
            abandoned_minted_close.close_abandoned(wt, now, limit - handled, skip=just_halted)
        except OSError:
            pass
    return reaped


def _drop_entries_naming(wt: Path, parent_id: str) -> None:
    try:
        names = [e.name for e in os.scandir(wt / DIGEST_DIR_REL) if e.name.endswith(".json")]
    except OSError:
        return
    for name in names:
        path = wt / DIGEST_DIR_REL / name
        entry = _entry(path)
        if entry is not None and entry[0] == parent_id:
            path.unlink(missing_ok=True)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="minted_parent", description=__doc__.splitlines()[0])
    parser.add_argument("--reap", metavar="PARENT_ID", required=True)
    parser.add_argument("--root", default=".", help="repo root (default: the working directory)")
    args = parser.parse_args(argv)
    wt = main_worktree_root(Path(args.root).resolve())
    record = _parent(wt, args.reap)
    if record is None:
        print(json.dumps({"parent_id": args.reap, "reaped": False, "reason": "no readable parent record"}))
        return 0
    started = _started_run(wt, record, abandoned_counts=True)
    reaped = started is None and reap_parent(wt, args.reap)
    if reaped:
        _drop_entries_naming(wt, args.reap)
    print(json.dumps(
        {"parent_id": args.reap, "reaped": reaped, **({"kept_because_started": started} if started else {})}
    ))
    return 0


if __name__ == "__main__":
    sys.exit(main())

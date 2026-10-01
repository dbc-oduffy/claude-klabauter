"""Drain pending index resyncs: restore from HEAD only where safe and needed.

Each pending record (see ``_index_resync_pending``) is classified from HEAD's tree
(read in-process) and one ``git ls-files -s`` per argv chunk, then acted on:

    needed     HEAD lacks src and has dst; index holds src at the committed blob
               (or, with HEAD lacking both, only src) and dst is absent or equals HEAD
               -> ``git restore --staged`` (the only class that mutates the index)
    healed     HEAD lacks src; index lacks src and index[dst] == HEAD[dst] -> discharge
    stale      HEAD and index both lack src and dst -> discharge
    reversed   HEAD has src and lacks dst -> keep, report once per class change
    contested  anything else, including unmerged entries, non-regular modes and an
               unknown committed blob -> keep, report once per class change

Contested is the default for every state the predicate cannot place: a restore from
HEAD discards staged peer intent at either path.
"""

from __future__ import annotations

import dataclasses
import logging
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from coordinator_core.ops.fleet._index_resync_pending import (
    PendingResync,
    discharge,
    list_pending,
    record_pending,
)
from coordinator_core.win_portability import no_console_creationflags

_LOG = logging.getLogger(__name__)
_LOG.addHandler(logging.NullHandler())

_REGULAR_MODE = 0o100644

NEEDED = "needed"
HEALED = "healed"
STALE = "stale"
REVERSED = "reversed"
CONTESTED = "contested"

_Entry = Tuple[int, str]


async def _run_git_once(argv: List[str], *, cwd: Path, env: dict) -> Tuple[int, str, str]:
    """One spawn, no retry, no sleep; returns ``(returncode, stdout, stderr)``."""
    import asyncio

    proc = await asyncio.create_subprocess_exec(
        *argv,
        cwd=str(cwd),
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        **no_console_creationflags(),
    )
    out, err = await proc.communicate()
    return (
        proc.returncode if proc.returncode is not None else 1,
        out.decode("utf-8", errors="replace"),
        err.decode("utf-8", errors="replace").strip(),
    )


def _parse_ls_files_s(stdout: str) -> Dict[str, List[Tuple[int, str, int]]]:
    """``{path: [(mode, sha, stage), ...]}`` from ``git ls-files -s -z`` output."""
    out: Dict[str, List[Tuple[int, str, int]]] = {}
    for record in stdout.split("\x00"):
        meta, sep, path = record.partition("\t")
        if not sep:
            continue
        try:
            mode, sha, stage = meta.split(" ")
            out.setdefault(path, []).append((int(mode, 8), sha, int(stage)))
        except ValueError:
            continue
    return out


def classify(
    rec: PendingResync,
    head: Dict[str, _Entry],
    index: Dict[str, List[Tuple[int, str, int]]],
) -> str:
    """The drain class of ``rec`` given HEAD entries and index entries for its paths."""
    src_rows = index.get(rec.src, [])
    dst_rows = index.get(rec.dst, [])
    if any(stage != 0 for _m, _s, stage in src_rows + dst_rows):
        return CONTESTED
    i_src: Optional[_Entry] = (src_rows[0][0], src_rows[0][1]) if src_rows else None
    i_dst: Optional[_Entry] = (dst_rows[0][0], dst_rows[0][1]) if dst_rows else None
    h_src = head.get(rec.src)
    h_dst = head.get(rec.dst)

    if h_src is not None and h_dst is None:
        return REVERSED
    if h_src is not None:
        return CONTESTED
    if h_dst is None:
        if i_src is None and i_dst is None:
            return STALE
        if (
            i_dst is None
            and rec.committed_blob
            and i_src == (_REGULAR_MODE, rec.committed_blob)
        ):
            return NEEDED
        return CONTESTED
    if i_src is None:
        return HEALED if i_dst == h_dst else CONTESTED
    if (
        rec.committed_blob
        and i_src == (_REGULAR_MODE, rec.committed_blob)
        and (i_dst is None or i_dst == h_dst)
    ):
        return NEEDED
    return CONTESTED


def _restore_tokens(rec: PendingResync, head: Dict[str, _Entry]) -> Tuple[str, ...]:
    """Pathspecs for the restore; a dst absent from HEAD would make git error out."""
    if rec.dst in head:
        return (rec.src, rec.dst)
    return (rec.src,)


async def drain_pending_resyncs(worktree_root: Path, *, run_git=_run_git_once) -> dict:
    """Classify every pending record and restore the ``needed`` ones from HEAD.

    ``run_git(argv, *, cwd, env) -> (returncode, stdout, stderr)``. Returns
    ``{"restored": [ids], "discharged": [ids], "kept": {id: class}}``. No records
    means no spawn. Never raises on a git fault: affected records are kept.
    """
    import asyncio

    result: dict = {"restored": [], "discharged": [], "kept": {}}
    records = await asyncio.to_thread(list_pending, worktree_root)
    if not records:
        return result

    from coordinator_core.git.git_state import head_blobs
    from coordinator_core.ops.fleet._common import (
        _argv_group_chunks,
        _make_git_env,
        _persist_index_resync_failure,
    )

    all_paths = sorted({p for r in records for p in (r.src, r.dst)})
    head = await asyncio.to_thread(head_blobs, worktree_root, all_paths)
    env = _make_git_env()

    for chunk in _argv_group_chunks([(r, (r.src, r.dst)) for r in records]):
        chunk_recs = [r for r, _tokens in chunk]
        argv = ["git", "ls-files", "-s", "-z", "--"]
        for r in chunk_recs:
            argv.extend((r.src, r.dst))
        rc, stdout, stderr = await run_git(argv, cwd=worktree_root, env=env)
        if rc != 0:
            _LOG.error("index-resync drain: ls-files failed: %s", stderr)
            for r in chunk_recs:
                result["kept"][r.candidate_id] = "unclassified"
            continue
        index = _parse_ls_files_s(stdout)

        needed: List[PendingResync] = []
        for r in chunk_recs:
            cls = classify(r, head, index)
            if cls == NEEDED:
                needed.append(r)
            elif cls in (HEALED, STALE):
                await asyncio.to_thread(discharge, worktree_root, r)
                result["discharged"].append(r.candidate_id)
            else:
                result["kept"][r.candidate_id] = cls
                if cls != r.last_reported_class:
                    await _report_kept(
                        worktree_root, r, cls, _persist_index_resync_failure
                    )

        if not needed:
            continue
        rargv = ["git", "restore", "--staged", "--"]
        for r in needed:
            rargv.extend(_restore_tokens(r, head))
        rc, _out, stderr = await run_git(rargv, cwd=worktree_root, env=env)
        if rc == 0:
            for r in needed:
                await asyncio.to_thread(discharge, worktree_root, r)
                result["restored"].append(r.candidate_id)
        else:
            _LOG.error(
                "index-resync drain: restore failed for %d record(s): %s",
                len(needed), stderr,
            )
            for r in needed:
                result["kept"][r.candidate_id] = "restore-failed"
    return result


async def _report_kept(worktree_root: Path, rec: PendingResync, cls: str, persist) -> None:
    """File the kept record once per class change, then remember the class on the record."""
    import asyncio

    try:
        await asyncio.to_thread(
            persist,
            worktree_root=worktree_root,
            candidate_id=rec.candidate_id,
            reason=f"drain-{cls}: {rec.src} -> {rec.dst}",
            op_label=rec.op_label,
        )
        await asyncio.to_thread(
            record_pending,
            worktree_root,
            [dataclasses.replace(rec, last_reported_class=cls)],
        )
    except Exception as exc:  # noqa: BLE001 - reporting must not fail the drain
        _LOG.error("index-resync drain: report failed for %s: %s", rec.candidate_id, exc)

"""``ceremony.hand_land``: land named paths on HEAD through ``commit_paths``, so a hand-land cannot skip the worktree sync.

Params: ``repo_root`` (string, optional: the repo to land in; defaults to the dispatching worktree's main root),
``paths`` (list of repo-relative path strings, required), ``message`` (string, required; the op appends a
``Hand-Landed: true`` trailer), ``expect_head`` (string, optional: a full or abbreviated HEAD sha; a HEAD that
does not start with it refuses with nothing moved).

The commit, the ref CAS and the 3-way sync of each landed path into the worktree are ``commit_paths``'s; this op
adds no commit-tree or update-ref of its own. A keyed dispatch also gets the durable land record ``land_run``
writes, advanced ``committing`` then ``committed``.

Reply: ``{ok, sha, rebased, sync_refused}``; a non-empty ``sync_refused`` adds ``sync_incomplete: true`` and
``worktree_sync_refused`` (the landing stands, those paths stay byte-intact); ``ownerless`` names landed paths no run
manifest declares (advisory); ``stranded`` names ``[{path, kind, reason, run_id}]``, the recorded sibling paths of a
halted run (``touched-blobs.json``) the landing leaves behind (advisory: the landing stands). Refusal:
``{ok: false, error, refused}``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from coordinator_core.ipc import register_op

OP_NAME = "ceremony.hand_land"
TRAILER = "Hand-Landed: true"


def _refuse(error: str, kind: str) -> dict:
    return {"ok": False, "error": error, "refused": kind}


def _root(params: dict, repo_root: Optional[Path]) -> Optional[Path]:
    given = params.get("repo_root")
    if isinstance(given, str) and given:
        return Path(given)
    if repo_root is None:
        return None
    from coordinator_core.lifecycle import main_worktree_root

    return main_worktree_root(repo_root)


def _stranded_siblings(root: Path, paths: list) -> list:
    """Recorded siblings of a halted run that landing ``paths`` would leave behind; one read of each record in
    ``state/warp`` plus one batched HEAD read over the intersecting records' paths. ``[]`` on any error."""
    try:
        from coordinator_core.authoring_leaks import HeadView
        from coordinator_core.ops.dispatch_emit import stranded_sibling

        from coordinator_core.session import record_homes

        warp = Path(record_homes.home_dir(str(root), "warp"))
        landing = {p.replace("\\", "/").removeprefix("./") for p in paths}
        records = stranded_sibling.read_records(warp)
        involved: set = set()
        for _run, _dir, _mtime, rec in records:
            if landing.intersection(rec):
                involved.update(rec)
        if not involved:
            return []
        head = HeadView(root, involved)
        head_blobs = {p: (head.entry(p) or (0, None))[1] for p in involved}
        return stranded_sibling.stranded(root, warp, landing, head_blobs, records)
    except Exception:  # noqa: BLE001 -- an unreadable record is no claim, never a refusal
        return []


def hand_land(params: dict, repo_root: Optional[Path] = None) -> dict:
    """Land ``params['paths']`` with ``params['message']`` on HEAD; see the module docstring for the reply."""
    from coordinator_core.git import git_state
    from coordinator_core.git.commit import CommitRefused, commit_paths, sync_report
    from coordinator_core.git.ref_cas_retry import REF_CAS_ATTEMPTS, is_ref_moved
    from coordinator_core.ops.dispatch_emit import land_intent

    root = _root(params, repo_root)
    if root is None:
        return _refuse("repo_root is required", "params")
    paths = params.get("paths")
    message = params.get("message")
    if not isinstance(paths, list) or not paths or not all(isinstance(p, str) and p for p in paths):
        return _refuse("paths must be a non-empty list of path strings", "params")
    if not isinstance(message, str) or not message.strip():
        return _refuse("message must be a non-empty string", "params")
    expect = params.get("expect_head")
    if expect is not None and (not isinstance(expect, str) or not expect):
        return _refuse("expect_head must be a non-empty sha string", "params")

    head = git_state.head_sha(root, fresh=True)
    if expect is not None and not (head or "").startswith(expect.lower()):
        return _refuse(f"HEAD is {head}, expected {expect}: nothing moved", "stale_head")

    stranded = [
        {"path": h["path"], "kind": h["kind"], "reason": h["reason"], "run_id": h["run"]}
        for h in _stranded_siblings(root, paths)
    ]

    from coordinator_core.ops.dispatch_emit import stranded_sibling

    ownerless = stranded_sibling.ownerless(root, [p.replace("\\", "/") for p in paths])
    land_intent.record(root, params, kind="hand_land")
    land_intent.mark(land_intent.PHASE_COMMITTING, base=head)
    try:
        for attempt in range(REF_CAS_ATTEMPTS):
            try:
                outcome = commit_paths(root, paths, f"{message.rstrip()}\n\n{TRAILER}\n")
                break
            except CommitRefused as exc:
                if not is_ref_moved(exc) or attempt == REF_CAS_ATTEMPTS - 1:
                    raise
    except (CommitRefused, OSError) as exc:
        land_intent.mark(land_intent.PHASE_CLAIMED)
        return _refuse(f"{type(exc).__name__}: {exc}", "commit")
    land_intent.mark(land_intent.PHASE_COMMITTED, outcome.sha)
    return {
        "ok": True,
        "sha": outcome.sha,
        "rebased": list(outcome.rebased),
        "sync_refused": list(outcome.sync_refused),
        **({"stranded": stranded} if stranded else {}),
        **({"ownerless": ownerless} if ownerless else {}),
        **sync_report(outcome),
    }


@register_op(OP_NAME)
def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    return hand_land(params or {}, repo_root)

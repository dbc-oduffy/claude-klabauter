"""
coordinator_core.ops.research_archive_workdir — JSON-RPC "research.archive_workdir" op.

Purpose: Native replacement for the deep-research pipelines' archive-workdir fence
(``mv docs/research/{run-id}-{topic-slug}-workdir docs/research/archive/...`` at
web-driver.md:261 / structured-driver.md:431 / repo-driver.md:419 — one shared op
covers all three call sites). Moves a completed research run's scratch workdir
into the caller repo's ``docs/research/archive/`` tree, atomically where the
filesystem allows and recoverably where it does not.

Dest naming: ``docs/research/archive/{run_id}-{topic_slug}`` — the workdir's own
name minus its ``-workdir`` suffix. Keeping ``run_id`` in the archived dir name
(where the legacy fence used a wall-clock ``YYYY-MM-DD-`` prefix) is what makes
every rerun state below resolvable from params alone: with ``run_id`` in hand the
op can locate the archived dir even after the source workdir is gone, so the
already-archived no-op needs no source-side evidence and no recorded side state.

Crash/rerun story (Wave-3 settlement A1, AMENDS — implemented exactly as settled,
docs/plans/2026-07-22-wave-3-design-settlements-15-design-bear.md § A1):

  - Same-device: single ``os.rename(src, dest)`` — atomic, done.
  - ``OSError`` EXDEV (incl. the Windows cross-drive equivalent, winerror 17,
    which CPython maps onto ``errno.EXDEV``): ``shutil.copytree(src, tmp)`` with
    ``tmp = dest.parent / f".tmp-archive-{run_id}"``, then ``os.rename(tmp, dest)``
    (same-device with dest by construction, atomic), then ``shutil.rmtree(src)``.
  - Crash window (a) — mid-copy: a stale ``.tmp-archive-{run_id}`` dir is left
    behind. A rerun detects and deletes its OWN stale tmp (keyed by run_id)
    before re-copying.
  - Crash window (b) — after the tmp→dest rename, before ``rmtree(src)``: both
    src and dest exist. Per the settlement's explicit CC-7 exception this state
    IS classified: verify src and dest are content-equal (relative-path walk +
    per-file size, the settled cheap check); equal → complete the pending
    ``rmtree(src)`` and return archived; unequal → structured error naming both
    paths, never clobber.

Rerun state table (exhaustive; anything else is CC-7 fail-loud):
  src absent  + dest present → ``{archived: true, already_archived: true}`` no-op.
  src present + dest present → crash window (b) handling above.
  src present + dest absent  → normal archive.
  src absent  + dest absent  → structured error (nothing to archive and nothing
                               archived — the caller's premise failed; CC-7).

DEC-7 idempotency note: idempotency is achieved by state classification, not by
suppression — the second identical invocation finds src absent + dest present and
returns the documented ``already_archived`` no-op shape without touching disk.
The two crash windows are each re-entrant: (a) self-cleans its run_id-keyed tmp,
(b) completes the one pending step (``rmtree(src)``) after proving content
equality. No invocation ever overwrites an existing archive dir.

``dry_run`` (CC-6): resolves and validates everything (including the both-exist
equality check and every structured-error path), performs ZERO writes — no tmp
cleanup, no mkdir, no rename, no rmtree — and returns the would-be result with
the mutation flag (``archived``) false and ``would_action`` naming the mutation
a real run would perform.

Scope: ``_OP_KEY_SCOPE`` will key this op ``"common_dir"`` (settlement A1
ratifies the manifest's scope verdict) — ``repo_root`` arrives as the git common
dir (``<main-worktree>/.git``); all paths derive from
``main_worktree_root(repo_root)``, never from ``repo_root`` directly, so the op
archives against the CALLER's ``docs/research/archive/``, not claude-klabauter's own clone.
Registration on the other three surfaces (``__init__``/``op_scopes``/
``_registry_map``) lands in the separate CC-3 serial pass, not in this module.

Spec backlinks:
  docs/plans/2026-07-22-wave-3-design-settlements-15-design-bear.md § A1, § CC-1..CC-7
  docs/plans/2026-07-22-coordinator-ops-buildout-from-fence-inventory.md § Mandated resolvers
  state/audits/2026-07-22-command-payload-inventory/op-classification.tsv (archive-research-workdir)

Negative-spec:
  - Does NOT git-commit (the fence's follow-up commit stays with the caller).
  - Does NOT shell out to anything — pure ``os``/``shutil``/``pathlib`` (CC-1;
    no ``mv``, no ``stat -c``/``stat -f`` GNU/BSD dual-branch — the EXDEV
    OSError from ``os.rename`` itself replaces the legacy device-id precheck).
  - Does NOT reproduce the legacy fence's ``YYYY-MM-DD-`` dest prefix — see the
    dest-naming note above; the run_id-keyed name is what makes rerun states
    resolvable, and the actual dest path is returned to the caller.
  - Does NOT guess on the src-present+dest-present-UNEQUAL state or on
    src-absent+dest-absent — both are structured errors naming the paths (CC-7).
  - Does NOT delete a tmp dir belonging to a DIFFERENT run_id — stale-tmp
    self-clean is keyed to this invocation's own ``.tmp-archive-{run_id}``.
"""

from __future__ import annotations

import asyncio
import errno
import os
import shutil
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from coordinator_core.ipc import register_op
from coordinator_core.ops._path_guard import safe_id
from coordinator_core.ops.fleet._common import main_worktree_root
from coordinator_core.session.core import resolve_session_id
from coordinator_core.session.scope import restate_touched_tree

_WORKDIR_SUFFIX = "-workdir"


def _err(error: str, **extra) -> dict:
    """Structured error envelope (exit_code 1) naming the paths involved."""
    out = {"exit_code": 1, "archived": False, "already_archived": False,
           "dest": "", "error": error}
    out.update(extra)
    return out


def _tree_signature(root: Path) -> Dict[str, Tuple[str, int]]:
    """Cheap content signature: relative path → (kind, size) for every entry.

    The settled equality check is "walk + size cheap check" (settlement A1) —
    relative-path sets plus per-file sizes, deliberately NOT a byte-level hash.
    """
    sig: Dict[str, Tuple[str, int]] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        d = Path(dirpath)
        for name in dirnames:
            rel = str((d / name).relative_to(root))
            sig[rel] = ("dir", 0)
        for name in filenames:
            p = d / name
            rel = str(p.relative_to(root))
            try:
                size = p.stat().st_size
            except OSError:
                size = -1
            sig[rel] = ("file", size)
    return sig


def _is_exdev(exc: OSError) -> bool:
    """True for a cross-device/cross-drive rename failure on any platform.

    CPython maps Windows ERROR_NOT_SAME_DEVICE (winerror 17) onto errno.EXDEV;
    the winerror check is belt-and-suspenders for any mapping gap.
    """
    return exc.errno == errno.EXDEV or getattr(exc, "winerror", None) == 17


def _resolve_src(research_dir: Path, run_id: str) -> Tuple[Optional[Path], List[Path]]:
    """Locate the run's workdir under docs/research/ by its run_id prefix.

    Returns (src_or_None, all_matches) — >1 match is the caller's ambiguity to
    resolve (structured error), never a silent pick.
    """
    if not research_dir.is_dir():
        return None, []
    matches = sorted(
        p for p in research_dir.glob(f"{run_id}-*{_WORKDIR_SUFFIX}") if p.is_dir()
    )
    if len(matches) == 1:
        return matches[0], matches
    return None, matches


def _resolve_dest_when_src_absent(archive_dir: Path, run_id: str) -> List[Path]:
    """Locate already-archived dir(s) for this run_id (src-absent rerun states)."""
    if not archive_dir.is_dir():
        return []
    return sorted(p for p in archive_dir.glob(f"{run_id}-*") if p.is_dir()
                  and not p.name.startswith(".tmp-archive-"))


def _archive_workdir_sync(
    worktree: Path, run_id: str, dry_run: bool, session_id: str
) -> dict:
    """Sync body: every blocking filesystem call (os.rename/os.walk via
    ``_tree_signature``/shutil.copytree/shutil.rmtree) lives here, off the
    event loop — the async ``_handler`` below runs this under
    ``asyncio.to_thread`` (AC-3 Gap-3; matches A2/A3/A4's pattern).

    ``session_id`` (resolved once by ``_handler``, possibly ``""`` when
    unresolvable) threads through to the one claim-restatement call in the
    normal-archive branch below — see that call site for why it sits ahead
    of leg selection rather than inside either physical leg.
    """
    research_dir = worktree / "docs" / "research"
    archive_dir = research_dir / "archive"
    tmp = archive_dir / f".tmp-archive-{run_id}"

    src, src_matches = _resolve_src(research_dir, run_id)
    if len(src_matches) > 1:
        return _err(
            f"ambiguous workdir for run_id {run_id!r}: "
            + ", ".join(str(p) for p in src_matches)
        )

    if src is None:
        # src absent — dest present is the settled already-archived no-op;
        # dest absent is CC-7 fail-loud (nothing to archive, nothing archived).
        dest_matches = _resolve_dest_when_src_absent(archive_dir, run_id)
        if len(dest_matches) == 1:
            dest = dest_matches[0]
            if dry_run:
                return {"exit_code": 0, "archived": False, "already_archived": True,
                        "dest": str(dest), "resumed_cleanup": False,
                        "dry_run": True, "would_action": "none"}
            return {"exit_code": 0, "archived": True, "already_archived": True,
                    "dest": str(dest), "resumed_cleanup": False}
        if len(dest_matches) > 1:
            return _err(
                f"ambiguous archived dirs for run_id {run_id!r}: "
                + ", ".join(str(p) for p in dest_matches)
            )
        return _err(
            f"nothing to archive for run_id {run_id!r}: no workdir matching "
            f"{str(research_dir / (run_id + '-*' + _WORKDIR_SUFFIX))!r} and no "
            f"archived dir matching {str(archive_dir / (run_id + '-*'))!r} "
            "(caller premise failed; CC-7 fail-loud)"
        )

    dest = archive_dir / src.name[: -len(_WORKDIR_SUFFIX)]

    if dest.exists():
        # Crash window (b): rename landed, rmtree(src) pending — explicitly
        # classified per settlement A1's CC-7 exception.
        if _tree_signature(src) == _tree_signature(dest):
            if dry_run:
                return {"exit_code": 0, "archived": False, "already_archived": False,
                        "dest": str(dest), "resumed_cleanup": False,
                        "dry_run": True, "would_action": "complete_pending_cleanup"}
            shutil.rmtree(src)
            return {"exit_code": 0, "archived": True, "already_archived": False,
                    "dest": str(dest), "resumed_cleanup": True}
        return _err(
            "both src and dest exist and are NOT content-equal — refusing to "
            f"clobber either: src={str(src)!r} dest={str(dest)!r}",
            src=str(src), dest=str(dest),
        )

    # Normal archive path (src present, dest absent).
    if dry_run:
        return {"exit_code": 0, "archived": False, "already_archived": False,
                "dest": str(dest), "resumed_cleanup": False,
                "dry_run": True, "would_action": "archive"}

    # Claim restatement — ONE call, ahead of leg selection below, per
    # restate_touched_tree's own contract: it is a pure touched.txt string
    # operation independent of which physical leg actually runs (same-device
    # os.rename vs the EXDEV copytree/rename/rmtree fallback), so one call
    # covers both. An unresolvable session_id means no claim could be held
    # in the first place — skip, still do the move.
    if session_id:
        restate_touched_tree(session_id, str(src), str(dest), cwd=str(worktree))

    # Crash window (a) self-clean: a stale tmp from THIS run_id's earlier
    # crashed copy is ours to delete before re-copying.
    if tmp.exists():
        shutil.rmtree(tmp)

    archive_dir.mkdir(parents=True, exist_ok=True)
    try:
        os.rename(src, dest)
    except OSError as exc:
        if not _is_exdev(exc):
            return _err(
                f"rename failed (non-EXDEV): src={str(src)!r} dest={str(dest)!r}: {exc}",
                src=str(src), dest=str(dest),
            )
        # EXDEV fallback: copy to a same-device tmp beside dest, atomic rename
        # into place, then remove the source.
        shutil.copytree(src, tmp)
        os.rename(tmp, dest)
        shutil.rmtree(src)

    return {"exit_code": 0, "archived": True, "already_archived": False,
            "dest": str(dest), "resumed_cleanup": False}


@register_op("research.archive_workdir")
async def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "research.archive_workdir" handler.

    Params:
        repo_root (str)  — optional consistency input: when provided it must
                           resolve to the same worktree the engine-supplied
                           common_dir derives; a mismatch is a structured error
                           (never a silent re-target).
        run_id    (str)  — required; the run identifier prefixing the workdir
                           name (``{run_id}-{topic_slug}-workdir``). Must be a
                           safe single path segment.
        dry_run   (bool) — CC-6 semantics (see module docstring). Default False.

    Returns (exit_code 0):
        archived (bool), dest (str), already_archived (bool), plus
        ``resumed_cleanup`` (True only when crash window (b) was completed) and,
        on dry_run, ``dry_run: true`` + ``would_action``
        ("archive" | "complete_pending_cleanup" | "none").
    Returns (exit_code 1): structured error with ``error`` naming the paths.
    """
    run_id = str(params.get("run_id") or "")
    dry_run = bool(params.get("dry_run", False))
    param_root_raw = params.get("repo_root")

    if not run_id:
        return _err("missing required param: run_id")
    if not safe_id(run_id):
        return _err(f"run_id is not a safe path segment: {run_id!r}")
    if repo_root is None:
        return _err("repo_root (engine common_dir) is required for research.archive_workdir")

    worktree = main_worktree_root(repo_root)
    if param_root_raw:
        try:
            if Path(str(param_root_raw)).resolve() != worktree.resolve():
                return _err(
                    "params.repo_root does not match the resolved worktree: "
                    f"param={param_root_raw!r} resolved_worktree={str(worktree)!r}"
                )
        except OSError as exc:
            return _err(f"params.repo_root failed to resolve: {exc}")

    # Resolved ONCE per call, against the resolved worktree (not the process
    # cwd — this op's caller may run from anywhere): every claim the moved
    # tree could hold belongs to this one invoking session. Empty/unresolved
    # means no claim could be held; _archive_workdir_sync skips restatement
    # in that case and still performs the move.
    session_id = resolve_session_id(str(worktree))

    # Blocking I/O (os.rename/os.walk/shutil.*) — off the event loop (AC-3
    # Gap-3 / async-handler-discipline).
    return await asyncio.to_thread(
        _archive_workdir_sync, worktree, run_id, dry_run, session_id
    )

"""
coordinator_core.ops.research_archive_workdir — JSON-RPC "research.archive_workdir" op.

Purpose: Native replacement for the deep-research pipelines' archive-workdir fence
(``mv docs/research/{run-id}-{topic-slug}-workdir docs/research/archive/...`` at
web-driver.md:261 / structured-driver.md:431 / repo-driver.md:419 — one shared op
covers all three call sites). Moves a completed research run's scratch workdir
into the run dir of the caller repo's research layout, atomically where the
filesystem allows and recoverably where it does not.

Dest naming: ``docs/research/<topic>/<date>-<topic_slug>/workdir`` — the run dir of
``_research_layout.run_dir``. The run dir is resolved from the run's own trail via
``_research_trail.locate_trail(worktree, run_id)`` (its parent dir) so a rerun on a
later day still finds the archived dest; only with no run-dir trail does it fall
back to ``<topic>/<today>-<topic_slug>`` (``topic`` param, else ``topic_for``).
``topic_slug`` is the source workdir's name minus ``{run_id}-`` and ``-workdir``.
A legacy ``docs/research/archive/{run_id}-{topic_slug}`` still reads as already
archived when the source is gone; the op never creates anything under ``archive/``.

Crash/rerun story (Wave-3 settlement A1, AMENDS — implemented exactly as settled,
docs/plans/2026-07-22-wave-3-design-settlements-15-design-bear.md § A1):

  - Same-device: single ``os.rename(src, dest)`` — atomic, done.
  - ``OSError`` EXDEV (incl. the Windows cross-drive equivalent, winerror 17,
    which CPython maps onto ``errno.EXDEV``): ``shutil.copytree(src, tmp)`` with
    ``tmp = <run dir> / f".tmp-archive-{run_id}"``, then ``os.rename(tmp, dest)``
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
archives against the CALLER's ``docs/research/``, not claude-klabauter's own clone.
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
  - Does NOT create ``docs/research/archive/`` or any top-level ``-workdir``
    sibling; the actual dest path is returned to the caller.
  - Does NOT guess on the src-present+dest-present-UNEQUAL state or on
    src-absent+dest-absent — both are structured errors naming the paths (CC-7).
  - Does NOT delete a tmp dir belonging to a DIFFERENT run_id — stale-tmp
    self-clean is keyed to this invocation's own ``.tmp-archive-{run_id}``.
"""

from __future__ import annotations

import asyncio
import datetime
import errno
import os
import shutil
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from coordinator_core.ipc import register_op
from coordinator_core.ops import _research_layout, _research_trail
from coordinator_core.ops._path_guard import safe_id
from coordinator_core.ops.fleet._common import main_worktree_root
from coordinator_core.session.core import resolve_session_id
from coordinator_core.session.scope import restate_touched_tree

_WORKDIR_SUFFIX = "-workdir"


def _err(error: str, **extra) -> dict:
    out = {"exit_code": 1, "archived": False, "already_archived": False,
           "dest": "", "error": error}
    out.update(extra)
    return out


def _tree_signature(root: Path) -> Dict[str, Tuple[str, int]]:
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
    if not research_dir.is_dir():
        return None, []
    matches = sorted(
        p for p in research_dir.glob(f"{run_id}-*{_WORKDIR_SUFFIX}") if p.is_dir()
    )
    if len(matches) == 1:
        return matches[0], matches
    return None, matches


def _resolve_dest_when_src_absent(archive_dir: Path, run_id: str) -> List[Path]:
    if not archive_dir.is_dir():
        return []
    return sorted(p for p in archive_dir.glob(f"{run_id}-*") if p.is_dir()
                  and not p.name.startswith(".tmp-archive-"))


def _trail_run_dir(worktree: Path, run_id: str) -> Optional[Path]:
    """Run dir holding ``run_id``'s run-dir-layout trail, or None (no trail / legacy trail)."""
    rel = _research_trail.locate_trail(worktree, run_id)
    if rel is None or rel.startswith(_research_trail.LEGACY_TRAIL_DIR.as_posix() + "/"):
        return None
    return (worktree / rel).parent


def _archive_workdir_sync(
    worktree: Path, run_id: str, dry_run: bool, session_id: str,
    topic: Optional[str] = None,
) -> dict:
    research_dir = worktree / "docs" / "research"
    archive_dir = research_dir / "archive"  # legacy read-only location

    trail_dir = _trail_run_dir(worktree, run_id)

    src, src_matches = _resolve_src(research_dir, run_id)
    if len(src_matches) > 1:
        return _err(
            f"ambiguous workdir for run_id {run_id!r}: "
            + ", ".join(str(p) for p in src_matches)
        )

    if src is None:
        if trail_dir is not None and (trail_dir / "workdir").is_dir():
            dest = trail_dir / "workdir"
            if dry_run:
                return {"exit_code": 0, "archived": False, "already_archived": True,
                        "dest": str(dest), "resumed_cleanup": False,
                        "dry_run": True, "would_action": "none"}
            return {"exit_code": 0, "archived": True, "already_archived": True,
                    "dest": str(dest), "resumed_cleanup": False}
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
            f"archived dir matching {str(archive_dir / (run_id + '-*'))!r} and no "
            "run-dir workdir (caller premise failed; CC-7 fail-loud)"
        )

    if trail_dir is not None:
        run_dir = trail_dir
    else:
        topic_slug = src.name[len(run_id) + 1: -len(_WORKDIR_SUFFIX)]
        try:
            rel = _research_layout.run_dir(
                _research_layout.topic_for(topic_slug, topic),
                datetime.date.today().isoformat(),
                topic_slug,
            )
        except ValueError as exc:
            return _err(f"cannot derive a run dir for {src.name!r}: {exc}")
        run_dir = research_dir / rel
    dest = run_dir / "workdir"
    tmp = run_dir / f".tmp-archive-{run_id}"

    if dest.exists():
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
    #
    # Worktree-relative, NOT absolute: restate_touched_tree routes both
    # src_rel/dst_rel through normalize_touch_path, whose absolute-path arm
    # unconditionally returns None for a directory-shaped input (pinned by
    # test_clause_not_a_directory_pinned /
    # test_directory_shaped_absolute_input_does_not_record_sibling_file in
    # coordinator_core/session/tests/test_scope.py — normalize_touch_path
    # does not support directories at all, by design). src/dest here name
    # the workdir DIRECTORY being relocated, so passing them absolute made
    # restate_touched_tree's own directory-tree call silently no-op (src_norm
    # resolved to None) every time src still existed on disk at call time —
    # i.e. every normal-archive invocation. A relative path never enters the
    # absolute arm at all (see normalize_touch_path's relative-arm dialect
    # fold), which is the calling convention every other caller of
    # restate_touched_tree/relocate_touched_path already uses.
    if session_id:
        restate_touched_tree(
            session_id,
            str(src.relative_to(worktree)),
            str(dest.relative_to(worktree)),
            cwd=str(worktree),
        )

    if tmp.exists():
        shutil.rmtree(tmp)

    run_dir.mkdir(parents=True, exist_ok=True)
    try:
        os.rename(src, dest)
    except OSError as exc:
        if not _is_exdev(exc):
            return _err(
                f"rename failed (non-EXDEV): src={str(src)!r} dest={str(dest)!r}: {exc}",
                src=str(src), dest=str(dest),
            )
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
        topic     (str)  — optional research topic dir; absent, derived from the
                           workdir's topic slug (``_research_layout.topic_for``).
                           Ignored when the run's trail already names the run dir.
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
    topic = params.get("topic")
    if topic is not None and (not isinstance(topic, str) or not _research_layout.TOPIC_RE.match(topic)):
        return _err(f"topic is not a valid research topic: {topic!r}")

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

    session_id = resolve_session_id(str(worktree))

    return await asyncio.to_thread(
        _archive_workdir_sync, worktree, run_id, dry_run, session_id, topic
    )

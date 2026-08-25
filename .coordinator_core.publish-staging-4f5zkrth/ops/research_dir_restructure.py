"""
coordinator_core.ops.research_dir_restructure — JSON-RPC
"research.restructure_for_repeat_topic" op.

Purpose: Native replacement for the relay-protocol repeat-topic restructure fence
(coordinator/pipelines/relay-protocol.md:147 — ``mkdir -p docs/research/{topic}``
then two ``mv``s), invoked when the PM indicates a research topic will be
revisited: the dated result markdown and the archived paper-trail dir are moved
into a persistent per-topic structure ``docs/research/{topic_slug}/``:

    docs/research/{topic_slug}/{YYYY-MM-DD}-result.md
    docs/research/{topic_slug}/{YYYY-MM-DD}-paper-trail/

Crash/rerun story (Wave-3 settlement A5, AMENDS — implemented exactly as settled,
docs/plans/2026-07-22-wave-3-design-settlements-15-design-bear.md § A5): the two
renames are non-transactional by construction; recoverability comes from the
resumable per-step design, NOT from pretending atomicity. Each step ("dated
result" → topic dir; "paper trail" dir → topic dir) is an independently checked
step over ``mkdir(topic_dir, exist_ok=True)``, classified by the settled
four-state table BEFORE any mutation:

  src present + dest absent  → ``os.rename``; record the step in steps_applied.
  src absent  + dest present → step already done; skip as a no-op.
  src present + dest present → structured error naming BOTH paths (a rerun must
                               not guess which copy is canonical; CC-7).
  src absent  + dest absent  → structured error (substrate missing — the
                               caller's premise failed; CC-7).

A crash between the two renames leaves step 1 done, step 2 pending; the rerun
skips 1 and completes 2 — "completes whatever step is still pending". A full
rerun after success finds both steps already done and returns
``{restructured: false, steps_applied: []}`` with ``new_dir`` populated.

DEC-7 idempotency note: idempotency is achieved by the resumable step-order
check — each step's post-state (src absent + dest present) is exactly the state
the four-state table classifies as skip, so the second identical invocation
no-ops per step by construction, and a crash-interrupted state is COMPLETED on
rerun rather than merely detected. All four states of both steps are classified
before the first write, so an error state never leaves a partial mutation from
THIS invocation behind.

Scope: ``_OP_KEY_SCOPE`` will key this op ``"common_dir"`` (settlement A5
ratifies the manifest's scope verdict) — ``docs/research/`` is a top-level doc
dir shared across linked worktrees; ``repo_root`` arrives as the git common dir
and every path derives from ``main_worktree_root(repo_root)``, so a cross-repo
invocation restructures the CALLER's docs/research/, never makima's own.
Registration on the other three surfaces (``__init__``/``op_scopes``/
``_registry_map``) lands in the separate CC-3 serial pass, not in this module.

Spec backlinks:
  docs/plans/2026-07-22-wave-3-design-settlements-15-design-bear.md § A5, § CC-1..CC-7
  docs/plans/2026-07-22-coordinator-ops-buildout-from-fence-inventory.md § Mandated resolvers
  state/audits/2026-07-22-command-payload-inventory/op-classification.tsv
  (restructure-research-dir-for-repeat-topic)

Negative-spec:
  - Does NOT shell out to anything — pure ``os``/``pathlib`` renames (CC-1; the
    legacy ``mv``s and ``mkdir -p`` are replaced natively, Windows-first).
  - Does NOT carry ``dry_run`` — the manifest contract for this op has no
    dry_run key, so CC-6 does not apply here.
  - Does NOT accept caller paths outside the worktree's ``docs/research/`` tree
    — both supplied paths are containment-checked against that root; an escape
    is a structured error, not a resolved write.
  - Does NOT guess on either CC-7 error state (both-present, both-absent) and
    never overwrites an existing destination.
  - Does NOT git-commit (the fence's surrounding commit stays with the caller).

Claim restatement: each step's touch-claim(s), if any, are re-declared onto
the post-move path immediately before that step's ``os.rename`` — a directory
tree (``paper_trail``) via ``coordinator_core.session.scope.
restate_touched_tree``, a single file (``dated_result``) via that module's
sibling ``coordinator_core.session.scope.restate_touched_path``. See
``_restructure_sync``'s docstring for why this does not disturb the
crash/rerun step protocol above.
"""

from __future__ import annotations

import asyncio
import os
import re
from pathlib import Path
from typing import List, Optional, Tuple

from coordinator_core.ipc import register_op
from coordinator_core.ops._path_guard import contained_path, safe_id
from coordinator_core.ops.fleet._common import main_worktree_root
from coordinator_core.session.core import resolve_session_id
from coordinator_core.session.scope import restate_touched_path, restate_touched_tree

_DATE_PREFIX = re.compile(r"^(\d{4}-\d{2}-\d{2})")

#: (step_name, params_key) — the settled two-step order.
_STEP_DATED_RESULT = "dated_result"
_STEP_PAPER_TRAIL = "paper_trail"


def _err(error: str, **extra) -> dict:
    """Structured error envelope (exit_code 1) naming the paths involved."""
    out = {"exit_code": 1, "restructured": False, "new_dir": "",
           "steps_applied": [], "error": error}
    out.update(extra)
    return out


def _date_prefix_of(name: str) -> Optional[str]:
    m = _DATE_PREFIX.match(name)
    return m.group(1) if m else None


def _classify_step(src: Path, dest: Path) -> str:
    """Return one of 'pending' | 'done' | 'both_present' | 'both_absent'.

    The settled four-state table (settlement A5) — every step is classified
    through this single function so no rename can bypass the table.
    """
    src_present = src.exists()
    dest_present = dest.exists()
    if src_present and not dest_present:
        return "pending"
    if not src_present and dest_present:
        return "done"
    if src_present and dest_present:
        return "both_present"
    return "both_absent"


def _resolve_step_src(
    raw: str, worktree: Path, research_dir: Path
) -> Tuple[Optional[Path], Optional[str]]:
    """Resolve a caller-supplied step-source path, contained to docs/research/.

    Relative paths resolve against the worktree root. Returns (path, None) or
    (None, error_reason). A path that has already been moved (src absent) must
    still be expressible, so containment is checked on the resolved-lexical
    parent chain via ``contained_path`` against the research root — an absent
    path under the root is fine; an escape is not.
    """
    if not raw:
        return None, "empty path"
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = worktree / candidate
    # contained_path resolves symlinks and fails closed; an absent-but-contained
    # path still resolves lexically under the root on all supported platforms
    # (Python >= 3.6 resolve(strict=False)).
    resolved = contained_path(candidate, [research_dir])
    if resolved is None:
        return None, (
            f"path escapes the worktree docs/research/ tree: {raw!r} "
            f"(root: {str(research_dir)!r})"
        )
    return resolved, None


def _restructure_sync(
    worktree: Path,
    research_dir: Path,
    topic_dir: Path,
    dated_result_raw: str,
    paper_trail_raw: str,
    session_id: str,
) -> dict:
    """Sync body: path resolution + the two ``os.rename`` calls (and the
    ``topic_dir.mkdir``) live here, off the event loop — the async
    ``_handler`` below runs this under ``asyncio.to_thread`` (AC-3 Gap-3;
    matches A2/A3/A4's pattern).

    Claim restatement (state/bug-backlog stranded-touch-claim shape, this
    op widened to close it): for each step about to be renamed, THIS
    session's claim (if any) on the step's descendants/self is re-declared
    onto the post-move path BEFORE that step's ``os.rename`` runs --
    :func:`restate_touched_tree` for ``paper_trail`` (a directory tree),
    :func:`coordinator_core.session.scope.restate_touched_path` for
    ``dated_result`` (a single file). Both are pure ``touched.txt``
    read+append operations with no
    filesystem side effect of their own, so calling them ahead of the
    rename does not disturb the crash/rerun step protocol below: a crash
    between a restatement call and its step's ``os.rename`` leaves an
    extra, harmless standing claim on a path the step hasn't moved to yet
    (the rerun still finds that step "pending" and completes it
    normally); a crash AFTER the rename but before the NEXT step's
    restatement leaves that next step's claim, if any, to be re-declared
    on the rerun exactly as it would be on a fresh call. ``session_id``
    empty/unresolvable degrades every restatement call to a no-op -- the
    renames themselves are unconditional either way.
    """
    result_src, reason = _resolve_step_src(dated_result_raw, worktree, research_dir)
    if result_src is None:
        return _err(f"dated_result_path: {reason}")
    trail_src, reason = _resolve_step_src(paper_trail_raw, worktree, research_dir)
    if trail_src is None:
        return _err(f"archived_paper_trail_dir: {reason}")

    result_date = _date_prefix_of(result_src.name)
    if result_date is None:
        return _err(
            "dated_result_path basename does not start with a YYYY-MM-DD date "
            f"prefix: {str(result_src)!r} (caller premise failed; CC-7)"
        )
    trail_date = _date_prefix_of(trail_src.name)
    if trail_date is None:
        return _err(
            "archived_paper_trail_dir basename does not start with a YYYY-MM-DD "
            f"date prefix: {str(trail_src)!r} (caller premise failed; CC-7)"
        )

    result_dest = topic_dir / f"{result_date}-result.md"
    trail_dest = topic_dir / f"{trail_date}-paper-trail"

    steps = [
        (_STEP_DATED_RESULT, result_src, result_dest),
        (_STEP_PAPER_TRAIL, trail_src, trail_dest),
    ]

    # Classify ALL steps before mutating ANYTHING — an error state on either
    # step aborts the whole invocation with zero writes from this call.
    states = {}
    for step_name, src, dest in steps:
        state = _classify_step(src, dest)
        if state == "both_present":
            return _err(
                f"step {step_name!r}: both src and dest exist — a rerun must "
                f"not guess which is canonical: src={str(src)!r} "
                f"dest={str(dest)!r}",
                src=str(src), dest=str(dest), step=step_name,
            )
        if state == "both_absent":
            return _err(
                f"step {step_name!r}: neither src nor dest exists — substrate "
                f"missing, caller premise failed: src={str(src)!r} "
                f"dest={str(dest)!r}",
                src=str(src), dest=str(dest), step=step_name,
            )
        states[step_name] = state

    cwd = str(worktree)
    steps_applied: List[str] = []
    if any(state == "pending" for state in states.values()):
        topic_dir.mkdir(parents=True, exist_ok=True)
        for step_name, src, dest in steps:
            if states[step_name] != "pending":
                continue
            if session_id:
                if step_name == _STEP_PAPER_TRAIL:
                    restate_touched_tree(session_id, str(src), str(dest), cwd=cwd)
                else:
                    restate_touched_path(session_id, str(src), str(dest), cwd=cwd)
            os.rename(src, dest)
            steps_applied.append(step_name)

    return {
        "exit_code": 0,
        "restructured": bool(steps_applied),
        "new_dir": str(topic_dir),
        "steps_applied": steps_applied,
    }


@register_op("research.restructure_for_repeat_topic")
async def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "research.restructure_for_repeat_topic" handler.

    Params:
        topic_slug              (str) — required; the persistent per-topic dir
                                        name under docs/research/. Must be a
                                        safe single path segment.
        dated_result_path       (str) — required; the dated result markdown
                                        (``YYYY-MM-DD-{topic}.md``), absolute
                                        or worktree-relative.
        archived_paper_trail_dir (str) — required; the archived paper-trail dir
                                        (``docs/research/archive/YYYY-MM-DD-…``),
                                        absolute or worktree-relative.

    Returns (exit_code 0):
        restructured  (bool)      — True iff at least one rename was applied.
        new_dir       (str)       — the topic dir path (always populated).
        steps_applied (list[str]) — subset of ["dated_result", "paper_trail"]
                                    actually renamed THIS invocation.
    Returns (exit_code 1): structured error with ``error`` naming the paths.
    """
    topic_slug = str(params.get("topic_slug") or "")
    dated_result_raw = str(params.get("dated_result_path") or "")
    paper_trail_raw = str(params.get("archived_paper_trail_dir") or "")

    if not topic_slug:
        return _err("missing required param: topic_slug")
    if not safe_id(topic_slug):
        return _err(f"topic_slug is not a safe path segment: {topic_slug!r}")
    if not dated_result_raw:
        return _err("missing required param: dated_result_path")
    if not paper_trail_raw:
        return _err("missing required param: archived_paper_trail_dir")
    if repo_root is None:
        return _err(
            "repo_root (engine common_dir) is required for "
            "research.restructure_for_repeat_topic"
        )

    worktree = main_worktree_root(repo_root)
    research_dir = worktree / "docs" / "research"
    topic_dir = research_dir / topic_slug

    # Resolved ONCE per call, matching coordinator_core.ops.
    # migrate_cross_repo_layout.main's convention: both renames below
    # belong to the same invoking session, and an unresolvable ("") id
    # degrades every claim-restatement call to a no-op while the renames
    # themselves still proceed unconditionally.
    session_id = resolve_session_id(str(worktree))

    # Blocking I/O (os.rename, plus stat-heavy path resolution) — off the
    # event loop (AC-3 Gap-3 / async-handler-discipline).
    return await asyncio.to_thread(
        _restructure_sync,
        worktree,
        research_dir,
        topic_dir,
        dated_result_raw,
        paper_trail_raw,
        session_id,
    )

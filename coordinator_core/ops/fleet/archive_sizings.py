"""
coordinator_core.ops.fleet.archive_sizings — fleet.archive_terminal_sizings op.

Purpose: git-mv terminal sizing-objects (status ∈ SIZING_TERMINAL_STATUS) from
state/sizings/*.yaml into archive/sizings/YYYY-MM/ under the confirm→act
(dry_run:true / dry_run:false) wire contract. Registers as
"fleet.archive_terminal_sizings" via @register_op.

Modeled on coordinator_core.ops.fleet.archive_plans for the MOVE shape
(same _handle_preview / _handle_act split, same _derive_yyyy_mm-from-filename
approach for the archive destination) — see that module's docstring for the
mechanics this one intentionally mirrors. Two things are deliberately NOT
mirrored:

1. The collision predicate. archive_plans carries only the source-gone
   idempotent-replay form; this family additionally distinguishes a
   byte-identical destination (converge via force-move) from a genuinely
   differing one (skip, never clobber) using the shared
   coordinator_core.ops.fleet._common._is_identical_duplicate /
   _REASON_DEST_CONFLICT predicate — see archive_handoffs.py for the
   worked pattern this module's dest-collision block is copied from.
2. Terminality itself is status-only, never inferred. See "Never-infer
   boundary" below.

Never-infer boundary (DR-293; AC3):
This module NEVER writes or infers a `status:` value. It reads the
frontmatter `status:` field of each sizing-object and, when it names a
terminal value, moves the file — nothing more. A non-terminal, malformed, or
unparseable record is left untouched on disk — never flipped, never
silently dropped from consideration in the sense of being reinterpreted as
terminal. At T3 act-time, a record that drifts non-terminal between preview
and act is surfaced in the result envelope with a named skip reason
(`terminality-drift:...`). At T1 preview, a non-terminal/malformed record is
simply not enumerated as a candidate — `build_dry_run_result` carries no
skip list at T1 (`skipped: []` is hardcoded empty there); the record's
never-flipped guarantee holds, but "surfaced with a reason" is a T3-only
property, not a T1 one in the WIRE envelope. `_handle_preview`'s own
`scan_skipped` out-param (see `_archive_terminal_sizings`'s docstring) lets
an in-process caller still recover the T1 refusal reasons without widening
that frozen envelope. `cascade_backstop_sweep` and this family's own
governing boundary are explicit on this point: a sweep that decides for
itself that work is "finished" is exactly the failure DR-293 forbids. Every
code path in this module is read-then-move or refuse-to-move; none is
read-then-write.

Forward-pointer refusal gate (AC6):
A sizing record MAY carry a `plan:` FK naming the plan it was consumed into.
When present, this module reads that plan's OWN `status:` and requires it to
be plan-terminal (coordinator_core.lifecycle_constants.PLAN_TERMINAL_STATUS)
before the sizing may move — even though the sizing's own status is already
terminal. This is a REFUSAL, not an inference: the family declines to act
and concludes nothing about the plan's true state. A null/absent `plan:` FK
(the common case — every dispatch-routed sizing) carries no such constraint.
Absent-target and unreadable-target both refuse-in-place with a named
reason, never treated as "no constraint" — a dangling FK is exactly the
ambiguous case this gate exists to fail closed on.

Terminality predicate: frontmatter status ∈ SIZING_TERMINAL_STATUS
(coordinator_core.lifecycle_constants — the family's ONLY terminality
source; no literal status list appears in this module).

Archive destination: archive/sizings/YYYY-MM/ (YYYY-MM from the sizing
record's OWN FILENAME prefix, e.g. 2026-08-13-foo.yaml → 2026-08 — never
from today's date; mirrors archive_plans._derive_yyyy_mm).

Worktree-dirty retention gate (AC5): at T3 act, `_dirty_sizing_relpaths`
retains (never moves) any surviving candidate carrying uncommitted worktree
or index changes — one batched `git status --porcelain` call scoped to the
CLASSIFICATION-time survivor set (source-still-present, status-still-
terminal — see `_handle_act`'s own pass-1/pass-2 split), mirroring
archive_terminal_handoffs.py's own scan-time Rail 1
(`_dirty_handoff_relpaths`), added here 2026-09-03 (this module previously
had no dirty check at all). It runs BEFORE the AC6 forward-pointer refusal,
the cannot-derive-date guard, dest-collision handling, and any `Move`
construction — not merely before the eventual commit — which is what makes
its "earlier, larger window" claim (below) actually true of where it sits,
rather than true only in wall-clock terms of a check still positioned right
beside the act. Deliberately NOT the drift check `_common.archive_and_commit`
used to carry internally and retired 2026-08-26 by PM ruling — that removal
covers a different, later, narrower window (between a stat-based check
immediately before the commit and the commit a moment after) which the PM
ruling accepts as safe for an already-terminal, already-claim-checked record
a sweep that refuses live-claimed records selected; this gate, sitting at
classification time, covers the materially larger window between whichever
peer session last touched the file and this sweep noticing it during
classification. T1 preview does not apply this gate — it stays status-only,
per this module's own preview/act split.

Spec backlinks:
  - Plan (C1): docs/plans/2026-08-13-terminal-sizings-boot-sweep-family.md
  - DR-293:    the ruling naming this family's shape and never-infer boundary
  - Sibling collision fix: docs/plans/2026-08-13-fleet-archive-dest-collision-vs-idempotent-replay.md
    (moved _is_identical_duplicate / _REASON_DEST_CONFLICT into _common.py;
    this family consumes that shared export rather than re-implementing it)

Negative-spec:
  - Does NOT write, flip, upgrade, or infer a `status:` on any record —
    sizing or plan. Read-then-move or refuse-to-move only.
  - Does NOT copy archive_plans.py's dest-collision predicate (bare
    `dst.exists()` → "already-archived") — that shape was removed from
    three other fleet families as a defect (widened "already-archived"
    past its AC12-pinned source-gone meaning). See _common._is_identical_duplicate.
  - Does NOT clobber a destination whose bytes differ from the source —
    a differing dst is real archived history.
  - Does NOT use git add -A or git add . — scoped exact-pathspec only via
    archive_and_commit (DR-211 D3 Invariant 4, inherited unchanged).
  - Does NOT reconcile pre-existing archive residue — this module adds the
    mechanism only; sweeping already-stranded records is a separate corpus
    remediation.
"""

from __future__ import annotations

import logging
import re
import os
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from coordinator_core.dag import _read_meta
from coordinator_core.ipc import register_op
from coordinator_core.lifecycle_constants import (
    HANDOFF_TERMINAL_DEPLOYMENT,
    PLAN_TERMINAL_STATUS,
    SIZING_TERMINAL_STATUS,
)
from coordinator_core.ops.ceremony.git_native import (
    REASON_WORKTREE_DIRTY,
    dirty_relpaths_from_porcelain,
)
from coordinator_core.ops.fleet._common import (
    Move,
    _REASON_DEST_CONFLICT,
    _is_identical_duplicate,
    archive_and_commit,
    build_act_result,
    build_dry_run_result,
    build_setup_error_result,
    check_repo_root,
    main_worktree_root,
    parse_frontmatter_field,
    parse_frontmatter_status,
    rel_id,
    validate_params,
)

_LOG = logging.getLogger(__name__)
_LOG.addHandler(logging.NullHandler())

_TERMINAL_STATUSES: frozenset = SIZING_TERMINAL_STATUS

_DATE_PREFIX_RE = re.compile(r"^(\d{4}-\d{2})-\d{2}-")

_REASON_FORWARD_PLAN_NOT_TERMINAL = "forward-plan-not-terminal"

# in `coordinator_core.ops.ceremony.git_native` (`REASON_WORKTREE_DIRTY`),
# `_SCAN_REASON_WORKTREE_DIRTY` is the same alias) rather than duplicated —
_REASON_WORKTREE_DIRTY = REASON_WORKTREE_DIRTY


def _derive_yyyy_mm(fname: str) -> Optional[str]:
    """Derive YYYY-MM from a sizing-object filename prefix.

    E.g. "2026-08-13-foo.yaml" → "2026-08".
    Returns None when the filename carries no YYYY-MM-DD prefix (ungated skip).
    """
    m = _DATE_PREFIX_RE.match(fname)
    return m.group(1) if m else None


def _extract_title(path: Path) -> Optional[str]:
    return parse_frontmatter_field(path, "title")


def _read_plan_fk(path: Path) -> Optional[str]:
    plan_fk = parse_frontmatter_field(path, "plan")
    if plan_fk in (None, "", "null", "~"):
        return None
    return plan_fk


_ARCHIVE_PLANS_SUBDIRS = (("archive", "specs"), ("archive", "plans"))


def _resolve_plan_fk(worktree_root: Path, plan_fk: str) -> Optional[Path]:
    """Resolve a sizing's `plan:` FK live-then-archive, or None.

    The FK is spelled `docs/plans/<id>.md` in every sizing that carries one,
    but a shipped plan is moved to `archive/specs/<month>/<id>.md` and no
    mechanism rewrites the citation -- the FK is archive-agnostic by design,
    exactly as `coordinator_core.ops._sizing_citation` already documents for
    the sizings direction of the same pointer. Resolving it literally makes
    every shipped plan read as DANGLING, which the refusal gate below then
    treats as "refuse in place": measured on this corpus, 37 of 41 otherwise-
    movable terminal sizings were held by plans that had simply been archived.

    Mirrors `_sizing_citation._archive_sizings_fallback`'s discipline: live
    always wins, the archive probe fires only on a miss, and an ambiguous
    multi-match is treated exactly like no match rather than guessed at.
    """
    direct = worktree_root / plan_fk
    if direct.is_file():
        return direct
    basename = os.path.basename(plan_fk.replace("\\", "/"))
    if not basename:
        return None
    bare = worktree_root / "docs" / "plans" / basename
    if bare.is_file():
        return bare
    for subdir in _ARCHIVE_PLANS_SUBDIRS:
        archive_root = worktree_root.joinpath(*subdir)
        if not archive_root.is_dir():
            continue
        matches = sorted(p for p in archive_root.rglob(basename) if p.is_file())
        if len(matches) == 1:
            return matches[0]
    return None


def _forward_plan_refusal_reason(
    worktree_root: Path, plan_fk: str
) -> Optional[str]:
    plan_path = _resolve_plan_fk(worktree_root, plan_fk)
    if plan_path is None:
        return (
            f"{_REASON_FORWARD_PLAN_NOT_TERMINAL}: plan FK {plan_fk!r} "
            f"could not be resolved to a file, live or archived"
        )

    plan_status = parse_frontmatter_status(plan_path)
    if plan_status not in PLAN_TERMINAL_STATUS:
        return (
            f"{_REASON_FORWARD_PLAN_NOT_TERMINAL}: plan {plan_fk!r} status is "
            f"{plan_status!r}, not terminal"
        )
    return None


#: A sizing whose `route:` is this value is initiative-scale by itself.
_ROUTE_ROADMAP = "roadmap"

#: A sizing whose `route:` is `pm-decision` and whose `xl_exit:` is this
#: value has been resolved to the same initiative-scale outcome as
#: `route: roadmap` directly — see plan_gate.py's own `_XL_EXIT_RESOLVING_TO_PLAN`
#: sibling constant for the parallel "effective route" reasoning on the other
#: `xl_exit` value.
_ROUTE_PM_DECISION = "pm-decision"
_XL_EXIT_ROADMAP = "roadmap"

_REASON_ROADMAP_STUBS_NOT_TERMINAL = "roadmap-stubs-not-terminal"

#: Corpus roots this module scans for a reverse `sizing_object:` citation.
#: Mirrors handoff.schema.json / plan.schema.json's shared FK shape — see
#: `_citer_is_terminal` for why handoffs and plans are tested differently.
_CITER_SEARCH_SUBDIRS = (
    ("docs", "plans"),
    ("state", "handoffs"),
    ("archive", "specs"),
    ("archive", "handoffs"),
)


def _is_roadmap_exit(route: Optional[str], xl_exit: Optional[str]) -> bool:
    """True when a sizing's route resolves to the initiative-scale roadmap exit.

    Two shapes name the same outcome (mirrors plan_gate.py's own
    `_sizing_route` "effective, not literal" reasoning): `route: roadmap`
    directly, or `route: pm-decision` with `xl_exit: roadmap` once the PM has
    recorded that choice.
    """
    if route == _ROUTE_ROADMAP:
        return True
    return route == _ROUTE_PM_DECISION and xl_exit == _XL_EXIT_ROADMAP


def _citer_is_terminal(path: Path) -> bool:
    """True when a file that cites a sizing via `sizing_object:` is itself terminal.

    A handoff (state/handoffs/, archive/handoffs/) is terminal by its own
    `deployment_state`, per HANDOFF_TERMINAL_DEPLOYMENT — the same axis
    archive_terminal_handoffs.py reads. A plan (docs/plans/, archive/specs/)
    is terminal by `status`, per PLAN_TERMINAL_STATUS. Path membership under
    a `handoffs` directory decides which axis applies.
    """
    if "handoffs" in path.parts:
        deployment_state = (parse_frontmatter_field(path, "deployment_state") or "").strip().lower()
        return deployment_state in HANDOFF_TERMINAL_DEPLOYMENT
    status = parse_frontmatter_status(path)
    return status in PLAN_TERMINAL_STATUS


def _build_citer_index(worktree_root: Path) -> Dict[str, List[Path]]:
    """One walk of `_CITER_SEARCH_SUBDIRS`, grouped by cited basename.

    Hoisted out of `_find_sizing_citers` so a batch of many candidates (as
    `_handle_preview`/`_handle_act` process, one candidate at a time) shares
    a single corpus walk instead of re-`rglob`-ing the same subtrees once
    per candidate being previewed/acted on.
    """
    index: Dict[str, List[Path]] = {}
    for subdir in _CITER_SEARCH_SUBDIRS:
        root = worktree_root.joinpath(*subdir)
        if not root.is_dir():
            continue
        for candidate in sorted(root.rglob("*.md")):
            if not candidate.is_file():
                continue
            cited = parse_frontmatter_field(candidate, "sizing_object")
            if not cited:
                continue
            basename = os.path.basename(cited.replace("\\", "/"))
            index.setdefault(basename, []).append(candidate)
    return index


def _find_sizing_citers(
    worktree_root: Path,
    sizing_relpath: str,
    citer_index: Optional[Dict[str, List[Path]]] = None,
) -> List[Path]:
    """Every stub/plan under `_CITER_SEARCH_SUBDIRS` whose `sizing_object:`
    names this sizing, matched by basename (the FK is archive-agnostic by
    design, per `_sizing_citation`'s docstring — a citer written before this
    sizing ever moves still spells it as the live `state/sizings/...` path,
    so basename is sufficient and avoids re-deriving that fallback here).

    ``citer_index`` (from `_build_citer_index`) lets a caller processing many
    candidates share one walk; omitted, this builds its own (single-candidate
    callers, tests).
    """
    basename = os.path.basename(sizing_relpath.replace("\\", "/"))
    index = citer_index if citer_index is not None else _build_citer_index(worktree_root)
    return index.get(basename, [])


def _roadmap_stubs_refusal_reason(
    worktree_root: Path,
    sizing_path: Path,
    sizing_relpath: str,
    citer_index: Optional[Dict[str, List[Path]]] = None,
) -> Optional[str]:
    """Refuse a roadmap-exit sizing until every stub/plan citing it is terminal.

    Terminality on the FIRST citer to ship is not terminality of the
    initiative the roadmap exit opened — the sizing stays live until every
    citer this module can find is itself terminal (or none exist yet).
    """
    route = parse_frontmatter_field(sizing_path, "route")
    xl_exit = parse_frontmatter_field(sizing_path, "xl_exit")
    if not _is_roadmap_exit(route, xl_exit):
        return None

    citers = _find_sizing_citers(worktree_root, sizing_relpath, citer_index=citer_index)
    non_terminal = [rel_id(c, worktree_root) for c in citers if not _citer_is_terminal(c)]
    if non_terminal:
        return (
            f"{_REASON_ROADMAP_STUBS_NOT_TERMINAL}: "
            f"{', '.join(sorted(non_terminal))}"
        )
    return None


def _dirty_sizing_relpaths(worktree_root: Path, candidate_relpaths: List[str]) -> Set[str]:
    """Return the subset of `candidate_relpaths` carrying uncommitted changes.

    ONE scoped `git status --porcelain -- <candidate_relpaths>` call (AC5) —
    applied at CLASSIFICATION time, over the batch already narrowed to the
    survivors of the act-time terminality re-verify (source-still-present,
    status-still-terminal) and nothing else: this runs BEFORE the AC6
    forward-pointer refusal, the cannot-derive-date guard, dest-collision
    handling, or any `Move` is built — mirroring exactly where
    archive_terminal_handoffs.py's own Rail 1 (`_dirty_handoff_relpaths`)
    sits relative to that module's classification pass, immediately after
    Branch A/B qualification and before any candidate-specific refinement.
    This is NOT archive_and_commit's own internal drift check, which was
    retired outright by PM ruling 2026-08-26 (see
    coordinator_core.ops.fleet._common.archive_and_commit's docstring, the
    "PM RULING, 2026-08-26" block): that removal covers the window between
    THIS call's own kin (a stat-based check immediately before the commit)
    and the commit a moment later, which the PM ruling accepts as safe for
    a record already terminal, already claim-checked, and selected by a
    sweep that refuses live-claimed records — "a peer editing a closed
    record inside a one-call window is not a shape this fleet produces."
    Sitting at classification time instead, this gate covers a materially
    different, much larger window: the time between whichever peer session
    last touched the file and this sweep NOTICING it during its own
    classification pass, seconds to minutes earlier than the commit the
    retired check sat beside — exactly the case a concurrent ~20-session
    tree needs retained rather than moved out from under an in-progress
    edit, and exactly the window the retired ruling never spoke to.

    FAIL-CLOSED on any git failure: a non-zero exit or launch failure treats
    every candidate as dirty (retained), never as clean — an empty dirty set
    from a failed call would silently let every candidate sail through
    unexcluded, which is the opposite of what this gate exists to do.

    Scoped only to `candidate_relpaths` (never the whole `state/sizings`
    tree) — this is called from _handle_act, over the act-time-terminal
    survivor set, which is already bounded by the incoming candidate_ids
    list, so there is no whole-corpus scan or chunking machinery to port
    from the handoffs sibling (see this module's own Anti-scope: reuse the
    primitives, do not fork the 1500-line module).
    """
    return dirty_relpaths_from_porcelain(worktree_root, candidate_relpaths, caller="archive_sizings")


@register_op("fleet.archive_terminal_sizings")
async def _archive_terminal_sizings(
    params: dict, repo_root=None, scan_skipped: Optional[List[dict]] = None,
) -> dict:
    """JSON-RPC 'fleet.archive_terminal_sizings' handler.

    dry_run:true  → T1 preview: enumerate terminal sizings (status-only,
                    AC6-refused ones excluded); return candidates[] (mutates
                    nothing).
    dry_run:false → T3 act: re-verify each candidate_id, git-mv into
                    archive/sizings/YYYY-MM/, commit.

    repo_root arg is the git common dir (from _OP_KEY_SCOPE = "common_dir").
    Worktree root is derived engine-side via main_worktree_root(common_dir).
    params.repo_root is the optional D3 consistency check ONLY — NOT the
    worktree source (mirrors archive_plans.py's Key Decision 5).

    scan_skipped: optional caller-owned out-list, appended to in place with
    every T1-excluded terminal sizing (cannot-derive-date / AC6 forward-
    pointer refusal) as {"id", "reason"}. NOT part of the frozen dry_run
    wire envelope (build_dry_run_result's own `skipped` key stays `[]` at
    T1, unchanged — see this module's "Never-infer boundary" docstring) —
    this is a side channel for an in-process caller (sweep-terminal-
    sizings.py) that needs the T1 refusal census even when it collapses
    `candidates` to empty, mirroring sweep-terminal-handoffs.py's own
    `scan_skipped=` out-param on its classify call. JSON-RPC dispatch never
    passes this kwarg, so remote callers see no behavior change. Ignored
    entirely when dry_run:false (T3 act already reports its own skips via
    the wire `skipped[]` field).
    """
    validated = validate_params(params)
    if isinstance(validated, dict):
        return validated
    mode, dry_run, candidate_ids = validated

    if repo_root is None:
        return build_setup_error_result(
            mode, dry_run,
            "fleet.archive_terminal_sizings: repo_root arg is None — "
            "common_dir not supplied by engine (check _OP_KEY_SCOPE = 'common_dir')",
        )
    common_dir = Path(repo_root)
    worktree_root = main_worktree_root(common_dir)

    d3_error = check_repo_root(params.get("repo_root"), common_dir)
    if d3_error:
        return build_setup_error_result(mode, dry_run, d3_error)

    sizings_dir = worktree_root / "state" / "sizings"
    if not sizings_dir.is_dir():
        if dry_run:
            return build_dry_run_result(mode, [])
        return build_act_result(mode, [], [], [])

    if dry_run:
        return await _handle_preview(
            mode, worktree_root, sizings_dir, common_dir, scan_skipped=scan_skipped,
        )
    else:
        return await _handle_act(mode, worktree_root, sizings_dir, candidate_ids, common_dir)


async def _handle_preview(
    mode: str,
    worktree_root: Path,
    sizings_dir: Path,
    common_dir: Path,
    scan_skipped: Optional[List[dict]] = None,
) -> dict:
    candidates: List[dict] = []
    citer_index = _build_citer_index(worktree_root)

    for path in sorted(sizings_dir.glob("*.yaml")):
        status = parse_frontmatter_status(path)
        if status not in _TERMINAL_STATUSES:
            continue

        rel_path = rel_id(path, worktree_root)

        # terminal sizing with no YYYY-MM-DD prefix has no archive
        if _derive_yyyy_mm(path.name) is None:
            if scan_skipped is not None:
                scan_skipped.append({
                    "id": rel_path,
                    "reason": f"cannot-derive-date: filename {path.name!r} "
                              f"has no YYYY-MM-DD prefix",
                })
            continue

        plan_fk = _read_plan_fk(path)
        if plan_fk is not None:
            refusal = _forward_plan_refusal_reason(worktree_root, plan_fk)
            if refusal is not None:
                if scan_skipped is not None:
                    scan_skipped.append({"id": rel_path, "reason": refusal})
                continue

        roadmap_refusal = _roadmap_stubs_refusal_reason(
            worktree_root, path, rel_path, citer_index=citer_index
        )
        if roadmap_refusal is not None:
            if scan_skipped is not None:
                scan_skipped.append({"id": rel_path, "reason": roadmap_refusal})
            continue

        title = _extract_title(path) or path.stem
        candidates.append({
            "id": rel_path,
            "title": title,
            "status": status,
            "family": "sizing",
            "terminal_since": None,
            "note": None,
        })

    return build_dry_run_result(mode, candidates)


async def _handle_act(
    mode: str,
    worktree_root: Path,
    sizings_dir: Path,
    candidate_ids: List[str],
    common_dir: Path,
) -> dict:
    """T3 act: per-candidate re-verify + AC6 forward-pointer-refusal guard +
    dest-collision handling + git-mv + commit.

    For each candidate_id:
    1. Source gone → skipped reason:"already-archived" (idempotent replay,
       AC12-pinned — see _common._REASON_DEST_CONFLICT's docstring).
    2. Act-time terminality re-verify: drifted non-terminal → skipped
       reason:"terminality-drift:...".
    3. Worktree-dirty retention (AC5, `_dirty_sizing_relpaths`): applied at
       CLASSIFICATION time, immediately after step 2 and before any of steps
       4-6 below — see `_dirty_sizing_relpaths`' own docstring for why this
       position (not "just before the commit") is what makes its claimed
       window real.
    4. AC6 forward-pointer refusal: live plan FK → skipped with the
       distinct _REASON_FORWARD_PLAN_NOT_TERMINAL reason.
    5. No YYYY-MM prefix → skipped reason:"cannot-derive-date".
    6. Dest-collision: differing dst → skipped reason:_REASON_DEST_CONFLICT
       (never "already-archived", never clobbered); byte-identical dst →
       force-move (converge).
    7. Otherwise: build Move and add to batch.

    After all checks, calls archive_and_commit once for the full batch (ONE
    commit).
    """
    acted: List[dict] = []
    skipped: List[dict] = []
    failed: List[dict] = []

    sizings_dir_safe = sizings_dir.resolve()

    classified: List[Tuple[str, Path]] = []
    for cid in candidate_ids:
        sizing_path = worktree_root / cid

        resolved = sizing_path.resolve()
        if not resolved.is_relative_to(sizings_dir_safe):
            failed.append({
                "id": cid,
                "reason": "path-traversal: candidate_id escapes state/sizings/",
            })
            continue
        sizing_path = resolved

        if not sizing_path.exists():
            skipped.append({"id": cid, "reason": "already-archived"})
            continue

        status = parse_frontmatter_status(sizing_path)
        if status not in _TERMINAL_STATUSES:
            skipped.append({
                "id": cid,
                "reason": f"terminality-drift: status is now {status!r}",
            })
            continue

        classified.append((cid, sizing_path))

    # Worktree-dirty retention gate (AC5) — CLASSIFICATION TIME: scoped to
    dirty: Set[str] = set()
    if classified:
        dirty = _dirty_sizing_relpaths(worktree_root, [cid for cid, _p in classified])

    candidate_moves: Dict[str, Move] = {}
    citer_index = _build_citer_index(worktree_root) if classified else {}

    for cid, sizing_path in classified:
        if cid in dirty:
            skipped.append({"id": cid, "reason": REASON_WORKTREE_DIRTY})
            continue

        plan_fk = _read_plan_fk(sizing_path)
        if plan_fk is not None:
            refusal = _forward_plan_refusal_reason(worktree_root, plan_fk)
            if refusal is not None:
                skipped.append({"id": cid, "reason": refusal})
                continue

        roadmap_refusal = _roadmap_stubs_refusal_reason(
            worktree_root, sizing_path, cid, citer_index=citer_index
        )
        if roadmap_refusal is not None:
            skipped.append({"id": cid, "reason": roadmap_refusal})
            continue

        yyyy_mm = _derive_yyyy_mm(sizing_path.name)
        if yyyy_mm is None:
            skipped.append({
                "id": cid,
                "reason": f"cannot-derive-date: filename {sizing_path.name!r} "
                          f"has no YYYY-MM-DD prefix",
            })
            continue

        dst = worktree_root / "archive" / "sizings" / yyyy_mm / sizing_path.name

        force = False
        if dst.exists():
            if not _is_identical_duplicate(sizing_path, dst):
                # A DIFFERENT file already occupies the archive destination —
                skipped.append({"id": cid, "reason": _REASON_DEST_CONFLICT})
                continue
            force = True

        candidate_moves[cid] = Move(src=sizing_path, dst=dst, candidate_id=cid, force=force)

    if not candidate_moves:
        return build_act_result(mode, acted, skipped, failed)

    flat_moves: List[Move] = list(candidate_moves.values())

    n_sizings = len(candidate_moves)
    subject = f"fleet: archive {n_sizings} terminal sizing-object(s) [fleet.archive_terminal_sizings]"
    raw_acted, raw_failed = await archive_and_commit(worktree_root, flat_moves, subject)

    raw_acted_ids: Set[str] = {a["id"] for a in raw_acted}
    raw_failed_by_id: Dict[str, str] = {f["id"]: f["reason"] for f in raw_failed}

    for cid in candidate_moves:
        if cid in raw_failed_by_id:
            failed.append({"id": cid, "reason": raw_failed_by_id[cid]})
        elif cid in raw_acted_ids:
            acted.append({"id": cid, "archived": True})
        else:
            failed.append({"id": cid, "reason": "unexpected: absent from acted and failed"})

    return build_act_result(mode, acted, skipped, failed)

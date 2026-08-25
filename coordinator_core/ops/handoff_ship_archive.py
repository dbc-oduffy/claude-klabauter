"""
coordinator_core.ops.handoff_ship_archive — "handoff.ship_and_archive" op.

Purpose: Event-driven, single-handoff *ship-then-archive* composite. Gives the
terminal-transition seam (the /workstream-complete + /handoff skill callers) ONE
call that (1) stamps deployment_state:shipped and (2) archives the handoff to
archive/handoffs/YYYY-MM/ immediately — instead of stamping now and waiting for
the batch session.boot_sweep to notice. This is claude-klabauter's engine realization of
the "fire archival off the terminal-transition event, not a 24h/boot batch"
directive (cross-repo memo 2026-07-12-example-retrieval-repo-em-handoff-archival-*), reconciled
with DR-215's spawn-per-call model: there is no resident daemon to host a deferred
~10s timer, so event-driven == inline-after-transition (one op invocation).

Composition (pure reuse of the tested single-verb internals — same cross-module
reuse pattern as session.boot_sweep):
  1. handoff.stamp     — stamp shipped_in:<sha> when a sha param is supplied and the
                         field is absent (idempotent). Archival's SHA-gate needs it.
  2. handoff.transition ship verb (_ship) — deployment_state → shipped (idempotent).
  3. fleet.archive_shipped_handoffs act path (_handle_act) — re-verifies terminality
     (deployment_state:shipped + git-reachable shipped_in) and git-mv+commits.
     Passes holder_initiated=True (2026-08-13): this call site archives a handoff
     the CALLING session itself holds — Check 3's live-claim-dir gate
     (_is_shipped_terminal, added same date) would otherwise read that self-claim
     as live and skip every archive on this path, since cs_claim_holder_live has
     no self-vs-peer discrimination. See _is_shipped_terminal's holder_initiated
     docstring paragraph for the full rationale; this is the ONE caller that opts
     in — the standalone fleet op and session.boot_sweep's batch sweep (both
     background, never the holder) must never pass it.

On the archived outcome (archived: True), the ship stamp is NOT committed separately:
this module's own call into _handle_act (imported as _archive_shipped_act) passes
restage_src=True, which runs a targeted `git add -- <src>` (private index only)
immediately before the git mv for THIS handoff, picking up Step 2's shipped_in
stamp from src's current on-disk content rather than the private index's
HEAD-seeded blob — so the stamp lands in the archival commit. This restage_src
opt-in is scoped to exactly this one call site (this module composing
handoff.stamp + handoff.transition + the fleet act path); it is NOT the default
for _handle_act's other two callers (the standalone fleet.archive_shipped_handoffs
op and session.boot_sweep's batch sweep), which never write to a candidate's src
immediately before archiving it and so keep restage_src's default of False. See
_handle_act's restage_src param docstring and archive_and_commit's "op-authored
pre-move content" / ACCEPTED RISK notes (coordinator_core/ops/fleet/_common.py)
for the full mechanism and its accepted (not eliminated) absorption window.

Graceful partial outcome (this is a FEATURE, not an error): if shipped_in is neither
supplied nor already present, step 2 still stamps deployment_state:shipped (closing the
Bug-2 "shipped work never marked terminal" gap) while step 3 skips archival with
reason "terminality-drift: shipped_in ... " — the handoff is now terminal-marked. On
THIS branch, unlike the archived outcome above, the deployment_state:shipped
frontmatter mutation is left UNCOMMITTED in the working tree — there is no archival
commit this call to carry it. Per the disk/HEAD drift guard in archive_and_commit
(coordinator_core/ops/fleet/_common.py, commit 4541069c3), a batch session.boot_sweep
pass CANNOT archive it from this state: its restage_src=False call re-verifies
terminality against fresh disk content but git-mv's from the HEAD-seeded private
index, so the guard HARD-REFUSES the move (exit_code:2, a per-item failure — not the
soft skip this paragraph described pre-guard) rather than commit HEAD's stale
pre-stamp blob. The stamp must first be COMMITTED (by any means) before boot_sweep's
batch path can act on it — until then the record sits in state/handoffs/, its
attempted archival landing in failed[], not skipped[]. The only call that tolerates a
fresh, still-uncommitted stamp on THIS handoff is a SUBSEQUENT handoff.ship_and_archive
call once a shipped_in lands — its own step 3 passes restage_src=True, which stages
src's on-disk content immediately before the move so the stamp rides into that call's
own archival commit.

Self-registration: importing this module fires register_op("handoff.ship_and_archive").
Add to coordinator_core/ops/__init__.py and register its scope ("common_dir") in
ipc.py::_OP_KEY_SCOPE.

Spec backlink: cross-repo/archive/2026-07-12-example-retrieval-repo-em-handoff-archival-mtime-veto-and-marking-gap.md

Negative-spec:
  - Does NOT introduce an mtime-keyed freshness veto — eligibility is the fleet op's
    frontmatter/SHA predicate (R5: correctness-critical reads key on content/git-rev,
    not mtime). This op adds no time window of its own.
  - Does NOT mutate deployment_state inside handoff.transition beyond the ship verb —
    the archival git-mv is a distinct step, not folded into the atomic frontmatter write.
  - Does NOT use params.repo_root as the path source — repo_root handler arg is the git
    common dir; worktree via main_worktree_root(common_dir), same as the fleet op.
  - Does NOT fall back to a branch-tip SHA — a caller that wants shipped_in stamped must
    pass the real shipping-commit sha; absent sha → archival defers (graceful).
  - Does NOT batch: exactly one handoff per call (the fleet ops remain the N-candidate path).
"""

from __future__ import annotations
import sys

import asyncio
import logging
from pathlib import Path
from typing import Optional

from coordinator_core.ipc import register_op
from coordinator_core.ops._path_guard import contained_path
from coordinator_core.ops.fleet._common import main_worktree_root
# DEGRADED 2026-08-25, deliberately not repaired here. C1b deleted
# `ops/fleet/archive_shipped_handoffs.py` as subsumed by
# `archive_terminal_handoffs.py` and swept its callers, but missed THIS one, so
# this module-scope import raised ModuleNotFoundError. That took down not just
# this op but `coordinator_core.ops` and `coordinator_core.baton_assemble` with
# it -- every handoff and pickup in the repo, for one dead op.
#
# The import is now guarded so the blast radius is this op alone; it fails loudly
# when INVOKED instead of when imported. NOT repointed at the successor's
# `_handle_act`, because that is not a rename: the deleted signature took
# `restage_src` and `holder_initiated` (the latter an opt-out of the live-claim
# gate, whose docstring names THIS module as its only opt-in caller) and the
# successor takes neither, plus a required `cap`. Silently dropping a
# claim-gate opt-out is a safety change, not a migration, and it is C1b's
# premise to settle rather than a handoff's.
try:
    from coordinator_core.ops.fleet.archive_shipped_handoffs import (  # type: ignore[import]
        _handle_act as _archive_shipped_act,
    )
except ModuleNotFoundError:
    _archive_shipped_act = None  # type: ignore[assignment]
from coordinator_core.ops.handoff_stamp import _SHIPPED_IN_KIND_ENUM
from coordinator_core.ops.handoff_stamp import _handler as _stamp_handler
from coordinator_core.ops.handoff_transition import _ship
from coordinator_core.frontmatter.primitives import read_fm_field_unquoted, split_frontmatter
# Aliased: `rel_id` is also a local variable name in _handler below, and an
# unaliased import would be shadowed by that binding (UnboundLocalError).
from coordinator_core.wire_paths import rel_id as _wire_rel_id

_LOG = logging.getLogger(__name__)

# Mode token echoed into the fleet act envelope (fleet ops use "already-terminal"
# for the self-selecting, non-cockpit-round-trip path — parity with session.boot_sweep).
# Review: code-reviewer F6 — session.boot_sweep also inlines this same literal rather
# than exporting it as a named constant, so there is nothing to import here; kept local.
_MODE = "already-terminal"


def _err(msg: str) -> dict:
    """Return an exit_code=1 setup/transition-error envelope."""
    _LOG.warning("handoff.ship_and_archive: %s", msg)
    return {
        "exit_code": 1,
        "shipped": False,
        "shipped_in_stamped": False,
        "archived": False,
        "archive_skip_reason": None,
        "error": msg,
    }


@register_op("handoff.ship_and_archive")
async def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "handoff.ship_and_archive" — stamp shipped + archive one handoff, event-driven.

    Params:
        handoff_path (str, required) — absolute or repo-relative path (must resolve
                     under <worktree>/state/handoffs/; archived handoffs are out of scope).
        sha          (str, optional) — shipping-commit SHA to stamp as shipped_in when
                     absent (idempotent). Omit only when shipped_in is already present.
        kind         (str, optional) — `shipped_in_kind` discriminant (DR-096) to stamp
                     alongside `sha`. Defaults to "ship-commit" when `sha` is supplied
                     and `kind` is omitted — the overwhelming majority of this handler's
                     callers hand it a caller-resolved shipping commit, which IS the
                     ship-commit case. The one caller that means something else
                     (`handoff_reconcile.py`'s pinned-predecessor-chain reconciliation,
                     gate (a): stamping a successor's sha onto an ancestor) passes
                     `kind="successor"` explicitly rather than relying on this default.
                     Rejected (no write) when supplied and outside
                     `coordinator_core.ops.handoff_stamp._SHIPPED_IN_KIND_ENUM`.

    repo_root handler arg is the git common dir (_OP_KEY_SCOPE="common_dir"); the
    worktree is derived via main_worktree_root(repo_root).

    Returns:
        exit_code (int) — 0 ok (incl. graceful archive-skip); 1 setup/ship error;
                          2 archival per-item failure (DETERMINATE-PARTIAL).
        shipped (bool) — deployment_state:shipped is now set (freshly or already).
        shipped_in_stamped (bool) — a shipped_in value was written this call.
        archived (bool) — the handoff was git-mv'd into archive/handoffs/.
        archive_skip_reason (str|None) — why archival did not act (e.g. shipped_in absent).
        message (str) — human-readable outcome.
    """
    handoff_path = (params.get("handoff_path") or "").strip()
    sha = (params.get("sha") or "").strip()
    kind_raw = params.get("kind")
    kind = kind_raw.strip() if isinstance(kind_raw, str) and kind_raw.strip() else None

    if not handoff_path:
        return _err("'handoff_path' is required")
    if kind is not None and kind not in _SHIPPED_IN_KIND_ENUM:
        return _err(
            f"rejected unknown kind {kind!r} — must be one of "
            f"{sorted(_SHIPPED_IN_KIND_ENUM)}, or omitted entirely"
        )
    if repo_root is None:
        return _err(
            "repo_root is required (handler called without socket-authoritative common_dir)"
        )

    worktree = main_worktree_root(repo_root)

    # Containment: mutation verb — resolved path MUST be under state/handoffs/
    # (archive/handoffs/ deliberately excluded), mirroring handoff.stamp/transition.
    p = Path(handoff_path)
    if not p.is_absolute():
        p = worktree / p
    contained = contained_path(p, [worktree / "state" / "handoffs"])
    if contained is None:
        return _err(f"handoff_path escapes state/handoffs/: {handoff_path!r}")

    rel_id = _wire_rel_id(contained, worktree)
    # Review: code-reviewer F7 — rel_id is re-resolved inside _stamp_handler and again
    # inside _ship's own _resolve_path. This is intentional re-validation-in-depth: each
    # sibling op independently re-checks containment rather than trusting this call's
    # resolution, not dead/redundant code.

    if not contained.is_file():
        # Idempotent replay: a prior call already ship+archived this handoff, so it
        # no longer lives in state/handoffs/. If the basename is present under
        # archive/handoffs/, report the terminal already-archived outcome (exit_code:0,
        # parity with the fleet op's "already-archived" skip) rather than a not-found error.
        #
        # Review: code-reviewer F1 — basename-only matching is not identity-safe: two
        # distinct handoffs can share the same YYYY-MM-DD-<slug>.md basename across
        # different archive months. Only accept a candidate as THIS call's replay when
        # its frontmatter shipped_in equals the sha this call supplied — that is the one
        # stable identity field a replay caller can cross-check. When no sha was supplied
        # on this call, shipped_in cannot be identity-verified against anything, so the
        # match is conservatively rejected and this falls through to the not-found error
        # rather than risk masking a typo'd/deleted path as a benign replay.
        archive_root = worktree / "archive" / "handoffs"
        if sha and archive_root.is_dir():
            for cand in archive_root.rglob("*.md"):
                if cand.name != contained.name or not cand.is_file():
                    continue
                try:
                    cand_text = cand.read_text(encoding="utf-8")
                except OSError:
                    print(f"skip: _handler: cand_text = cand.read_text(encoding=\"utf-8\") failed: {sys.exc_info()[1]}", file=sys.stderr)
                    continue
                split = split_frontmatter(cand_text)
                if split is None:
                    continue
                # Unquoted read: handoff_stamp writes shipped_in with
                # numeric_quoting=True, so an all-digit ('44379324') or
                # scientific-notation ('23814e50') sha8 lands single-quoted on
                # disk. A raw read compares "'44379324'" against the bare sha
                # and never matches, turning a legitimate idempotent replay into
                # a hard "handoff not found on disk" error on ~13% of commits.
                cand_shipped_in = read_fm_field_unquoted(split.fm_text, "shipped_in")
                if cand_shipped_in == sha:
                    return {
                        "exit_code": 0,
                        "shipped": True,
                        "shipped_in_stamped": False,
                        "archived": True,
                        "archive_skip_reason": None,
                        "message": f"{rel_id} already shipped and archived (idempotent replay)",
                    }
        return _err(f"handoff not found on disk: {handoff_path}")

    # --- Step 1: stamp shipped_in (only when a sha is supplied; idempotent) ---
    shipped_in_stamped = False
    if sha:
        stamp_res = await _stamp_handler(
            {"handoff_path": rel_id, "sha": sha, "kind": kind or "ship-commit"}, repo_root
        )
        if stamp_res.get("exit_code") != 0:
            return _err(
                f"shipped_in stamp failed: {stamp_res.get('error', 'unknown error')}"
            )
        shipped_in_stamped = bool(stamp_res.get("applied"))

    # --- Step 2: ship transition (deployment_state -> shipped; idempotent) ---
    ship_res = await asyncio.to_thread(_ship, rel_id, worktree, repo_root)
    if ship_res.get("exit_code") != 0:
        out = _err(f"ship transition failed: {ship_res.get('error', 'unknown error')}")
        out["shipped_in_stamped"] = shipped_in_stamped
        return out

    # --- Step 3: archive immediately (re-verifies terminality; git-mv + commit) ---
    # restage_src=True: this call site is the ONE caller in the _handle_act family
    # that just wrote src's own frontmatter (Step 2's ship stamp) moments before
    # this call — see _handle_act's restage_src param docstring and
    # archive_and_commit's "op-authored pre-move content" note for why this is
    # the sole opt-in site, not a default for the shared internal.
    #
    # holder_initiated=True: this call site archives a handoff the CALLING
    # session itself holds — see this module's own docstring composition step 3
    # and _is_shipped_terminal's holder_initiated docstring for the full
    # rationale. common_dir is deliberately NOT passed here: holder_initiated=True
    # makes _is_shipped_terminal skip Check 3 unconditionally, so common_dir would
    # never be read for this call — passing it would be a dead, misleading
    # argument.
    if _archive_shipped_act is None:
        return _err(
            "handoff.ship_and_archive is inoperative: its archive leg "
            "(ops/fleet/archive_shipped_handoffs) was deleted by the C1b "
            "subsumption without migrating this caller. The successor drops "
            "the live-claim-gate opt-out this op depends on, so the migration "
            "is a safety decision, not a repoint. Ship and archive as two "
            "steps until it is settled."
        )
    act = await _archive_shipped_act(
        _MODE, worktree, [rel_id], restage_src=True, holder_initiated=True
    )

    acted = act.get("acted", [])
    skipped = act.get("skipped", [])
    failed = act.get("failed", [])

    archived = any((item.get("id") == rel_id) for item in acted)
    archive_skip_reason: Optional[str] = None
    if not archived:
        for item in skipped:
            if item.get("id") == rel_id:
                archive_skip_reason = item.get("reason")
                break

    # exit_code 2 only when archival reported a per-item FAILURE (not a graceful skip).
    exit_code = 2 if failed else 0

    if archived:
        message = f"shipped and archived {rel_id}"
    elif failed:
        message = f"shipped {rel_id}; archival failed"
    else:
        message = (
            f"shipped {rel_id}; archival skipped "
            f"({archive_skip_reason or 'not eligible'})"
        )

    return {
        "exit_code": exit_code,
        "shipped": True,
        "shipped_in_stamped": shipped_in_stamped,
        "archived": archived,
        "archive_skip_reason": archive_skip_reason,
        "message": message,
    }

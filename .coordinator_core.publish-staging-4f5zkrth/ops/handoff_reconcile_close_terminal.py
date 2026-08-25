"""
coordinator_core.ops.handoff_reconcile_close_terminal — "handoff.reconcile_close_terminal" op.

Purpose: composed close-and-archive primitive for the "reconcile-to-terminal,
no successor" shape (cross-repo/inbox/2026-08-04-market-intelligence-em-baton-
terminal-state-not-cleared-programmatically.md, defect 1). A baton whose every
next-step was closed by work that landed after it was written has nothing left
to execute — the reconcile conclusion is genuinely terminal, but neither
`handoff.reconcile_open`'s C2/C3 commit-matching machinery nor
`handoff.archive_transition`'s own modes have a call shape for "a human/session
concluded this scope is moot; stamp and archive it" (`handoff.archive_transition`
mode="supersede" REQUIRES a positive `continued_into` successor by construction,
and mode="chain"/"stamp_shipped"/"stamp_only" all assume a SHIPPED terminal —
none of the four modes fit a baton with no shipping commit and no successor at
all). This op is that missing call shape, so a reconcile-to-terminal conclusion
can be TURNED INTO a stamp + archive in one call, exactly like any other
terminal transition, rather than narrated into an audit-record file and
left there.

Investigation finding (this op's own spec backlink): no code path in this repo
currently PRODUCES a `*-baton-reconciled-closed.md` audit record — grepping the
full tree for that filename pattern, for `kind: audit-record`+"claimed and
terminal", and for any writer under `coordinator_core/` or `coordinator/bin/`
found zero matches. The audit record the reporting memo describes is authored
by a human/session ceremony (external to this engine's op registry, most
plausibly a coordinator-claude skill in a sibling repo) using a generic
doc-authoring surface, not by any `coordinator_core` op. That ceremony is
therefore the ACTING CALLER this op exists to complete: it should call THIS op
once its own reconcile judgment is made, instead of stopping after the audit
write. This module cannot compel that call (the ceremony lives outside this
repo's write-scope) — it only makes the correct next step exist and be no
harder to reach for than the audit-authoring step already is.

Composition (pure reuse of tested single-verb internals — same pattern as
`handoff_ship_archive.py`'s ship+archive composite):
  0. `handoff_children._handoff_has_live_children` (2026-08-10 fix, cross-
     repo/inbox/2026-08-10-doe-claude-em-reconcile-close-terminal-and-scrub-
     key.md § 2) — the SAME live-lineage-edge guard step 2's chain-mode call
     runs internally, reused here BEFORE step 1 so a live successor edge
     refuses the call outright instead of letting step 1 stamp closed_reason:
     "displaced" (schema-forbidden with a lineage edge present) and step 2
     merely retain the archival move on the same fact, too late to matter.
     See § Live-lineage-edge guard below and the `_handler` inline comment
     at the call site for the full design rationale and the reported verb-
     gap this refusal surfaces rather than solves.
  1. `handoff_transition._close` — deployment_state -> closed, closed_reason:
     <reason> (DR-084 human/session-only terminal; `reason` must be one of
     `handoff_transition._CLOSED_REASONS` — "displaced" is the expected value
     for THIS shape: the baton's own next-steps were displaced/subsumed by
     other work that landed after it was written, AND step 0 has confirmed
     no live successor names this baton as its predecessor). Also clears
     `pickup_ready: false` (2026-08-10 fix, same memo § 1) — a closed baton
     has nothing left to advertise to /pickup or boot-sweep triage; see
     `_close`'s own docstring for the full rationale, now unconditional in
     that verb so every caller of `_close` (not just this op) gets it.
  2. `handoff_archive_transition._handler(mode="chain")` — the terminal-state
     precondition it already enforces (git-mv only once deployment_state is
     one of shipped|continued|closed) is satisfied by step 1's fresh write,
     and the unconditional live-children guard still applies exactly as it
     does for every other chain-mode call — this op adds no privilege a caller
     invoking the two verbs by hand would not already have. Its guard is now
     structurally redundant with step 0's fresh check for archival purposes
     (a live edge that would retain here was already refused at step 0), but
     is left unmodified — it is still the correct authority for its own
     go/no-go decision, and de-duplicating the two calls is out of scope for
     this fix.

Idempotency: a SECOND call against a handoff already closed+archived by a
prior call to this op resolves `handoff_path` under one of
`fleet._common.ARCHIVE_ROOT_SUBDIRS` (chain mode's own containment is
live-only, so a naive replay would hit `_close`'s "escapes state/handoffs/"
refusal) — this op checks that shape FIRST and, when the archived record's
own on-disk `deployment_state` is already terminal, returns a clean no-op
(`closed: True` (already), `archived: True` (already), `exit_code: 0`) without
attempting any mutation. A resolved archive-root path whose deployment_state
is NOT terminal is a corrupt-by-construction state
(`handoff_archive_transition`'s own module docstring: a non-terminal baton
under `archive/handoffs/` can never afterwards be repaired by any transition
verb, since `handoff_transition._resolve_path` refuses any path outside
`state/handoffs/`) — this op surfaces that as an error rather than attempting
a repair no verb in this codebase is authorized to perform.

Self-registration: importing this module fires
register_op("handoff.reconcile_close_terminal"). Added to
coordinator_core/ops/__init__.py and registered "common_dir" scope in
coordinator_core/op_scopes.py, matching handoff.archive_transition/
handoff.ship_and_archive's own precedent.

Spec backlink: cross-repo/inbox/2026-08-04-market-intelligence-em-baton-
terminal-state-not-cleared-programmatically.md, defect 1, item 2. Also
cross-repo/inbox/2026-08-10-doe-claude-em-reconcile-close-terminal-and-
scrub-key.md § 1-2 (pickup_ready + live-lineage-edge fixes).

Negative-spec:
  - Does NOT re-derive whether a baton's next-steps are actually closed
    elsewhere — that judgment (the C2/C3-style evidence-matching this op does
    NOT attempt) stays with whichever caller decided to invoke this op; this
    op composes the terminal-stamp + archive-move ONLY, on a `reason` the
    caller has already decided.
  - Does NOT decide whether a baton with a live lineage edge but no claim
    history CAN be superseded — the step 0 guard only detects the edge and
    refuses this op's own (wrong) route; whether DR-242 should ever admit a
    never-claimed predecessor with a genuine successor is a discriminator
    change this op does not make (see module docstring § Composition step 0
    and this fix's own session report — DR-242's discriminator is project-
    makima's to own, ratified in archive/specs/2026-08/2026-08-02-roadmap-
    baton-lifecycle-model.md, not re-decided from a single op's bugfix).
  - Does NOT hand-write frontmatter — reuses `handoff_transition._close` and
    `handoff_archive_transition._handler` verbatim; no field is set outside
    those two calls' own contracts.
  - Does NOT stamp `deployment_state:closed` on an already-shipped/continued
    handoff — `_close` itself refuses that (see `_CLOSE_CONFLICTING_TERMINALS`)
    and this op propagates the refusal unchanged.
  - Does NOT batch — exactly one handoff per call, mirroring
    `handoff.ship_and_archive`'s single-handoff contract.
  - Does NOT attempt archival when the close step itself failed — the two
    steps run in sequence and a close failure short-circuits before any
    archive_transition call, leaving the handoff exactly as it was pre-call.
"""

from __future__ import annotations
import sys

import asyncio
import logging
from pathlib import Path
from typing import Optional

from coordinator_core.frontmatter.primitives import read_fm_field_unquoted, split_frontmatter
from coordinator_core.ipc import register_op
from coordinator_core.ops._path_guard import contained_path
from coordinator_core.ops.fleet._common import ARCHIVE_ROOT_SUBDIRS, main_worktree_root
from coordinator_core.ops.handoff_archive_transition import _handler as _archive_transition_handler
from coordinator_core.ops.handoff_children import _handoff_has_live_children
from coordinator_core.ops.handoff_transition import _CLOSED_REASONS, _close
# Aliased: `rel_id` is also a local variable name in _handler below, and an
# unaliased import would be shadowed by that binding (UnboundLocalError) —
# mirrors handoff_ship_archive.py / handoff_archive_transition.py's own
# aliasing convention.
from coordinator_core.wire_paths import rel_id as _wire_rel_id

_LOG = logging.getLogger(__name__)

# Mirrors handoff_archive_transition._TERMINAL_DEPLOYMENT_STATES (vendored,
# not imported — same "one constant, duplicated across a module boundary
# rather than pulling in a full op-registration side effect" precedent that
# module's own docstring already documents for _SCHEMA_PATH, and
# baton_drift_sweep.py already follows for this exact constant).
_TERMINAL_DEPLOYMENT_STATES = frozenset({"shipped", "continued", "closed"})


def _err(msg: str) -> dict:
    """Return an exit_code=1 setup/transition-error envelope."""
    _LOG.warning("handoff.reconcile_close_terminal: %s", msg)
    return {
        "exit_code": 1,
        "closed": False,
        "archived": False,
        "retained": False,
        "retain_reason": None,
        "error": msg,
        "message": None,
    }


def _usage_error(msg: str) -> dict:
    """Return an exit_code=2 usage-error envelope (invalid params)."""
    _LOG.warning("handoff.reconcile_close_terminal: usage error: %s", msg)
    return {
        "exit_code": 2,
        "closed": False,
        "archived": False,
        "retained": False,
        "retain_reason": None,
        "error": msg,
        "message": None,
    }


def _read_deployment_state(handoff_abs: Path) -> "tuple[Optional[str], Optional[str]]":
    """Best-effort read of an on-disk handoff's `deployment_state:` value.
    Mirrors `handoff_archive_transition._current_fm_field`'s narrow
    single-field reader (not imported — that helper is module-private to
    its own file).

    Returns `(state, read_error)`:
      - `read_error` is None on a clean read of the file itself (even when
        `state` then resolves to None because the field/frontmatter is
        absent) — the caller's "not terminal" message is accurate here,
        the field genuinely wasn't found.
      - `read_error` is a human-readable string when the FILE could not be
        read at all — distinguishing "missing" (FileNotFoundError) from
        "unreadable for another reason" (permission error, transient I/O
        failure, etc. — any other OSError), so the caller can report a
        truthful "could not be read" message instead of folding both into
        a misleading `deployment_state: None → not terminal` (P3 fix: the
        prior bare `except OSError` conflated the two)."""
    try:
        text = handoff_abs.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None, "file does not exist"
    except OSError as exc:
        print(
            f"skip: _read_deployment_state: text = handoff_abs.read_text(encoding=\"utf-8\") failed: {exc}",
            file=sys.stderr,
        )
        return None, f"unreadable: {exc}"
    split = split_frontmatter(text)
    if split is None:
        return None, None
    val = read_fm_field_unquoted(split.fm_text, "deployment_state")
    return (val if val not in (None, "null", "") else None), None


@register_op("handoff.reconcile_close_terminal")
async def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "handoff.reconcile_close_terminal" — close (DR-084 human/
    session-only terminal) + archive one handoff in a single call, for the
    "reconcile concluded terminal, no successor" shape.

    Params:
        handoff_path (str, required) — absolute or repo-relative path. Must
                     resolve under <worktree>/state/handoffs/ on a first call;
                     a resolved path under one of
                     `fleet._common.ARCHIVE_ROOT_SUBDIRS` is tolerated ONLY as
                     the idempotent-replay shape (see module docstring).
        reason       (str, required) — must be one of
                     `handoff_transition._CLOSED_REASONS`
                     ("cancelled" | "displaced" | "stale"). "displaced" is the
                     expected value for a no-successor reconcile-to-terminal
                     (the baton's own next-steps were subsumed/displaced by
                     other work that landed after it was written).
        exclude      (list[str], optional) — threaded verbatim to
                     `handoff.archive_transition`'s own `exclude` param (paths
                     dropped from the live-children guard's scan set).

    Returns:
        exit_code (int)          — 0 ok (incl. idempotent replay and a
                                   graceful live-children retain); 1
                                   close/archive-transition error; 2 usage
                                   error (missing/invalid params).
        closed (bool)            — deployment_state:closed is now set
                                   (freshly this call, or already on a prior
                                   call — see `already_closed`).
        already_closed (bool)    — the close step was a no-op because the
                                   handoff already carried
                                   deployment_state:closed with this SAME
                                   reason.
        archived (bool)          — the handoff is now under
                                   archive/handoffs/YYYY-MM/ (freshly this
                                   call, or already — see `already_archived`).
        already_archived (bool)  — True on the idempotent-replay shape (this
                                   call's `handoff_path` resolved under an
                                   archive root with an already-terminal
                                   deployment_state); no mutation attempted.
        retained (bool)          — the live-children guard retained (see
                                   `handoff.archive_transition`'s own
                                   contract) — the close still applied even
                                   when this is True.
        retain_reason (str|None) — human-readable reason when retained.
        message (str)            — human-readable outcome summary.

    P9 WORKTREE DERIVATION: repo_root arrives as the git common dir
    (<worktree>/.git); main_worktree_root(repo_root) derives the worktree.
    """
    handoff_path_raw: str = (params.get("handoff_path") or "").strip()
    reason: str = (params.get("reason") or "").strip()
    exclude = params.get("exclude") or []

    if not handoff_path_raw:
        return _usage_error("'handoff_path' is required")
    if reason not in _CLOSED_REASONS:
        return _usage_error(
            f"'reason' must be one of {sorted(_CLOSED_REASONS)} (got {reason!r}) "
            "— mirrors handoff_transition._close's own closed_reason enum"
        )
    if repo_root is None:
        return _err(
            "repo_root is required (handler called without socket-authoritative common_dir)"
        )

    worktree = main_worktree_root(repo_root)

    p = Path(handoff_path_raw)
    if not p.is_absolute():
        p = worktree / p

    live_root = worktree / "state" / "handoffs"
    contained_live = contained_path(p, [live_root])

    if contained_live is None:
        # Not live — check the idempotent-replay shape (already closed +
        # archived by a prior call to this op) before treating this as an
        # error. See module docstring § Idempotency.
        archive_roots = [worktree / sub for sub in ARCHIVE_ROOT_SUBDIRS]
        contained_archived = contained_path(p, archive_roots)
        if contained_archived is None or not contained_archived.is_file():
            return _err(
                f"handoff_path escapes state/handoffs/ and every known archive "
                f"dir ({', '.join(ARCHIVE_ROOT_SUBDIRS)}): {handoff_path_raw!r}"
            )
        state, read_error = _read_deployment_state(contained_archived)
        if read_error is not None:
            rel_id = _wire_rel_id(contained_archived, worktree)
            return _err(
                f"{rel_id} lives under an archive root but its on-disk "
                f"deployment_state could not be read ({read_error}) — cannot "
                "determine idempotent-replay terminality; refusing rather "
                "than treating an unreadable file as a non-terminal state"
            )
        if state in _TERMINAL_DEPLOYMENT_STATES:
            rel_id = _wire_rel_id(contained_archived, worktree)
            return {
                "exit_code": 0,
                "closed": True,
                "already_closed": True,
                "archived": True,
                "already_archived": True,
                "retained": False,
                "retain_reason": None,
                "message": f"{rel_id} already deployment_state:{state} and archived (idempotent replay)",
            }
        rel_id = _wire_rel_id(contained_archived, worktree)
        return _err(
            f"{rel_id} lives under an archive root but its on-disk "
            f"deployment_state ({state!r}) is not terminal — this is a "
            "corrupt-by-construction state (handoff_transition._resolve_path "
            "refuses any mutation outside state/handoffs/, so no verb in "
            "this codebase can repair it); refusing rather than attempting "
            "an unauthorized repair"
        )

    rel_id = _wire_rel_id(contained_live, worktree)

    # --- Step 0: live-lineage-edge guard (defect 2, cross-repo/inbox/2026-
    # 08-10-doe-claude-em-reconcile-close-terminal-and-scrub-key.md § 2) ---
    # The handoff schema's own closed_reason description is explicit:
    # "displaced = replaced with NO lineage edge (with an edge it's
    # continued, not closed)". This op's `reason` param defaults callers
    # toward "displaced" for the no-successor reconcile-to-terminal shape
    # (see module docstring), but nothing before this fix verified the
    # "no successor" half of that premise — a caller could pass reason=
    # "displaced" against a baton a live successor had already named via
    # `predecessor:`, and step 1 would stamp the schema-forbidden combination
    # sight-unseen.
    #
    # Reuses `handoff.has_live_children` (handoff_children._handoff_has_live_
    # children) rather than reimplementing lineage detection — this is the
    # EXACT SAME guard `handoff.archive_transition` mode="chain" runs at step
    # 2 below (its own retain_kind="live-parent" / "still a live merge-parent
    # of another active handoff" message). Running it there is too late: by
    # the time chain mode's guard retains the archival move, step 1 has
    # already written closed_reason:displaced to disk. Running the SAME
    # check here, before any mutation, catches it before either field is
    # touched.
    #
    # On a live edge (guard exit_code==0) or an indeterminate guard
    # (exit_code==2, fail-closed — same posture as every other consumer of
    # this guard in this codebase), this op REFUSES rather than either of
    # the two alternatives considered:
    #   - silently downgrading "displaced" to "stale"/"cancelled" would still
    #     write deployment_state:closed over a live successor edge, which
    #     the schema's own continued-vs-closed distinction says is simply
    #     the wrong terminal, not a wrong SUBTYPE of the right terminal;
    #   - writing deployment_state:continued + continued_into: directly
    #     (the caller's own hand workaround) would duplicate
    #     handoff_archive_transition._supersede_continued's tested holder-
    #     attribution, conflict-detection, and roadmap-baton-kind gating
    #     logic a second time in this module, AND would require this op to
    #     decide whether DR-242 applies to the human/session-invoked door —
    #     an explicit non-goal (see report to EM/PM: DR-242's discriminator
    #     is project-makima's to own, not to be re-decided from a single
    #     op's fix).
    # Refusing and naming the correct verb keeps this op's own scope exactly
    # as documented (module docstring: "the reconcile-to-terminal, NO
    # SUCCESSOR shape") — a baton WITH a successor edge was never this op's
    # shape to begin with.
    #
    # KNOWN GAP (reported, not solved here): `handoff.archive_transition`
    # mode="supersede" AND the `handoff.transition` verb dispatcher's
    # "supersede" verb BOTH gate on DR-242 (`claimed_or_shipped_at_path`) —
    # a predecessor that was never claimed or shipped refuses supersede for
    # want of a claim. A baton that is (a) never formally claimed, (b) whose
    # work was nevertheless done, and (c) now has a genuine successor via
    # `predecessor:` therefore has NO automated route through EITHER verb
    # after this fix: supersede refuses for want of a claim; close-terminal
    # (this op) now correctly refuses too, instead of silently accepting and
    # stamping the wrong terminal as it did before. This is a real regression
    # in usability relative to the (incorrect) pre-fix behavior, flagged
    # explicitly rather than shipped silently — see this session's report.
    guard_result = await _handoff_has_live_children(
        {"candidate": str(contained_live), "exclude": exclude}, repo_root
    )
    guard_exit = guard_result.get("exit_code")
    if guard_exit != 1:
        if guard_exit == 0:
            child_rel_ids = [
                _wire_rel_id(Path(c), worktree) for c in (guard_result.get("children") or [])
            ]
            return _err(
                f"{rel_id} has a live lineage edge — named as predecessor by "
                f"{', '.join(child_rel_ids) or 'a live successor'}. closed_reason:"
                f"{reason!r} would be schema-forbidden when reason='displaced' "
                "(handoff.schema.json: 'displaced = replaced with NO lineage edge "
                "— with an edge it's continued, not closed'), and this op is "
                "scoped to the no-successor reconcile-to-terminal shape only. "
                "Route this baton through handoff.archive_transition "
                "mode='supersede' (or the handoff.transition 'supersede' verb) "
                "instead — deployment_state:continued + continued_into is the "
                "correct terminal for a baton with a live successor edge. NOTE: "
                "if this predecessor was never claimed or shipped, supersede's "
                "own DR-242 gate will ALSO refuse it — that shape has no "
                "automated route through either verb today; escalate rather than "
                "hand-editing frontmatter."
            )
        return _err(
            f"{rel_id}: live-lineage-edge guard indeterminate (exit_code "
            f"{guard_exit}) — refusing rather than risk stamping closed_reason:"
            f"{reason!r} over a lineage edge this guard could not rule out: "
            f"{guard_result.get('error', 'unknown guard error')}"
        )

    # --- Step 1: close (deployment_state -> closed; idempotent) ---
    #
    # P1 TOCTOU fix: the step-0 guard above ran unlocked, before any lock on
    # this handoff's own file was acquired — a successor naming this baton
    # as `predecessor:` could be created in the window between that read and
    # `_close`'s own `locked_rmw` acquisition, and step 1 would then stamp
    # closed_reason:displaced over what is now a live lineage edge (exactly
    # the schema-forbidden state step 0 exists to prevent). `_close` accepts
    # an optional `live_children_recheck` callback that it invokes INSIDE
    # its own locked_rmw mutate closure, immediately before building the
    # real write, so the guard is re-verified atomically with the write it
    # gates rather than merely before it. `asyncio.run` is safe here: this
    # closure only ever executes inside the `asyncio.to_thread(_close, ...)`
    # worker thread below, which has no running event loop of its own.
    def _live_children_recheck() -> dict:
        return asyncio.run(
            _handoff_has_live_children(
                {"candidate": str(contained_live), "exclude": exclude}, repo_root
            )
        )

    close_res = await asyncio.to_thread(
        _close, rel_id, reason, worktree, repo_root, _live_children_recheck
    )
    if close_res.get("exit_code") != 0:
        out = _err(f"close failed: {close_res.get('error', 'unknown error')}")
        return out
    already_closed = not bool(close_res.get("applied"))

    # --- Step 2: archive (chain mode; re-verifies terminality; git-mv + commit) ---
    #
    # restage_src=close_res["applied"]: step 1's `_close` wrote
    # deployment_state:closed + closed_reason + pickup_ready:false to disk
    # and left them UNCOMMITTED ONLY when it actually applied a write this
    # call (`applied` True) — this op is the composing caller that authored
    # that drift, and ONLY in that case. When `already_closed` is True,
    # `_close` was an idempotent no-op: this call authored no drift, and
    # whatever is dirty at src is not this call's to sweep — restaging then
    # would bypass the disk/HEAD drift guard for content a concurrent
    # session (or an unrelated later write) may own, exactly the failure
    # mode the guard exists to prevent (review: coordinatorcode-reviewer-
    # 2d69ff87.md P1). chain mode's own terminal-state precondition reads
    # fresh on-disk content, but `archive_and_commit`'s plain move re-keys
    # src's HEAD blob, so the disk/HEAD drift guard (commit 4541069c3)
    # refuses the move outright and a freshly-authored close is stranded in
    # state/handoffs/ without the opt-in. Restaging is the sanctioned route
    # for op-authored pre-move content (Move.restage_src, and
    # handoff_ship_archive's identical `_ship` + archive composition), and it
    # is what makes the archival commit carry the terminal state this call
    # just verified rather than the pre-close blob — but only when this call
    # is the one that wrote it.
    archive_params = {
        "handoff_path": rel_id,
        "mode": "chain",
        "restage_src": bool(close_res.get("applied")),
    }
    if exclude:
        archive_params["exclude"] = exclude
    archive_res = await _archive_transition_handler(archive_params, repo_root)

    if archive_res.get("exit_code") not in (0, None):
        out = _err(
            f"closed {rel_id} but archival failed: "
            f"{archive_res.get('error', 'unknown error')}"
        )
        out["closed"] = True
        out["already_closed"] = already_closed
        return out

    moved = bool(archive_res.get("moved"))
    retained = bool(archive_res.get("retained"))
    retain_reason = archive_res.get("retain_reason")

    if moved:
        message = f"closed and archived {rel_id} (reason: {reason})"
    elif retained:
        message = (
            f"closed {rel_id} (reason: {reason}); archival retained "
            f"({retain_reason or 'live-children guard'})"
        )
    else:
        # Carry the mover's own refusal text rather than restating the symptom.
        # archive_transition surfaces it on failed[]/warnings[]; dropping it here
        # is what made a drift-guard refusal read as an unexplained no-op.
        archive_failed = archive_res.get("failed") or []
        archive_reason = (
            archive_failed[0].get("reason")
            if archive_failed and isinstance(archive_failed[0], dict)
            else None
        )
        if archive_reason:
            message = f"closed {rel_id} (reason: {reason}); not archived: {archive_reason}"
        else:
            message = f"closed {rel_id} (reason: {reason}); archival did not move the file"

    return {
        "exit_code": 0,
        "closed": True,
        "already_closed": already_closed,
        "archived": moved,
        "already_archived": False,
        "retained": retained,
        "retain_reason": retain_reason,
        "message": message,
    }

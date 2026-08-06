"""
coordinator_core.ops.handoff_archive_transition — "handoff.archive_transition" op.

Purpose: single native-Python composition of example-doctrine-repo's bash orchestrator
coordinator-handoff-archive.sh — collapses the example-doctrine-repo bash -> node -> python hop
chain into ONE in-process op. Four modes, all operating on one handoff .md path:

  chain (default)  — UNCONDITIONAL live-children guard; if safe AND the
                      baton's on-disk deployment_state is already terminal
                      (shipped | continued | closed) -> git mv to
                      archive/handoffs/YYYY-MM/ + commit. NO stamp. A
                      non-terminal deployment_state is a REFUSAL (exit_code:1),
                      not a silent stamp and not a silent move — see
                      § Terminal-state precondition below.
  stamp_shipped    — stamp_shipped_in(allow_branch_tip_fallback=False)
                      BEFORE the guard, unconditionally (mirrors bash: the
                      stamp already lands even when the guard subsequently
                      decides to retain) -- then, once the guard clears,
                      deployment_state:shipped (ship verb) BEFORE the git mv +
                      commit, so the archived record carries shipped_in AND
                      deployment_state:shipped together (the terminal shipped
                      contract; the deployment_state flip lands only when the
                      handoff actually ships, never on a guard-retained one).
  stamp_only       — guard FIRST; if safe: stamp_shipped_in +
                      deployment_state:shipped (ship verb); NO git mv (the file
                      stays in state/handoffs/ for a later async archival sweep).
  supersede        — stamp_shipped_in, THEN the status flip (status:claimed +
                      deployment_state:continued + continued_into:<successor>,
                      the supersede verb, DR-084 split — see § Supersede-verb
                      split below), BOTH BEFORE the live-children guard (see
                      § Status-flip-precedes-guard fix below); only the git mv
                      itself waits on the guard clearing. The ONE mode whose
                      `handoff_path` may ALSO name an already-archived record
                      (archive/handoffs/, cross-repo/archive/,
                      archive/completed/) — see § Archived-predecessor
                      stamp-in-place below; every other mode stays live-only.

Live-children guard (handoff.has_live_children) is UNCONDITIONAL across all
four modes -- it runs exactly once per call, in the same structural position
bash runs it (after any --stamp-shipped/--supersede pre-guard stamp AND the
supersede status flip, before any --stamp-only post-guard mutation). Guard
exit 1 (safe) is the ONLY outcome that proceeds to the archival move; guard
exit 0 (has live children) or exit 2 (indeterminate/fail-closed) both retain
the handoff on disk and return a graceful (exit_code:0) skip -- retention is
NEVER an error, and (as of the fix below) never suppresses the supersede
status flip either, only the archival git-mv.

Status-flip-precedes-guard fix (2026-07-27, cross-repo example-doctrine-repo incident
"handoff-archive-transition supersede silently no-ops"): the supersede
mutation (status:claimed + deployment_state:continued + continued_into) used
to run AFTER the live-children guard's early-return, so a live child — which
in the normal `/handoff` flow is precisely the SUCCESSOR this call is meant
to name, the instant it exists on disk with a non-terminal deployment_state —
made the guard retain and skip the mutation entirely, on essentially every
real call. PM ruling: "as soon as a successor baton exists, the predecessor
is by definition no longer in flight" — a live claim holder (or a live child)
is irrelevant to that fact; it may legitimately still gate archival, but not
the status flip. The supersede mutation is now unconditional (mirrors how
the pre-guard stamp_shipped_in call for stamp_shipped/supersede has always
been unconditional), and the guard governs ONLY whether the git-mv into
archive/handoffs/ proceeds this same call.

**Scoping (2026-08-02, roadmap-baton-supersession-hazard plan, chunk C2;
re-keyed 2026-08-05, DR-126 § Clarifications C-1, plan
c2-supersede-gate-chaseable-terminus chunk C1):** the ruling above — the
flip is UNCONDITIONAL — is scoped to SESSION-HANDOFF succession, the
artifact class the 2026-07-27 incident concerned. It is NOT universal: for a
predecessor whose frontmatter `kind` canonicalizes to `roadmap-baton`, the
flip is refused outright — no automated supersede for a roadmap baton in
any state, whether or not it currently has a live `blocked_by` dependent
(DR-126 § Clarifications C-1; see § roadmap-baton blocked_by gate below).
Ordinary session-handoff succession is unaffected — the flip stays
unconditional for every predecessor whose `kind` is not `roadmap-baton`.

**roadmap-baton blocked_by gate (C2, PIN-2; re-keyed on `kind` alone by
DR-126 § Clarifications C-1, C1 2026-08-05):** placed in the same
`if mode == "supersede":` block as the DR-242 gate above, before `do_stamp`
is computed and before the status flip — gating the git-mv alone would do
nothing, since `gate_eval` reads `deployment_state`, never file location.
The refusal decision is `canonical_kind(_current_kind(contained)) ==
"roadmap-baton"` ALONE — DR-126's own reasoning (the dependent set is
authored incrementally; a `stub_id` outlives the baton file; `d6`
(`handoff.supersede_predecessor`) already enforces this rule kind-first,
with no dependents condition, on the other path). `blocked_by_dependents`
(PIN-1) is still composed at the same call site, but it now feeds the
refusal MESSAGE, not the decision: `"dependents"` (a live handoff lists the
predecessor's stub id in its own `blocked_by`) names the live dependent(s);
`"none"` (no live dependents today) states the refusal is on kind alone;
`"indeterminate"` (an unresolvable candidate identifier, or a non-empty
`scan_errors`) states the live-dependent list is unavailable because the
scan could not complete — a distinct, still-true FACT, but not a distinct
DECISION, since every arm refuses. None of the three arms is a retention
(`retained: True`) — see § Layering and the SEMANTICS note in the plan: a
retain here would reach `apply.py`'s
`_dispatch_handoff_supersede_predecessor`, which keys on
`result["superseded"]` and raises on `False` — by which point
`_cleanup_successor()` has already unlinked the freshly-minted successor and
d5's claim has already released, making a "graceful retain" a harder,
dirtier block than a refusal. A REPLAY of an already-successful
supersession (on-disk `deployment_state:continued` with `continued_into`
already equal to the requested successor) short-circuits past this gate
entirely, before it is evaluated, into the existing byte-identical no-op
path — the gate guards the TRANSITION into `continued`, not the steady
state. `exclude` threads through to `blocked_by_dependents` exactly as it
threads to `_handoff_has_live_children`, so a scaffolded successor
inheriting its predecessor's `blocked_by` list does not read itself as a
blocking dependent in the MESSAGE — `exclude` no longer affects the
decision at all, since a dependent-free roadmap baton is refused exactly
the same as one with dependents.

Reuse (no reimplementation of tested internals):
  - coordinator_core.archive_stamp.stamp_shipped_in / _run_git — imported
    function-local at each call site, not at module top-level: archive_stamp
    imports coordinator_core.ops.session_context, whose package import
    (coordinator_core.ops/__init__.py eager-import sweep) reaches this module
    in turn — a top-level back-edge here would deadlock the cycle on
    archive_stamp's own partially-initialized module.
  - coordinator_core.ops.handoff_transition._ship (authorized-writer RMW path,
    same locked_rmw + schema-validation gate as every other handoff frontmatter
    mutation). The supersede verb no longer delegates to
    handoff_transition._supersede — see § Supersede-verb split (DR-084) below;
    it reuses the SAME locked_rmw + validate_frontmatter primitives inline
    instead, since _supersede itself is off-limits to this chunk's rename and
    still speaks the retired consumed+abandoned vocabulary for its other
    caller (the frontmatter-only `handoff.transition supersede` verb, called
    directly, not through this op).
  - coordinator_core.ops.handoff_children._handoff_has_live_children (the guard)
  - coordinator_core.ops.fleet._common.handoff_archive_dest / archive_and_commit / Move

Terminal-state precondition (example-doctrine-repo, 2026-07-26, plan C7): the git-mv
block at the tail of this op (all modes that reach it — chain, stamp_shipped,
supersede; stamp_only never reaches it, it returns before the move) is now
gated on the CANDIDATE'S OWN on-disk deployment_state already being terminal
(one of "shipped" | "continued" | "closed") at the moment of the move. This
closes the defect that let mode="chain" git-mv a non-terminal baton into
archive/handoffs/ with no stamp of any kind: handoff_transition._resolve_path
refuses any path outside state/handoffs/, so a baton that lands in
archive/handoffs/ non-terminal can never afterwards be repaired by ANY
transition verb — it is corrupt by construction and permanently so.

The precondition is a REFUSAL, not an in-op stamp. Two shapes were possible
here — stamp a terminal state inside "chain" before the move, or refuse the
move on a non-terminal baton and fail loud, telling the caller which mode
stamps a terminal state instead. This op takes the second shape deliberately:
"chain"'s own docstring/contract has always been "NO stamp" (see mode
description above), and a caller relying on that documented no-stamp contract
would be silently surprised if this op started stamping deployment_state
under the hood -- trading one surprise (a corrupt archived baton) for another
(an undocumented-until-you-read-the-diff mutation) is not a fix, it's a
lateral move. Refusal preserves "chain never stamps" as an invariant a caller
can still rely on, and makes the actual fix (reach a terminal state first)
an explicit, visible, caller-chosen step via mode="stamp_shipped" (->
shipped), mode="supersede" (-> continued), or a direct
handoff.transition close call (-> closed) — never a guess this op makes on
the caller's behalf.

The check runs AFTER any do_stamp/do_supersede mutation in this same call (so
a stamp_shipped/supersede call that just wrote a terminal state on THIS call
sees its own fresh write and proceeds) and applies to every mode that reaches
the git-mv block, including any mode added in the future -- it is not an
"if mode == 'chain'" special case, so a new mode that forgets to stamp a
terminal state before falling through to the move is caught by construction,
not by remembering to re-add this check at each new mode's call site.

Supersede-verb split (DR-084, plan C5): the old status:consumed +
deployment_state:abandoned expression RETIRES from this op. mode="supersede"
now REQUIRES the caller to supply `continued_into` (the successor handoff's
id-or-path) as positive succession proof; on the guard clearing, this op
stamps status:claimed + deployment_state:continued + continued_into:<value>.
A supersede call with no `continued_into` is a usage error (exit_code:2) — an
automated writer that cannot name the successor cannot stamp `continued` by
construction (the same anti-loophole tooth as the schema's
`_cf_continued_into_required` cross-field rule). This op NEVER stamps
deployment_state:closed — that is a human/session-only decision under
DR-084's no-automated-abandonment ruling, full stop.

Archived-predecessor stamp-in-place (2026-07-28, d6-archived-predecessor
fix): the normal `/handoff` call shape archives the PREDECESSOR via the
session boot sweep before d6 (this op's mode="supersede" caller) ever runs —
by the time this op is invoked, `handoff_path` routinely names a path already
under `archive/handoffs/YYYY-MM/` (or, less commonly, `cross-repo/archive/` /
`archive/completed/` — the same three roots resolve_swept_baton.py's own
`ARCHIVE_ROOT_SUBDIRS` search order covers). Before this fix, that was a hard
usage-error refusal ("handoff_path escapes state/handoffs/"), so d6 was a
structural no-op on the common path: the successor correctly named its
predecessor, but the predecessor was never actually stamped `continued` and
was left non-terminal forever.

The fix, scoped to mode="supersede" only (chain/stamp_shipped/stamp_only stay
strictly live-only — a chain/stamp_shipped/stamp_only call against an archived
path is unchanged, still a usage-error refusal):
  - The containment allowlist widens to admit `worktree / <ARCHIVE_ROOT_SUBDIRS
    entry>` in addition to `state/handoffs/`, reusing the SAME constant
    resolve_swept_baton.py's own archive search already defines (lifted to
    `coordinator_core.ops.fleet._common.ARCHIVE_ROOT_SUBDIRS` so there is
    exactly one archive-dir list in this codebase, not two that can drift).
  - When the resolved `handoff_path` is under one of those archive roots
    (not under `state/handoffs/`), the status flip (status:claimed +
    deployment_state:continued + continued_into:<successor>) still runs
    exactly as it does for a live predecessor — it is a plain frontmatter
    RMW, indifferent to which directory the file lives in — but this op
    returns IMMEDIATELY after that flip succeeds, BEFORE the live-children
    guard call and BEFORE the terminal-state-precondition/git-mv block. An
    already-archived record has nothing left to move: there is no git-mv to
    perform (it is already at its archive destination) and no "is it safe to
    move" question to ask (nothing is being moved). `retained`/`moved` both
    report False in this shape — not because anything was retained, but
    because there was never a move to retain or complete.
  - Idempotency and conflict detection (`_supersede_continued`): a re-stamp
    with the SAME `continued_into` the record already carries is a clean
    no-op (unchanged from the live-predecessor case). A re-stamp naming a
    DIFFERENT `continued_into` than the one already on disk is a genuine
    conflict, not a silent overwrite — it raises MutateAbort naming both
    values, since blindly overwriting would erase a real, already-recorded
    succession edge in favor of a second one.

Negative-spec: this widening does NOT make mode="supersede" tolerate an
arbitrary path — `contained_path`'s post-`.resolve()` containment check still
fails closed for anything outside `state/handoffs/` ∪ ARCHIVE_ROOT_SUBDIRS,
and every OTHER mode's allowlist is completely unchanged (still
`state/handoffs/` only).

Port of: coordinator-handoff-archive.sh (example-doctrine-repo c47b0268, 2026-07-19).
Spec: cross-repo example-doctrine-repo 7-bug route item 4 (this op). DR-059 (engine-tier bash
bugs route to claude-klabauter).

--- Position A: no branch-tip fallback, no Session-Id trailer-correction walk ---

The example-doctrine-repo bash oracle (and this op's earlier faithful port) stamped shipped_in
via stamp_shipped_in(allow_branch_tip_fallback=True): when the handoff's
scope: paths resolved to no commit, it fell back to guessing the current
branch tip. On a shared work/* branch, that guess can land a SIBLING
session's commit (item-7's defect class — see the now-closed backlog entry
state/bug-backlog/2026-07-14-handoff-archive-stamp-only-stamps-sibling-sha.yaml,
status: closed-not-reproducible). The prior port then added a Session-Id
trailer verify/correction walk purely to detect and fix that self-inflicted
sibling-SHA hazard.

Claude-klabauter's PM-ratified architecture (Position A, 2026-07-15) eliminates the
hazard at the source instead of patching around it: this op NEVER falls back
to a branch tip that could belong to a sibling session.
stamp_shipped_in(allow_branch_tip_fallback=False) resolves shipped_in ONLY
from `git log -n1 -- <scope: paths>` — this session's own commit that
touched the handoff's declared scope. If scope resolution finds no SHA,
stamp_shipped_in is a 0/no-op and shipped_in is left UNSET; that honest
"unresolved" is the intended Position-A outcome, not an error, and this op
surfaces a short warning in the result so it stays observable. Because there
is no branch-tip guess, there is nothing to verify or correct after the
fact — the entire Session-Id trailer-correction machinery (compare-and-
replace, the 20-commit trailer walk, the idempotency dance) is gone; it
existed only to fix a hazard this architecture no longer creates.

Negative-spec:
  - Does NOT fall back to a branch-tip guess under any mode — stamp_shipped_in
    is always called with allow_branch_tip_fallback=False (or the bare
    default, which is False) from every call site in this module.
  - Does NOT reimplement handoff.stamp/handoff.transition frontmatter-mutation
    LOGIC -- shipped_in is written exclusively via stamp_shipped_in's own
    handoff.stamp op call.
  - Does NOT change handoff_ship_archive.py's behavior or scope -- that op
    remains the event-driven ship+archive composite for the /workstream-complete
    call site; this op is the faithful port of the example-doctrine-repo archive-ceremony CLI
    for /handoff Step 1 and callers that need the 4-mode flag surface
    (stamp_shipped / stamp_only / supersede / chain) and the unconditional
    live-children guard in one call.
  - Does NOT batch -- exactly one handoff per call, mirroring the bash CLI's
    single positional handoff-path argument.
  - Does NOT change Position A's own resolution semantics -- the optional
    `sha`/`force` params (added 2026-07-22) are a provenance-repair escape for
    a caller that has ALREADY independently derived the correct sha (the same
    contract as stamp_shipped_in's own `sha=` override); they do not add a
    new resolution path or a branch-tip fallback of any kind. See incident:
    state/bug-backlog/2026-07-14-handoff-archive-stamp-only-stamps-sibling-sha.yaml
    (reopened 2026-07-22 — the hazard reproduced via a shared-scope-path race,
    one layer above where the earlier closure looked).

Self-registration: importing this module fires
register_op("handoff.archive_transition"). Add to coordinator_core/ops/__init__.py
and register its scope ("common_dir") in ipc.py::_OP_KEY_SCOPE.
"""

from __future__ import annotations
import sys

import asyncio
import logging
from pathlib import Path
from typing import List, Optional

import yaml

from coordinator_core.frontmatter.baton_class import canonical_kind
from coordinator_core.frontmatter.primitives import (
    insert_fm_field,
    read_fm_field,
    read_fm_field_unquoted,
    rebuild,
    replace_fm_field,
    split_frontmatter,
)
from coordinator_core.frontmatter.schema_validate import (
    format_validation_errors,
    validate_frontmatter,
)
from coordinator_core.ipc import register_op
from coordinator_core.locked_write import LockTimeout, MutateAbort, locked_rmw
from coordinator_core.ops._path_guard import contained_path
from coordinator_core.ops.fleet._common import (
    ARCHIVE_ROOT_SUBDIRS,
    Move,
    archive_and_commit,
    handoff_archive_dest,
    main_worktree_root,
)
from coordinator_core.ops.handoff_children import (
    _handoff_has_live_children,
    blocked_by_dependents,
)
from coordinator_core.ops.handoff_transition import _ship
# Aliased: `rel_id` is also a local variable name in _handler below, and an
# unaliased import would be shadowed by that binding (UnboundLocalError).
from coordinator_core.wire_paths import rel_id as _wire_rel_id

_LOG = logging.getLogger(__name__)

_VALID_MODES = frozenset({"chain", "stamp_shipped", "stamp_only", "supersede"})

# Terminal-state precondition (see module docstring § Terminal-state
# precondition) — the git-mv block requires the candidate's on-disk
# deployment_state to already be one of these before it moves the baton.
# Mirrors the handoff schema's own deployment_state enum tail
# (frontmatter/schemas/handoff.schema.json): "shipped" | "continued" |
# "closed" are the three lifecycle terminals; "awaiting_gate" |
# "ready_to_fire" | "in_flight" are not.
_TERMINAL_DEPLOYMENT_STATES = frozenset({"shipped", "continued", "closed"})

# Vendored handoff schema — same path handoff_transition.py resolves,
# duplicated here (not imported) since the supersede-verb-split mutation is
# inline in this module (see module docstring § Supersede-verb split).
_SCHEMA_PATH: Path = (
    Path(__file__).parent.parent / "frontmatter" / "schemas" / "handoff.schema.json"
)


# ---------------------------------------------------------------------------
# Reply helpers
# ---------------------------------------------------------------------------


def _err(msg: str) -> dict:
    """Return an exit_code=1 setup/transition-error envelope.

    AC11: key set is uniform with `_usage_error` and the retain-path envelope
    (both carry `retain_kind`/`message` — always None here, since an error
    envelope never retains anything and its "message" is `error`).
    """
    _LOG.warning("handoff.archive_transition: %s", msg)
    return {
        "exit_code": 1,
        "mode": None,
        "stamped": False,
        "superseded": False,
        "retained": False,
        "retain_reason": None,
        "retain_kind": None,
        "moved": False,
        "warnings": [],
        "error": msg,
        "message": None,
    }


def _usage_error(msg: str) -> dict:
    """Return an exit_code=2 usage-error envelope (mutually-exclusive/invalid params).

    AC11: key set is uniform with `_err` and the retain-path envelope — see
    `_err`'s docstring.
    """
    _LOG.warning("handoff.archive_transition: usage error: %s", msg)
    return {
        "exit_code": 2,
        "mode": None,
        "stamped": False,
        "superseded": False,
        "retained": False,
        "retain_reason": None,
        "retain_kind": None,
        "moved": False,
        "warnings": [],
        "error": msg,
        "message": None,
    }


# ---------------------------------------------------------------------------
# Position A: shipped_in observability — no branch-tip fallback means a
# scope-resolution miss must be surfaced, not silently masked as "stamped".
# ---------------------------------------------------------------------------


def _current_fm_field(handoff_abs: Path, field: str) -> Optional[str]:
    """Read a single frontmatter field's current on-disk value, or None.

    Review: code-reviewer (P2, Finding 3) — extracted from four byte-identical
    read/split/unquoted-read helpers (`_current_shipped_in`,
    `_current_deployment_state`, `_current_kind`, `_current_continued_into`)
    that differed only in the field name and a debug string; kept as named
    thin wrappers below so call sites and their docstrings stay unchanged.
    """
    try:
        text = handoff_abs.read_text(encoding="utf-8")
    except OSError:
        print(f"skip: _current_fm_field({field!r}): text = handoff_abs.read_text(encoding=\"utf-8\") failed: {sys.exc_info()[1]}", file=sys.stderr)
        return None
    split = split_frontmatter(text)
    if split is None:
        return None
    # Unquoted read: stamp_shipped_in writes with numeric_quoting=True, so a
    # numeric-looking sha8 lands single-quoted. The raw form would leak quotes
    # into the surfaced value and defeat the "null"/"" sentinel test.
    val = read_fm_field_unquoted(split.fm_text, field)
    return val if val not in (None, "null", "") else None


def _current_shipped_in(handoff_abs: Path) -> Optional[str]:
    """Read the current shipped_in: value from a handoff's frontmatter, or None.

    Used before/after a stamp_shipped_in call to distinguish "actually wrote
    a scope-resolved SHA" from stamp_shipped_in's own 0/no-op-on-unresolved
    return code (both return rc 0 — see archive_stamp.stamp_shipped_in
    docstring), so this op can surface the Position-A "unresolved" case as an
    observable warning instead of a false stamped:True.
    """
    return _current_fm_field(handoff_abs, "shipped_in")


def _current_deployment_state(handoff_abs: Path) -> Optional[str]:
    """Read the current deployment_state: value from a handoff's frontmatter, or None.

    Used by the terminal-state precondition immediately before the git-mv
    block (see module docstring § Terminal-state precondition) — reads
    AFTER any do_stamp/do_supersede mutation earlier in the same call, so a
    stamp_shipped/supersede call sees its own fresh write.
    """
    return _current_fm_field(handoff_abs, "deployment_state")


def _current_kind(handoff_abs: Path) -> Optional[str]:
    """Read the current kind: value from a handoff's frontmatter, or None.

    Used by the C2 roadmap-baton blocked_by gate (module docstring §
    roadmap-baton blocked_by gate) to scope the new pre-flip gate to
    `kind: roadmap-baton` only — ordinary session-handoff succession stays
    on the 2026-07-27 unconditional-flip path (AC2).
    """
    return _current_fm_field(handoff_abs, "kind")


def _current_continued_into(handoff_abs: Path) -> Optional[str]:
    """Read the current continued_into: value from a handoff's frontmatter, or None.

    Used by the C2 replay-convergence short-circuit (module docstring §
    roadmap-baton blocked_by gate) to detect an already-converged
    supersession before the new gate is ever evaluated.
    """
    return _current_fm_field(handoff_abs, "continued_into")


def _sha_canonically_matches(supplied: str, prior_value: str) -> bool:
    """True when `supplied` (a caller-supplied SHA, any length 7-64 hex) and
    `prior_value` (the stored `shipped_in`, `_final_stamp_value`-truncated to
    8 chars) name the SAME commit — a case-insensitive prefix comparison, not
    a string-equality diff. The expected shape is `supplied` full-length and
    `prior_value` the 8-char truncated storage form, so `prior_value` must be
    a prefix of `supplied`; neither being a prefix of the other means they
    are provably different commits.

    Review: code-reviewer (nit F3) — `supplied` shorter than `prior_value` is
    refused outright rather than prefix-matched. Two distinct commits can
    share a 7-char prefix (git only guarantees short-SHA uniqueness at
    generation time, not permanently as a repo grows), so an
    abbreviation-vs-abbreviation prefix match risked declaring a false match.
    Refusing is the AC6-safe default: worst case is a legitimate re-stamp
    treated as a fresh write, never a wrong commit accepted as canonical.

    § S11/AC6b (`docs/plans/2026-07-28-handoff-close-path-fail-loud.md`,
    chunk C0): this is the ONLY correct way to compare an already-present
    `shipped_in` against a caller's `--sha` — a bare `==` string diff false-
    refuses every legitimate same-commit re-stamp, because `shipped_in` is
    stored 8-char-truncated while every caller supplies a full-length SHA.
    """
    a, b = supplied.strip().lower(), prior_value.strip().lower()
    if not a or not b:
        return False
    if len(a) < len(b):
        # `supplied` shorter than the stored value: refuse as non-matching
        # rather than prefix-matching two abbreviations against each other.
        return False
    return a.startswith(b)


# ---------------------------------------------------------------------------
# Supersede-verb split (DR-084, plan C5) — status:claimed +
# deployment_state:continued + continued_into:<successor>. Inline mutation
# (not delegated to handoff_transition._supersede — see module docstring)
# reusing the same locked_rmw + validate_frontmatter primitives that op uses.
# ---------------------------------------------------------------------------


def _supersede_continued(
    handoff_abs: Path, continued_into: str, repo_root: Path
) -> dict:
    """Apply the supersede-verb split: status->claimed, deployment_state->continued,
    continued_into->the caller-supplied successor id-or-path.

    Idempotency: no-op when status==claimed AND deployment_state==continued AND
    continued_into already equals the requested successor.

    Conflict (2026-07-28, d6-archived-predecessor fix): status==claimed AND
    deployment_state==continued AND continued_into ALREADY SET to a DIFFERENT
    value is not treated as "just write the new one" — that would silently
    erase a real, already-recorded succession edge in favor of a second one
    this call happens to be asking for. Raises MutateAbort naming both values
    instead; the caller (a fresh mint racing an already-superseded
    predecessor, or a caller with a stale successor id) needs to know it hit
    a real conflict, not get a quiet overwrite.

    Routes the read-modify-write through locked_rmw for cross-process
    serialisation. Domain-abort paths raise MutateAbort from inside the mutate
    closure so no write occurs.
    """
    _state: dict = {"applied": False, "message": ""}

    def mutate(old_text: str) -> str:
        split = split_frontmatter(old_text)
        if split is None:
            raise MutateAbort(f"supersede: no parseable YAML frontmatter in {handoff_abs}")

        status = read_fm_field(split.fm_text, "status")
        deployment = read_fm_field(split.fm_text, "deployment_state")
        existing_continued_into = read_fm_field_unquoted(split.fm_text, "continued_into")

        if status == "claimed" and deployment == "continued" and existing_continued_into:
            if existing_continued_into == continued_into:
                _state["message"] = (
                    f"{handoff_abs} already claimed+continued into {continued_into} — no-op"
                )
                return old_text  # byte-identical → locked_rmw skips the write
            raise MutateAbort(
                f"supersede conflict: {handoff_abs} is already deployment_state:"
                f"continued with continued_into={existing_continued_into!r}, but "
                f"this call requested continued_into={continued_into!r} — refusing "
                "to silently overwrite one real succession edge with a different "
                "one; resolve the conflict by hand before retrying"
            )

        fm = split.fm_text

        # status → claimed (replace existing; insert after 'title' if missing).
        if status != "claimed":
            if status is None:
                fm = insert_fm_field(fm, "status", "claimed", "title")
            else:
                fm = replace_fm_field(fm, "status", "claimed")

        # deployment_state → continued (replace existing; insert after 'status' if missing).
        if deployment != "continued":
            if deployment is None:
                fm = insert_fm_field(fm, "deployment_state", "continued", "status")
            else:
                fm = replace_fm_field(fm, "deployment_state", "continued")

        # continued_into → the successor (replace existing; insert after
        # 'deployment_state' if missing).
        if read_fm_field(fm, "continued_into") is not None:
            fm = replace_fm_field(fm, "continued_into", continued_into)
        else:
            fm = insert_fm_field(fm, "continued_into", continued_into, "deployment_state")

        # Post-mutation schema validation gate — raise MutateAbort to skip the write.
        try:
            fm_dict = yaml.safe_load(fm) or {}
        except Exception as exc:  # noqa: BLE001
            raise MutateAbort(f"handoff frontmatter YAML parse error: {exc}") from exc
        errors = validate_frontmatter(fm_dict, _SCHEMA_PATH)
        if errors:
            details = format_validation_errors(errors)
            raise MutateAbort(f"handoff frontmatter validation failed: {details}")

        _state["applied"] = True
        _state["message"] = (
            f"superseded {handoff_abs} (status: claimed, deployment_state: "
            f"continued, continued_into: {continued_into})"
        )
        return rebuild(split, fm)

    try:
        locked_rmw(handoff_abs, mutate, repo_root=repo_root)
    except FileNotFoundError:
        return {"exit_code": 1, "error": f"supersede: handoff not found: {handoff_abs}"}
    except LockTimeout as exc:
        return {
            "exit_code": 1,
            "error": f"supersede: timed out waiting for file lock on {handoff_abs}: {exc}",
        }
    except MutateAbort as exc:
        return {
            "exit_code": 1,
            "error": exc.args[0] if exc.args else "supersede: mutation aborted",
        }

    return {"exit_code": 0, "applied": _state["applied"], "message": _state["message"]}


# ---------------------------------------------------------------------------
# Op handler
# ---------------------------------------------------------------------------


@register_op("handoff.archive_transition")
async def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "handoff.archive_transition" — the 4-mode handoff archive ceremony.

    Faithful native-Python port (Port of: coordinator-handoff-archive.sh, example-doctrine-repo
    c47b0268, 2026-07-19). See module docstring for the full mode/order
    contract and the Position-A no-branch-tip-fallback rationale.

    Params:
        handoff_path (str, required) — absolute or repo-relative path; must
                     resolve under <worktree>/state/handoffs/ (mutation verbs
                     are live-only, same containment as handoff.stamp/transition)
                     for every mode EXCEPT "supersede", which additionally
                     admits an already-archived path under one of
                     ARCHIVE_ROOT_SUBDIRS (archive/handoffs/, cross-repo/archive/,
                     archive/completed/) — see module docstring § Archived-
                     predecessor stamp-in-place.
        mode         (str, optional) — one of "chain" (default) | "stamp_shipped"
                     | "stamp_only" | "supersede". Any other value is a usage
                     error (exit_code:2) — mirrors the bash mutual-exclusion
                     check (--stamp-only combined with --stamp-shipped/--supersede
                     collapses to an invalid single-mode selection here, since
                     mode is a one-of-four enum rather than independent flags).
        exclude      (list[str], optional) — paths dropped from the live-children
                     guard's scan set before checking (mirrors --exclude).
        continued_into (str, required when mode="supersede") — the successor
                     handoff's id-or-path (positive succession proof). Missing
                     or empty is a usage error (exit_code:2) — DR-084 retires
                     the consumed+abandoned expression, so an automated writer
                     that cannot name the successor cannot supersede at all.
                     Ignored for every other mode.
        sha          (str, optional) — caller-supplied SHA override, threaded
                     verbatim to stamp_shipped_in's own `sha=` override on
                     every do_stamp call site (stamp_shipped/supersede AND
                     stamp_only). Ignored when mode="chain" (no stamp call).
        force        (bool, optional) — provenance-repair escape, threaded
                     verbatim to stamp_shipped_in's own `force=` param.
                     REQUIRES `sha` — see stamp_shipped_in's Negative-spec;
                     an unpaired force is rejected fail-loud (exit_code:2)
                     before any stamp call. Added 2026-07-22.
        kind         (str, optional) — explicit `shipped_in_kind` (DR-096)
                     override, REQUIRES `sha` (an explicit kind only makes
                     sense paired with the sha it describes — mirrors
                     `stamp_shipped_in`'s own "'ship-commit'/'successor' ...
                     require a hex sha override" rule). Must be one of
                     'ship-commit' | 'successor' — 'no-commit' and
                     'scope-derived' have no call shape here (the former
                     needs the sanctioned no-commit token, not a sha; the
                     latter is this op's own no-sha default, below). Omitted
                     entirely, `kind` defaults to 'ship-commit' when `sha` is
                     supplied and 'scope-derived' (DR-096 legacy self-
                     derivation) when it is not — unchanged 2026-07-26
                     default. Added 2026-07-26 (scope-derived-retirement
                     audit, Change 2) so a caller that already has a
                     successor's sha in hand — e.g. `/update-docs` Phase 8's
                     lineage-predecessor archival — can tag the write
                     `kind="successor"` explicitly instead of silently
                     falling into `scope-derived`.
        successor_path (str, optional) — the SUCCESSOR handoff's own path
                     (absolute or repo-relative), for a caller that knows
                     which successor triggered this archival but does not
                     want to resolve that successor's own sha itself (e.g.
                     `/update-docs` Phase 8's lineage-predecessor archival:
                     the successor is "the handoff whose `predecessor:`/
                     `Continuing from` reference triggered this call", not
                     the handoff being archived — a distinct path from
                     `handoff_path`, never the same file). Named
                     `successor_path`, not `sha`/`scope`, because it names a
                     FILE the op resolves internally, never a sha the caller
                     already holds — that is what `sha`/`kind` are for
                     (mutually exclusive with both; supplying `successor_path`
                     alongside `sha` or `kind` is a usage error, since it is
                     ambiguous which sha wins). Distinct from `exclude`
                     (which drops paths from the live-children guard's live-
                     child scan — a different concept; passing the same path
                     to both `exclude` and `successor_path` is fine and
                     expected, since the successor is usually excluded from
                     its own predecessor's guard scan too).

                     Resolution: `git log --format=%H -n1 -- <successor_path>`
                     against this call's worktree (the same primitive
                     `stamp_shipped_in`'s own scope-path resolution uses,
                     `archive_stamp._resolve_scope_sha`, reused rather than
                     reimplemented). A resolved sha is threaded through as an
                     explicit `sha=` override with `kind="successor"` (DR-096)
                     — the caller-supplied-override path, so the ownership
                     guard (which applies only to a scope/branch-tip DERIVED
                     sha) does not apply here; resolving the successor's own
                     commit is exactly the assertion-of-ownership an explicit
                     override represents. When resolution finds no commit
                     (the successor is not yet committed to this worktree),
                     that is a real, honest state, not an error: the call
                     falls back to this op's existing no-sha `scope-derived`
                     default and surfaces the fallback via `warnings`, on the
                     same channel as the "resolved no commit"/"scope-derived
                     selected" warnings above — never a second, bespoke
                     warning convention. Ignored for mode="chain" (no stamp
                     call to feed).

    Returns:
        exit_code (int)         — 0 ok (incl. graceful retain-skip); 1 setup or
                                  mutation-transition error; 2 usage error
                                  (invalid mode).
        mode (str)               — echoed mode.
        stamped (bool)           — a stamp_shipped_in call ran AND actually
                                   wrote a scope-resolved shipped_in SHA this
                                   call (stamp_shipped/stamp_only/supersede
                                   modes only). Position A: False when scope
                                   resolution found no commit — that case is
                                   an honest no-op, not an error, and is
                                   surfaced via `warnings` instead.
        superseded (bool)        — the supersede verb (status:claimed +
                                   deployment_state:continued +
                                   continued_into:<successor>) applied. Set
                                   BEFORE the live-children guard runs (2026-
                                   07-27 fix) — independent of `retained`; a
                                   call can be `superseded: True, retained:
                                   True` (status flipped, archival deferred).
        retained (bool)          — the live-children guard said retain (has
                                   live children, or indeterminate/fail-closed)
                                   — governs ONLY the archival git-mv, never
                                   the supersede status flip above.
        retain_reason (str|None) — human-readable reason when retained.
        retain_kind (str|None)   — populated only when `retained` is True;
                                   "live-parent" (deliberate: still a live
                                   merge-parent) or "indeterminate" (guard
                                   fail-closed / degraded state).
        moved (bool)             — the handoff was git-mv'd + committed into
                                   archive/handoffs/YYYY-MM/.
        warnings (list[str])     — non-fatal warnings (shipped_in_kind
                                   selected as scope-derived — the legacy,
                                   no-`--sha` path, DR-096; stamp transport
                                   failure; scope resolved to no commit —
                                   shipped_in left unset; git-mv concurrent-
                                   move failure).
        message (str)            — human-readable outcome summary.

    P9 WORKTREE DERIVATION: repo_root arrives as the git common dir
    (<worktree>/.git); main_worktree_root(repo_root) derives the worktree.
    """
    from coordinator_core.archive_stamp import stamp_shipped_in

    handoff_path_raw: str = (params.get("handoff_path") or "").strip()
    mode: str = (params.get("mode") or "chain").strip()
    exclude: List[str] = params.get("exclude") or []
    continued_into: str = (params.get("continued_into") or "").strip()
    stamp_sha: Optional[str] = params.get("sha") or None
    stamp_force: bool = bool(params.get("force", False))
    _kind_raw = params.get("kind")
    stamp_kind_override: Optional[str] = (
        _kind_raw.strip() if isinstance(_kind_raw, str) and _kind_raw.strip() else None
    )
    successor_path_raw: str = (params.get("successor_path") or "").strip()
    # DR-096 (2026-07-26): stamp_shipped_in's `kind` param is now required at
    # the choke point, with no default. This module never invents its own
    # scope: paths — a caller-supplied `stamp_sha` (params["sha"]) is, by
    # DEFAULT, the "caller already has a specific ship commit in hand" case
    # (`kind="ship-commit"`); its absence is always the self-derivation path
    # (`kind="scope-derived"`, scope-path or — where allowed —
    # `allow_branch_tip_fallback`'s branch-tip resolution). A caller may
    # override that default to `kind="successor"` (2026-07-26, scope-derived-
    # retirement audit Change 2) when the sha it supplies belongs to a
    # successor rather than the handoff's own ship — see the `kind` param
    # doc above. Validated below; `no-commit`/`scope-derived` have no
    # explicit-override call shape here.
    if stamp_kind_override is not None:
        if not (stamp_sha and stamp_sha.strip()):
            return _usage_error(
                "'kind' requires 'sha' — an explicit kind override only makes "
                "sense paired with the sha it describes (mirrors "
                "archive_stamp.stamp_shipped_in's own kind/sha cross-validation)"
            )
        if stamp_kind_override not in ("ship-commit", "successor"):
            return _usage_error(
                f"unsupported kind override {stamp_kind_override!r} for this op "
                "— must be 'ship-commit' or 'successor' when paired with an "
                "explicit sha ('no-commit' and 'scope-derived' have no "
                "explicit-override call shape here)"
            )
        stamp_kind: str = stamp_kind_override
    else:
        stamp_kind = "ship-commit" if (stamp_sha and stamp_sha.strip()) else "scope-derived"

    if successor_path_raw and (stamp_sha or stamp_kind_override):
        return _usage_error(
            "'successor_path' is mutually exclusive with 'sha'/'kind' — "
            "supply the successor's own path and let this op resolve its "
            "sha internally, or supply 'sha'/'kind' directly with a sha "
            "already in hand, not both (ambiguous which sha would win)"
        )

    if mode not in _VALID_MODES:
        return _usage_error(
            f"unknown mode {mode!r} — must be one of {sorted(_VALID_MODES)} "
            "(stamp_only is mutually exclusive with stamp_shipped/supersede; "
            "there is no combined-flag shape here, mode is a one-of-four enum)"
        )

    if mode == "supersede" and not continued_into:
        return _usage_error(
            "mode 'supersede' requires 'continued_into' (successor handoff "
            "id-or-path) — DR-084 retires the consumed+abandoned expression; "
            "an automated writer may only stamp deployment_state:continued on "
            "positive succession proof"
        )

    if stamp_force and not (stamp_sha and stamp_sha.strip()):
        return _usage_error(
            "'force' requires 'sha' — force must never trigger its own "
            "resolution (see archive_stamp.stamp_shipped_in's Negative-spec)"
        )

    if not handoff_path_raw:
        out = _err("'handoff_path' is required")
        out["mode"] = mode
        return out
    if repo_root is None:
        out = _err(
            "repo_root is required (handler called without socket-authoritative common_dir)"
        )
        out["mode"] = mode
        return out

    worktree = main_worktree_root(repo_root)

    p = Path(handoff_path_raw)
    if not p.is_absolute():
        p = worktree / p

    # Containment allowlist — live-only for every mode except supersede (see
    # module docstring § Archived-predecessor stamp-in-place). Widening this
    # for mode="supersede" only: a `/handoff` boot sweep routinely archives
    # the predecessor before d6 ever runs, so `handoff_path` legitimately
    # names an archive/ path on the normal call shape, not an edge case.
    # ARCHIVE_ROOT_SUBDIRS is the SAME list resolve_swept_baton.py's own
    # archive search uses (lifted to _common.py so there is exactly one copy)
    # — reusing it here keeps "where an archived handoff can live" a single
    # fact instead of two lists that can drift apart.
    _live_root = worktree / "state" / "handoffs"
    allowed_roots = [_live_root]
    if mode == "supersede":
        allowed_roots += [worktree / sub for sub in ARCHIVE_ROOT_SUBDIRS]
    contained = contained_path(p, allowed_roots)
    if contained is None:
        if mode == "supersede":
            out = _usage_error(
                "handoff_path escapes state/handoffs/ and every known archive "
                f"dir ({', '.join(ARCHIVE_ROOT_SUBDIRS)}): {handoff_path_raw!r}"
            )
        else:
            out = _usage_error(f"handoff_path escapes state/handoffs/: {handoff_path_raw!r}")
        out["mode"] = mode
        return out

    if not contained.is_file():
        out = _err(f"handoff not found on disk: {handoff_path_raw}")
        out["mode"] = mode
        return out

    # True only for mode="supersede" against a candidate that already lives
    # under one of ARCHIVE_ROOT_SUBDIRS (not state/handoffs/) — the
    # already-archived-predecessor case. Every other mode's `allowed_roots`
    # above never admits a non-live path in the first place, so this is
    # always False for chain/stamp_shipped/stamp_only by construction.
    is_archived_target = mode == "supersede" and (
        contained_path(contained, [_live_root]) is None
    )

    rel_id = _wire_rel_id(contained, worktree)

    # Review: code-reviewer (Finding 1, C5 slice) — DR-242 gate moved to this
    # op choke point. This op is reachable directly via
    # `coordinator_core.invoke handoff.archive_transition`, which bypasses
    # every wrapper-level claimed_or_shipped_at_path check
    # (cs_supersede_archive_handoff, apply.py's
    # _dispatch_handoff_supersede_predecessor, the CLI's cmd_supersede — none
    # of them is a load-bearing choke point). Runs BEFORE do_stamp so a
    # refused supersede never partially stamps shipped_in first; gating here
    # makes the loose discriminator actually unavailable (AC8), not merely
    # forbidden at three caller sites — the wrapper-level checks remain as
    # defense in depth.
    if mode == "supersede":
        from coordinator_core.archival import claimed_or_shipped_at_path

        if not claimed_or_shipped_at_path(str(contained)):
            out = _err(
                f"mode='supersede' refused: {rel_id} was never claimed or shipped "
                "(DR-242: a successor-named child is not evidence of succession; "
                "nothing to supersede)"
            )
            out["mode"] = mode
            return out

        # ------------------------------------------------------------------
        # C2 — roadmap-baton blocked_by gate (docs/plans/2026-08-02-roadmap-
        # baton-supersession-hazard.md, PIN-2; re-keyed 2026-08-05, DR-126 §
        # Clarifications C-1, plan c2-supersede-gate-chaseable-terminus
        # chunk C1). See module docstring § roadmap-baton blocked_by gate
        # for the full design rationale. Scoped to kind: roadmap-baton only
        # — the 2026-07-27 unconditional-flip ruling stays in force for
        # ordinary session-handoff succession (AC3c). Runs BEFORE
        # `do_stamp`/`do_supersede` are computed and BEFORE the status flip
        # below — gating the git-mv alone would do nothing (gate_eval reads
        # deployment_state, not file location).
        #
        # DR-126 § Clarifications C-1: the refusal DECISION is
        # `canonical_kind(...) == "roadmap-baton"` ALONE — no automated
        # supersede for a roadmap baton in any state, whether or not it
        # currently has a live blocked_by dependent (the dependent set is
        # authored incrementally, and `d6` already enforces this rule
        # kind-first with no dependents condition). `blocked_by_dependents`
        # (PIN-1) stays composed at this same call site — its call site does
        # not move — but it now feeds the refusal MESSAGE, not the decision.
        # ------------------------------------------------------------------
        # Review: code-reviewer (P1, Finding 1) — canonicalize before comparing:
        # a raw string compare bypassed both case/whitespace variants and the
        # pre-rename alias (kind: spinoff-roadmap -> roadmap-baton), matching
        # canonical_kind()'s two normalization steps every sibling gate
        # (gate_eval.py, archive_handoffs.py) already relies on. C2 is the
        # backstop behind C3's _resolved_predecessor_canonical_kind (which
        # already canonicalizes); a raw compare here made the backstop
        # strictly weaker than the primary it backs up.
        if canonical_kind(_current_kind(contained)) == "roadmap-baton":
            # REPLAY CONVERGENCE (AC7): the gate condition can still be true
            # on a replay of an already-successful supersession — short-
            # circuit to the existing byte-identical no-op path
            # (_supersede_continued's own idempotency check, below) whenever
            # the on-disk record already carries deployment_state:continued
            # AND continued_into already equal to the requested successor,
            # BEFORE evaluating the gate at all. This is a reading of the
            # idempotency contract, not a demonstrated red test — see C6a's
            # own replay-convergence regression test. NOT touched by C1
            # (2026-08-05) — stays upstream of every arm below, same
            # ordering.
            already_converged = (
                _current_deployment_state(contained) == "continued"
                and _current_continued_into(contained) == continued_into
            )
            if not already_converged:
                # DR-126 § Clarifications C-1 (2026-08-05): every arm below
                # refuses — `dep_state` selects only which MESSAGE is
                # returned, never whether the call is refused.
                dep_result = blocked_by_dependents(contained, worktree, exclude=exclude)
                dep_state = dep_result.get("state")
                if dep_state == "dependents":
                    # Review: code-reviewer (P2, Finding 4) — surface dependents
                    # via this module's own rel-id convention, not raw absolute
                    # filesystem paths (which are machine-local and inconsistent
                    # with every other operator-facing message here).
                    dep_rel_ids = [
                        _wire_rel_id(Path(p), worktree)
                        for p in dep_result.get("dependents", [])
                    ]
                    out = _err(
                        f"refusing supersede: {rel_id} is kind: roadmap-baton "
                        "— DR-126 § Clarifications C-1: no automated "
                        "supersede for a roadmap baton in any state; it has "
                        "a live blocked_by dependent "
                        f"({', '.join(dep_rel_ids)})"
                    )
                    out["mode"] = mode
                    return out
                if dep_state == "none":
                    out = _err(
                        f"refusing supersede: {rel_id} is kind: roadmap-baton "
                        "— DR-126 § Clarifications C-1: no automated "
                        "supersede for a roadmap baton in any state; no "
                        "blocked_by dependent is currently live, refused "
                        "on kind alone"
                    )
                    out["mode"] = mode
                    return out
                # Review: code-reviewer (nit, Finding 1) — bare `else` rather
                # than `if dep_state == "indeterminate":` so this arm keeps
                # firing fail-closed for "indeterminate" AND any unrecognized
                # future `dep_state` value; narrowing to an explicit equality
                # check would drop that fail-closed-on-unknown property.
                out = _err(
                    f"refusing supersede: {rel_id} is kind: roadmap-baton "
                    "— DR-126 § Clarifications C-1: no automated supersede "
                    "for a roadmap baton in any state; refused on kind, "
                    "not on the dependent count — the live-dependent list "
                    "is unavailable because the blocked_by scan could not "
                    f"complete ({dep_result.get('error') or 'unknown resolver error'})"
                )
                out["mode"] = mode
                return out

    do_stamp = mode in ("stamp_shipped", "supersede")
    do_supersede = mode == "supersede"
    do_stamp_only = mode == "stamp_only"

    warnings: List[str] = []
    stamped = False
    superseded = False

    # ------------------------------------------------------------------
    # successor_path resolution (example-doctrine-repo, 2026-07-26) — resolves the
    # SUCCESSOR's own sha internally, BEFORE the scope-derived-selection
    # warning below (so that warning correctly no-ops once resolution
    # succeeds — stamp_kind is no longer "scope-derived" at that point).
    # Reuses archive_stamp._resolve_scope_sha (the same `git log
    # --format=%H -n1 -- <path>` primitive stamp_shipped_in's own
    # scope-path resolution already uses) rather than reimplementing a
    # second git-log call here. Only relevant to modes that stamp at all
    # (stamp_shipped / supersede / stamp_only) — a no-op for "chain".
    # ------------------------------------------------------------------
    if successor_path_raw and (do_stamp or do_stamp_only):
        from coordinator_core.archive_stamp import _resolve_scope_sha

        resolved_successor_sha = await asyncio.to_thread(
            _resolve_scope_sha, worktree, [successor_path_raw]
        )
        if resolved_successor_sha:
            stamp_sha = resolved_successor_sha
            stamp_kind = "successor"
        else:
            # Honest unresolved state — not an error. Same warnings channel
            # as the "resolved no commit"/"scope-derived selected" cases
            # below; falls back to this op's existing no-sha scope-derived
            # default rather than fabricating a sha.
            warnings.append(
                f"successor_path={successor_path_raw!r} resolved no commit via "
                "`git log -- <path>` (the successor is presumably not yet "
                "committed to this worktree) — falling back to this op's "
                "no-sha scope-derived default for this stamp"
            )
            _LOG.info(
                "handoff.archive_transition: successor_path=%r resolved no "
                "commit for %s (mode=%s) — falling back to scope-derived",
                successor_path_raw,
                rel_id,
                mode,
            )

    # DR-096 (example-doctrine-repo, 2026-07-26): scope-derivation is retired as the
    # PREFERRED write-time strategy but survives as a narrowing legacy path
    # here — this op has no `--sha` call shape, so every stamp attempt with
    # no caller-supplied sha silently fell into `kind="scope-derived"`
    # (line ~433 above) with no observable trace of which kind had been
    # selected. Surface the selection itself (not just its outcome) so a
    # reader of `warnings`/logs can tell "this stamp used the legacy
    # scope-derived path" without cross-referencing the absence of a --sha
    # argument. Mirrors the existing "resolved no commit" warning
    # convention below — this is a distinct fact (which strategy was
    # chosen) from that one (whether it found anything).
    if stamp_kind == "scope-derived" and (do_stamp or do_stamp_only):
        warnings.append(
            f"shipped_in_kind selected: scope-derived (legacy write-time "
            f"strategy, retired as preferred per DR-096) — no --sha was "
            f"supplied to this {mode!r} call for {rel_id}"
        )
        _LOG.info(
            "handoff.archive_transition: %s selected shipped_in_kind=scope-derived "
            "(no --sha supplied, mode=%s)",
            rel_id,
            mode,
        )

    # ------------------------------------------------------------------
    # DO_STAMP block (stamp_shipped / supersede) — BEFORE the guard,
    # unconditionally (mirrors bash :219-273 — the stamp lands even if the
    # guard subsequently decides to retain). Position A: no branch-tip
    # fallback — shipped_in is scope-path-resolved or left unset (see
    # module docstring).
    # ------------------------------------------------------------------
    if do_stamp:
        before = _current_shipped_in(contained)
        outcome = await asyncio.to_thread(
            stamp_shipped_in,
            str(contained),
            kind=stamp_kind,
            allow_branch_tip_fallback=False,
            sha=stamp_sha,
            force=stamp_force,
        )
        if outcome.exit_code != 0:
            # AC14 (§ S12 site (b), chunk C4): a stamp TRANSPORT failure (the
            # op itself erroring) is not the honest "no commit resolved,
            # shipped_in left unset" outcome (Position A, working as
            # designed) — it is this file's own instance of the invariant
            # the whole plan exists to enforce: a mutation op that cannot
            # confirm its own write must never exit 0. The prior behaviour
            # appended a warning and let archival (and, for mode="supersede",
            # the status flip below) proceed anyway, discoverable only by
            # parsing the warnings array. For mode="stamp_shipped" the
            # downstream flip-refusal (:1089, "no shipped_in could be
            # resolved") happened to already compose into an overall
            # non-zero exit when shipped_in had no prior value — but
            # mode="supersede" has no such downstream check (it never
            # requires shipped_in), so the old warning-only behaviour let a
            # supersede archival succeed silently on top of a failed stamp.
            # Refusing here closes that gap for every mode uniformly,
            # instead of depending on an incidental downstream composition.
            #
            # JUDGEMENT CALL: ABORT here rather than proceed-and-report-
            # nonzero. do_stamp runs before both the supersede status flip
            # and the live-children guard/archival move — at this point
            # nothing downstream has mutated yet, so returning now leaves
            # the artifact exactly as it was pre-call and a retry is safe.
            # Proceeding into supersede/archival on top of an unconfirmed
            # stamp would risk a half-mutated handoff (status flipped or
            # moved, shipped_in unconfirmed) that a retry cannot cleanly
            # recover — the opposite of retry-safe.
            warnings.append(
                f"stamp_shipped_in exited {outcome.exit_code} for {rel_id} "
                "— stamp transport failure"
            )
            out = _err(
                f"stamp transport failure for {rel_id}: stamp_shipped_in "
                f"exited {outcome.exit_code} — archival aborted, nothing "
                "else mutated by this call; retry once the underlying "
                "failure is resolved (pass --sha to retry with an explicit "
                "override once resolved, if appropriate)"
            )
            out["mode"] = mode
            out["stamped"] = stamped
            out["warnings"] = warnings
            return out
        elif not outcome.applied and stamp_sha and stamp_sha.strip() and not (
            outcome.prior_value is not None
            and _sha_canonically_matches(stamp_sha, outcome.prior_value)
        ):
            # AC6 (§ S11, chunk C0): a caller-supplied --sha the idempotency
            # guard silently retained-over must never be discarded quietly —
            # refuse loudly, naming --force as the remedy, rather than falling
            # through to the "left unset"/"retained prior value" warning
            # below. AC6b's same-commit re-stamp (canonical match) is
            # deliberately excluded from this branch — it is a legitimate
            # no-op, not a discard.
            out = _err(
                f"refusing to discard supplied --sha {stamp_sha!r} for {rel_id}: "
                f"shipped_in is already present (prior_value="
                f"{outcome.prior_value!r}) and does not canonically match the "
                "supplied sha — pass --force to overwrite it"
            )
            out["mode"] = mode
            out["stamped"] = stamped
            out["warnings"] = warnings
            return out
        else:
            after = _current_shipped_in(contained)
            if after is None or after == before:
                # AC7 (§ S11): distinguish "nothing was ever there" (prior_value
                # None) from "a stamp attempt no-opped, retaining a prior value"
                # (prior_value set) — the prior prose collapsed both into "left
                # unset" and asserted a scope-derivation cause that, on the
                # --sha override path, never ran (§ S11 diagnosis).
                if outcome.prior_value is None:
                    warnings.append(
                        f"stamp_shipped_in resolved no commit for {rel_id}'s scope: "
                        "paths — shipped_in left unset (Position A: no branch-tip "
                        "fallback)"
                    )
                else:
                    warnings.append(
                        f"stamp_shipped_in retained prior value "
                        f"{outcome.prior_value!r} for {rel_id} — nothing new was "
                        "written"
                    )
            else:
                stamped = True

    # ------------------------------------------------------------------
    # supersede: status:claimed + deployment_state:continued +
    # continued_into:<successor> — BEFORE the live-children guard (2026-07-27
    # fix, cross-repo example-doctrine-repo incident: "supersede silently no-ops"). PM ruling:
    # "as soon as a successor baton exists, the predecessor is by definition
    # no longer in flight" — a live claim holder is IRRELEVANT to that fact.
    # The status flip is NOT gated on `_handoff_has_live_children`'s
    # (predecessor/additional_predecessors/forked_from) liveness check; only
    # the archival move below still is. That ruling is scoped to ordinary
    # session-handoff succession (2026-08-02, plan
    # roadmap-baton-supersession-hazard, AC2; re-keyed 2026-08-05, DR-126 §
    # Clarifications C-1) — a `kind: roadmap-baton` predecessor never
    # reaches this point at all: the C2 blocked_by gate above (§
    # roadmap-baton blocked_by gate) refuses on `kind` alone, before
    # `do_stamp`/`do_supersede` are computed, whether or not it has live
    # blocked_by dependents (or is a converged replay, which short-circuits
    # into this same unconditional path). This mattered concretely because the
    # guard's own live-child
    # membership check (handoff.has_live_children) treats the SUCCESSOR
    # itself — the very handoff whose `predecessor:` names this candidate —
    # as a live child the instant it exists on disk with a non-terminal
    # deployment_state, which is the normal shape of every real `/handoff`
    # call: the successor is written, then archival is invoked, so the
    # guard retained on essentially every real supersede call and the status
    # flip beneath it (previously placed AFTER the guard's early return)
    # never ran. A failed mutation is NOT swallowed as a warning — fail loud
    # so the caller never proceeds with a half-superseded handoff (bash
    # :396-407 heritage, preserved).
    # ------------------------------------------------------------------
    if do_supersede:
        supersede_res = await asyncio.to_thread(
            _supersede_continued, contained, continued_into, repo_root
        )
        if supersede_res.get("exit_code") != 0:
            out = _err(
                f"supersede failed: {supersede_res.get('error', 'unknown error')}"
            )
            out["mode"] = mode
            out["stamped"] = stamped
            out["warnings"] = warnings
            return out
        superseded = True

    # ------------------------------------------------------------------
    # Archived-predecessor stamp-in-place (see module docstring § Archived-
    # predecessor stamp-in-place) — a supersede call whose target already
    # lives under one of ARCHIVE_ROOT_SUBDIRS is DONE the moment the status
    # flip above lands: there is no git-mv to perform (the record is already
    # archived) and no live-children guard question to ask (the guard exists
    # to decide whether it's safe to MOVE the file; this call never intends
    # to move it). Returning here, before the guard call, is deliberate —
    # calling the guard on an already-archived path would be a no-op query
    # whose answer this branch would then have to ignore anyway, and every
    # `retained`/`moved` field below is unambiguous without it (nothing was
    # retained because nothing was ever going to move).
    # ------------------------------------------------------------------
    if is_archived_target:
        return {
            "exit_code": 0,
            "mode": mode,
            "stamped": stamped,
            "superseded": superseded,
            "retained": False,
            "retain_reason": None,
            "moved": False,
            "warnings": warnings,
            "message": (
                f"superseded {rel_id} in place — already archived, no move needed"
            ),
        }

    # ------------------------------------------------------------------
    # Live-children guard — UNCONDITIONAL (all modes, no flag), and governs
    # ONLY the archival move from here on (see the supersede block directly
    # above — the status flip is no longer subject to this guard). Tri-state:
    # exit 1 = safe-to-archive -> proceed; exit 0 OR exit 2 = DO-NOT-move.
    # ------------------------------------------------------------------
    guard_result = await _handoff_has_live_children(
        {"candidate": str(contained), "exclude": exclude}, repo_root
    )
    guard_exit = guard_result.get("exit_code")

    if guard_exit != 1:
        if guard_exit == 0:
            retain_kind = "live-parent"
            retain_reason = (
                "predecessor retained — still a live merge-parent of another "
                "active handoff"
            )
            _LOG.info("handoff.archive_transition: %s (%s)", retain_reason, rel_id)
        else:
            retain_kind = "indeterminate"
            retain_reason = (
                f"guard indeterminate (exit_code {guard_exit}) — fail-closed; "
                "predecessor retained to avoid data loss"
            )
            warnings.append(
                "retained because guard could not determine liveness "
                "(degraded state) — NOT a deliberate retain"
            )
            _LOG.warning("handoff.archive_transition: %s (%s)", retain_reason, rel_id)
        # `superseded` reflects the mutation applied ABOVE, before this guard
        # ran — a retained (not-yet-archived) supersede call still reports
        # superseded:True when the status flip landed. This is the load-
        # bearing change: retention now describes ONLY the archival move,
        # never the status flip.
        return {
            "exit_code": 0,
            "mode": mode,
            "stamped": stamped,
            "superseded": superseded,
            "retained": True,
            "retain_reason": retain_reason,
            "retain_kind": retain_kind,
            "moved": False,
            "warnings": warnings,
            "error": None,
            "message": (
                f"superseded {rel_id}; {retain_reason}"
                if superseded
                else retain_reason
            ),
        }

    # ------------------------------------------------------------------
    # stamp_only: guard has cleared — stamp in place, NO git mv (async sweep
    # archives later). Mirrors bash :319-374 (its own duplicate stamp block,
    # run only after the guard clears). Position A: no branch-tip fallback.
    # ------------------------------------------------------------------
    if do_stamp_only:
        before = _current_shipped_in(contained)
        outcome = await asyncio.to_thread(
            stamp_shipped_in,
            str(contained),
            kind=stamp_kind,
            allow_branch_tip_fallback=False,
            sha=stamp_sha,
            force=stamp_force,
        )
        if outcome.exit_code != 0:
            # AC14 — identical treatment (and identical abort-for-retry-
            # safety rationale) to the do_stamp twin above. At this point
            # the live-children guard has already cleared and no further
            # write (the flip-refusal check at :1030 or `_ship` below) has
            # run yet — aborting here leaves the handoff exactly as it was
            # pre-call. Unlike the do_stamp twin, stamp_only's own
            # downstream flip-refusal (:1030) would have caught this too
            # (stamp_only always requires a resolved shipped_in before its
            # own _ship call) — but refusing at the point of failure, not
            # a few lines later via a check whose real subject is a
            # different fact, keeps the "why" of the refusal accurate.
            warnings.append(
                f"stamp_shipped_in exited {outcome.exit_code} for {rel_id} "
                "— stamp transport failure"
            )
            out = _err(
                f"stamp transport failure for {rel_id}: stamp_shipped_in "
                f"exited {outcome.exit_code} — --stamp-only aborted, "
                "nothing else mutated by this call; retry once the "
                "underlying failure is resolved (pass --sha to retry with "
                "an explicit override once resolved, if appropriate)"
            )
            out["mode"] = mode
            out["stamped"] = stamped
            out["warnings"] = warnings
            return out
        elif not outcome.applied and stamp_sha and stamp_sha.strip() and not (
            outcome.prior_value is not None
            and _sha_canonically_matches(stamp_sha, outcome.prior_value)
        ):
            # AC6/AC6b — identical treatment to the do_stamp twin above; see
            # its comment for the full rationale.
            out = _err(
                f"refusing to discard supplied --sha {stamp_sha!r} for {rel_id}: "
                f"shipped_in is already present (prior_value="
                f"{outcome.prior_value!r}) and does not canonically match the "
                "supplied sha — pass --force to overwrite it"
            )
            out["mode"] = mode
            out["stamped"] = stamped
            out["warnings"] = warnings
            return out
        else:
            after = _current_shipped_in(contained)
            if after is None or after == before:
                # AC7 — identical split to the do_stamp twin above.
                if outcome.prior_value is None:
                    warnings.append(
                        f"stamp_shipped_in resolved no commit for {rel_id}'s scope: "
                        "paths — shipped_in left unset (Position A: no branch-tip "
                        "fallback)"
                    )
                else:
                    warnings.append(
                        f"stamp_shipped_in retained prior value "
                        f"{outcome.prior_value!r} for {rel_id} — nothing new was "
                        "written"
                    )
                stamped = False
            else:
                stamped = True

        # ------------------------------------------------------------------
        # Refuse the flip when shipped_in would be left unset.
        #
        # The frontmatter schema REQUIRES shipped_in whenever deployment_state
        # is 'shipped' (handoffs created on/after 2026-05-29). Position A
        # deliberately leaves shipped_in unset rather than guessing a
        # branch-tip sha (see module docstring § Position A) — but that
        # honest "unresolved" must never reach the _ship flip below, or the
        # write proceeds, the frontmatter validator then rejects the very
        # state this call just wrote (deployment_state:shipped with no
        # shipped_in), and the caller is left staring at a downstream rc!=0
        # with no clue WHICH argument would fix it. Check the CURRENT
        # on-disk value (not the `stamped` bool above, which only tracks
        # whether THIS call wrote a *fresh* SHA) — an idempotent re-ship of
        # an already-shipped handoff has stamped=False yet a perfectly valid
        # pre-existing shipped_in, and must still be allowed to proceed.
        #
        # Negative-spec: this is NOT a branch-tip fallback and does not add
        # any resolution path — it is a refusal gate. The fix on the missing
        # side is the caller passing --sha, never this op guessing one.
        # Incident: cross-repo memo
        # 2026-07-22-claude-central-em-deliver-ship-handoff-writes-
        # deployment-state-shipped-without-shipped-in.md.
        # ------------------------------------------------------------------
        if _current_shipped_in(contained) is None:
            out = _err(
                f"stamp_only: refusing to flip deployment_state:shipped for "
                f"{rel_id} — no shipped_in could be resolved from its scope: "
                "paths, and no --sha was supplied to `archive-stamp-cli "
                "ship-handoff`. Pass --sha <SHA> to resolve/override "
                "shipped_in explicitly (Position A never guesses a "
                "branch-tip sha — see module docstring)."
            )
            out["mode"] = mode
            out["stamped"] = stamped
            out["warnings"] = warnings
            return out

        ship_res = await asyncio.to_thread(_ship, rel_id, worktree, repo_root)
        if ship_res.get("exit_code") != 0:
            out = _err(
                f"stamp_only: deployment_state:shipped write failed: "
                f"{ship_res.get('error', 'unknown error')}"
            )
            out["mode"] = mode
            out["stamped"] = stamped
            out["warnings"] = warnings
            return out

        return {
            "exit_code": 0,
            "mode": mode,
            "stamped": stamped,
            "superseded": False,
            "retained": False,
            "retain_reason": None,
            "moved": False,
            "warnings": warnings,
            "message": f"stamped {rel_id} (deployment_state: shipped) — retained in state/handoffs/ for later archival sweep",
        }

    # ------------------------------------------------------------------
    # stamp_shipped: guard has cleared — flip deployment_state:shipped
    # BEFORE the git mv, so the archived record is internally consistent
    # (shipped_in AND deployment_state:shipped land together). Without this
    # the archived handoff carried shipped_in while deployment_state stayed
    # in_flight — the half-state the /workstream-complete Step 2.7 doc and
    # coordinator/CLAUDE.md handoff-lifecycle doctrine both forbid (terminal
    # shipped record = status:claimed + deployment_state:shipped +
    # shipped_in:<sha>). The flip is scoped to stamp_shipped: supersede owns
    # deployment_state:continued above, chain does no stamp, and stamp_only
    # already flips via its own _ship call before returning. _ship is
    # idempotent (no-op when already shipped), so a re-run is a clean no-op.
    # A failed write is fail-loud — never git mv a half-shipped handoff.
    # (cross-repo memo 2026-07-21-claude-central-em-archive-stamp-
    # deployment-state-not-flipped-shipped.md)
    # ------------------------------------------------------------------
    if mode == "stamp_shipped":
        # Refuse the flip when shipped_in would be left unset — same guard
        # and rationale as the stamp_only branch above (see its comment
        # block); duplicated here rather than factored out because the two
        # branches sit either side of the supersede/git-mv block and return
        # through different envelopes.
        if _current_shipped_in(contained) is None:
            out = _err(
                f"stamp_shipped: refusing to flip deployment_state:shipped for "
                f"{rel_id} — no shipped_in could be resolved from its scope: "
                "paths, and no --sha was supplied to `archive-stamp-cli "
                "ship-handoff`. Pass --sha <SHA> to resolve/override "
                "shipped_in explicitly (Position A never guesses a "
                "branch-tip sha — see module docstring)."
            )
            out["mode"] = mode
            out["stamped"] = stamped
            out["warnings"] = warnings
            return out

        ship_res = await asyncio.to_thread(_ship, rel_id, worktree, repo_root)
        if ship_res.get("exit_code") != 0:
            out = _err(
                f"stamp_shipped: deployment_state:shipped write failed: "
                f"{ship_res.get('error', 'unknown error')}"
            )
            out["mode"] = mode
            out["stamped"] = stamped
            out["warnings"] = warnings
            return out

    # ------------------------------------------------------------------
    # TERMINAL-STATE PRECONDITION (see module docstring § Terminal-state
    # precondition) — refuse the move outright when the candidate's on-disk
    # deployment_state is not one of shipped/continued/closed at this point.
    # Runs for every mode that reaches here (chain, stamp_shipped, supersede
    # — stamp_only already returned above) and AFTER any stamp/supersede
    # mutation this same call just performed, so a fresh terminal write is
    # visible here. This is the tooth that keeps mode="chain" (which stamps
    # nothing, by design) from ever git-mv'ing a non-terminal baton into
    # archive/handoffs/, where handoff_transition._resolve_path's
    # live-only containment means NO transition verb could ever repair it
    # again. Refusal, not an in-op stamp — see module docstring for why.
    # ------------------------------------------------------------------
    current_deployment_state = _current_deployment_state(contained)
    if current_deployment_state not in _TERMINAL_DEPLOYMENT_STATES:
        out = _err(
            f"refusing to archive {rel_id}: deployment_state is "
            f"{current_deployment_state!r}, not terminal (must be one of "
            f"{sorted(_TERMINAL_DEPLOYMENT_STATES)}) — mode={mode!r} does "
            "not stamp a terminal state on this baton. Reach a terminal "
            "state first: mode='stamp_shipped' (-> deployment_state: "
            "shipped), mode='supersede' with continued_into=<successor> "
            "(-> deployment_state: continued), or a direct "
            "handoff.transition close call (-> deployment_state: closed) "
            "— then retry this archival."
        )
        out["mode"] = mode
        out["stamped"] = stamped
        out["superseded"] = superseded
        out["warnings"] = warnings
        return out

    # ------------------------------------------------------------------
    # git mv — YYYY-MM archive/handoffs/ destination + one atomic commit.
    # On failure (already moved by a concurrent session): warning, continue,
    # exit_code:0 (bash :438-440 — non-fatal).
    # ------------------------------------------------------------------
    dest = handoff_archive_dest(worktree, contained)
    # restage_src=do_stamp (2026-07-27 review fix, Finding 1): stamp_shipped
    # and supersede both write to `contained` on disk (via the do_stamp
    # block above and, for supersede, _supersede_continued /
    # mode=="stamp_shipped"'s _ship call) BEFORE this git-mv. Without
    # restage_src, archive_and_commit's private index still carries the
    # pre-write blob for `contained` (seeded from `git read-tree HEAD`), so
    # the archived commit would silently carry stale, pre-stamp content —
    # the exact defect class the sibling archive_handoffs.py fix
    # (restage_src=is_heir, "2026-07-27 C4c fix") closed the same day. See
    # Move.restage_src in coordinator_core/ops/fleet/_common.py.
    move = Move(src=contained, dst=dest, candidate_id=rel_id, restage_src=do_stamp)
    subject = f"archive handoff: {rel_id}\n\nVia handoff.archive_transition (mode={mode})."
    acted, failed = await archive_and_commit(worktree, [move], subject)

    moved = bool(acted) and not failed
    if failed:
        reason = failed[0].get("reason", "git-mv-failed")
        warnings.append(
            f"git mv failed for {rel_id}: {reason} — may already have been moved "
            "by a concurrent session; continuing"
        )

    if moved:
        message = f"archived {rel_id} to {_wire_rel_id(dest, worktree)}"
    elif do_supersede:
        message = f"superseded {rel_id}; archival did not complete this call"
    else:
        message = f"{rel_id}: archival did not complete this call"

    return {
        "exit_code": 0,
        "mode": mode,
        "stamped": stamped,
        "superseded": superseded,
        "retained": False,
        "retain_reason": None,
        "moved": moved,
        "warnings": warnings,
        "message": message,
    }

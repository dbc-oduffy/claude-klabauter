"""deliverable_carry — deliverable_id/initiative-FK carry-or-mint cascade.

Purpose: implements DR-207 DD#1's carry-not-remint rule — mint a stable opaque
`deliverable_id` once at the earliest artifact for a piece of work and carry it
verbatim by every downstream artifact, minting fresh only when no parent id is
discoverable from context.

Relocated verbatim (behaviour byte-identical) from
`coordinator/bin/handoff-deliverable-carry.py`, where this cascade was correct
but unimportable from the engine — `coordinator/bin/` scripts are not an
importable package for `coordinator_core` callers, so this logic accumulated
ZERO production callers despite being the canonical implementation. That
bin/-only home is exactly the defect this plan (docs/plans/2026-08-01-
deliverable-id-carry-onto-executing-handoff.md, chunk C1b) fixes: giving the
cascade an engine-importable home lets `/handoff` and other in-process callers
wire to it directly instead of shelling out.

Cascade (mirrors the bash oracle verbatim — see the original SKILL.md block):
  deliverable_id — 1. active plan's frontmatter `deliverable_id`
                   2. predecessor handoff's frontmatter `deliverable_id`
                   3. carry (mint(deliverable_id=...)) if either hit, else
                      mint-from-slug — mint(slug=<the caller's `work_slug`>),
                      or mint(slug="<YYYYMMDD>-<slug_suffix>") when the caller
                      supplies none
  initiative     — 1. active plan's frontmatter `initiative`
                   2. predecessor handoff's frontmatter `initiative` (fallback
                      only; continuation handoffs inherit the predecessor's
                      initiative FK when the plan doesn't carry one)

Dropped-join refusal, two arms (DR-207 DD#1 AC4; widened for the plan-input
axis 2026-08-13): mint-from-slug is refused, loud, whenever a plan is in
play and no rung of the cascade produced a `deliverable_id` — this is the
carry-not-remint rule's own fail-loud complement. Arm one fires when the
plan arrives as `plan_file` (the session's CLAIMED plan, via
`resolve_claimed_plan_path`). Arm two fires when the plan arrives instead
as `predecessor` — the plan-input axis, `predecessor_is_plan_input=True` —
because a plan handed directly as the artifact under `baton-assemble brief
handoff <plan-path>` never touches `plan_file` at all. Same defect, same
exception type, opposite entry door; see `resolve_deliverable_and_
initiative`'s own docstring for why the caller (not this function) asserts
the plan-input fact.

Spec backlink: coordinator/skills/handoff/SKILL.md § Deliverable-spine threading
               (D1 carry-not-remint) — DoE-claude, C3d
               docs/plans/2026-08-01-deliverable-id-carry-onto-executing-handoff.md
               (DR-207 DD#1) — chunk C1b
               docs/decisions/DR-207-deliverable-spine-initiative-entity.md DD#1
               (earliest-artifact tiebreak)
               coordinator_core/contract/commit-trailer-producer-contract.md § 1.2
               (two independent producers of the same FK, and the value-divergence
               hazard that spelling-only enforcement missed)

Negative-spec: this is the ONLY implementation of the carry-or-mint cascade in
this repo. Do not re-implement or fork a second copy of
`resolve_deliverable_and_initiative`, `DroppedDeliverableJoinError`, or
`DivergentDeliverableIdError` anywhere else — `coordinator/bin/handoff-
deliverable-carry.py` imports from this module rather than carrying its own copy.

Sanctioned exception (DR-328, 2026-08-19): `coordinator_core.ops.ceremony.
git_native.DeliverableIdAssertionConflictError` is a deliberate SIBLING of
`DivergentDeliverableIdError` (not a subclass, not a fork of it) — it serves a
different consumer (a commit-time caller-vs-message-trailer assertion
conflict, not this module's carry-or-mint provenance cascade) and must not
reach `baton_assemble.brief`'s `DivergentDeliverableIdError`-typed catch,
which converts that error into a `j-divergent-deliverable-id` judgment point
nonsensical for a commit assertion conflict. Read this negative-spec as
scoped to forks of the carry class itself; a sibling built to stay OUT of its
typed catch is not one.
"""

from __future__ import annotations

import datetime
import os
import sys

from coordinator_core.frontmatter.baton_class import kind_values_for_canonical
from coordinator_core.ops.deliverable_equivalence import canonicalize

# Accepted `kind` values for a genuine roadmap stub, at the session-state
# parent tier (AC1). `handoff.schema.json` x-schema-version 4.0.0 RETIRED
# `spinoff-roadmap` (along with `spinoff-goal` and `spinoff-roadmap-creator`)
# from `properties.kind.enum`, replacing it with `roadmap-baton` — the live
# corpus and `coordinator/bin/coordinator-doc-new.py`'s scaffolder both emit
# `kind: roadmap-baton` now. `spinoff-roadmap` stays accepted here because
# `handoff-archived.schema.json` was deliberately WIDENED in the same 4.0.0
# change to admit both vocabularies (the archived corpus permanently retains
# the historical name), so an archived roadmap stub a session still holds a
# claim on legitimately carries the retired spelling. Both values denote the
# same "is this a roadmap stub" fact — accepting both preserves the AC1/AC4b
# false-merge guard in full while matching the schema's actual accepted
# vocabulary. Do NOT add `spinoff-goal`/`goal-seed`/`spinoff-roadmap-creator`/
# `roadmap-seed` here — those are a different artifact class, not a roadmap
# stub, and must keep failing the AC4b false-merge check.
#
# Sourced from `baton_class.kind_values_for_canonical("roadmap-baton")`
# rather than a hand-paired literal — that accessor returns exactly
# `["roadmap-baton", "spinoff-roadmap"]` (the only pre-rename alias whose
# target is `roadmap-baton`), so this is the same membership, derived from
# the single owning table instead of re-declaring the retired/successor
# pair here. See `coordinator_core/frontmatter/baton_class.py`'s "Vocabulary
# bridge" section and
# `coordinator_core/tests/test_baton_class_is_the_only_membership_set.py`.
#
# Review: coordinatorcode-reviewer-3e4f4e1b — this coupling is intended, made
# explicit here rather than removed: a future `_PRE_RENAME_ALIASES` entry
# targeting `roadmap-baton` widens this set automatically with no code change
# at this call site, so `baton_class.py`'s own review discipline is now the
# only gate on this membership.
_ROADMAP_STUB_KINDS = frozenset(kind_values_for_canonical("roadmap-baton"))


class DroppedDeliverableJoinError(RuntimeError):
    """Raised when an active plan names no `deliverable_id` (absent field, unreadable
    file, or literal `null` — all indistinguishable at this layer) and the predecessor
    handoff's `deliverable_id` fallback also yields nothing.

    This is distinct from the benign "no active plan / no predecessor at all" case
    (nothing to carry from), which stays a silent mint-from-slug. This case has an
    active plan in hand that dropped the join — it must fail loud, not degrade
    silently into a fresh mint that severs the deliverable-spine thread.
    """


class DivergentDeliverableIdError(RuntimeError):
    """Raised when the active plan AND the predecessor handoff both name a non-empty
    `deliverable_id`, and the two values disagree.

    This is the disagreement sibling of `DroppedDeliverableJoinError` (which catches
    ABSENCE — neither rung has a value). This one catches PRESENCE-but-mismatch: both
    rungs answered, and joining on either silently drops the other. Left unchecked,
    `close_out_and_stamp` under-counts or zero-counts chunk evidence for whichever id
    it isn't watching, and every consumer that joins on exact string equality
    (rag/cockpit included) forks one deliverable into two entities — silently, in
    every direction. See docs/decisions/DR-207-deliverable-spine-initiative-entity.md
    DD#1 and coordinator_core/contract/commit-trailer-producer-contract.md § 1.2 (the
    two-independent-producers hazard that ruling fixed for spelling but not value).

    Per DR-207 DD#1: mint once at the earliest artifact and carry it verbatim — the
    EARLIEST artifact's id wins. That is a fact about artifact history this function
    cannot see; the plan/predecessor arguments it receives carry no timestamp or
    provenance that would let it apply the tiebreak itself.

    Negative-spec — do NOT make this function auto-pick a winner. Both plausible
    auto-resolutions are wrong in some real case: always preferring the plan is
    correct on the ordinary plan -> handoff edge, but wrong when a roadmap stub
    predates the plan and the plan wrongly re-minted (the earliest artifact is then
    the predecessor's id, not the plan's) — a live cross-repo incident, not a
    hypothetical. Always preferring the predecessor is correct in that case but wrong
    on the ordinary edge. There is no context-free rule that resolves both correctly;
    only a human applying the earliest-artifact test to the actual artifact history
    can. A future "helpful" simplification that picks either side by default silently
    reintroduces the exact fork-into-two-entities failure this error exists to catch —
    resist it. The fix here is to make the divergence impossible to miss, not to guess.
    """


def resolve_session_state_parent_deliverable_id(
    read_frontmatter_field,
    held_claim_path: "str | None",
) -> "str | None":
    """Session-state parent tier (AC1/AC2/AC3) — carries `deliverable_id` onto a
    plan being scaffolded from a session-HELD roadmap stub (`kind` in
    `_ROADMAP_STUB_KINDS`: `roadmap-baton` or the retired `spinoff-roadmap`),
    never from the file being scaffolded itself.

    ``held_claim_path`` is a caller-resolved path to whatever artifact the
    running session's SESSION STATE names as held (see
    `coordinator_core.session.claims.list_claims_by_session_checked` — the
    CALLER's job, not this function's, mirroring `resolve_deliverable_and_
    initiative`'s own contract of taking already-resolved `plan_file`/
    `predecessor` paths rather than resolving them itself). This function
    NEVER reads `predecessor_handoff` off the file being scaffolded — that
    field is emitted commented out and hand-filled later, so a scaffold-time
    read of it always finds nothing (the trap this tier exists to avoid).

    Gate (AC1): the held claim's OWN frontmatter `kind` must be one of
    `_ROADMAP_STUB_KINDS` (`roadmap-baton` — the live enum value — or the
    retired `spinoff-roadmap`, still valid per `handoff-archived.schema.json`
    and still present on archived stubs). Holding ANY claim is not evidence a
    plan descends from it — an ordinary handoff/memo/plan baton unrelated to
    the work being scaffolded must never be carried onto it (the false-merge
    case AC4b pins). A resolved claim whose `kind` is something else (e.g.
    plain `spinoff`, `handoff`, `session-handoff`) logs the rejected
    candidate id on stderr and falls through to `None` (AC1).

    Omit-rather-than-guess (AC3): no `held_claim_path`, an unreadable file, a
    `kind` outside `_ROADMAP_STUB_KINDS`, or an absent/`null`/blank
    `deliverable_id` on the held stub all return `None` — the caller then
    falls through to mint-from-slug exactly as it did before this tier
    existed. This function NEVER raises.

    Error-policy choice, stated explicitly (this tier sits at the boundary
    between two disagreeing module conventions): `resolve_deliverable_and_
    initiative` below is fail-loud on `DivergentDeliverableIdError` because
    that cascade sees TWO independently-authored rungs (plan + predecessor)
    that can genuinely DISAGREE, and silently picking one would hide a real
    provenance conflict. This tier sees at most ONE candidate value (the
    single held claim, if any) — there is no second rung to disagree with,
    so there is nothing to arbitrate and nothing a raise would protect. Every
    failure mode here is a SOURCING ambiguity (wrong kind, absent field,
    unreadable file), which is exactly the omit-rather-than-guess class this
    plan's own AC3 commits to, not the divergent-values class
    `DivergentDeliverableIdError` exists for. Deliberately never raises.

    Logs the accept/reject outcome on stderr per DR-207 D1 (AC2) — the
    caller separately logs the final carry-vs-mint decision via the existing
    stderr convention once it acts on this function's return value.
    """
    if not held_claim_path or not os.path.isfile(held_claim_path):
        return None

    kind = read_frontmatter_field(held_claim_path, "kind")
    if kind not in _ROADMAP_STUB_KINDS:
        print(
            "deliverable_carry: session-state parent tier — held claim "
            f"'{held_claim_path}' has kind {kind!r}, not a roadmap stub kind "
            f"({sorted(_ROADMAP_STUB_KINDS)!r}) — holding a claim is not "
            "evidence this plan descends from it; rejecting as parent "
            "candidate, falling through to mint-from-slug",
            file=sys.stderr,
        )
        return None

    dlvr_id = read_frontmatter_field(held_claim_path, "deliverable_id")
    if not dlvr_id:
        print(
            "deliverable_carry: session-state parent tier — held roadmap stub "
            f"'{held_claim_path}' (kind {kind!r}) carries no deliverable_id "
            "(absent/null/blank) — falling through to mint-from-slug",
            file=sys.stderr,
        )
        return None

    print(
        "deliverable_carry: session-state parent tier — held roadmap stub "
        f"'{held_claim_path}' (kind {kind!r}) carries deliverable_id {dlvr_id!r} "
        "— carrying",
        file=sys.stderr,
    )
    return dlvr_id


def resolve_explicit_predecessor_edge_deliverable_id(
    read_frontmatter_field,
    predecessor_path: "str | None",
) -> "str | None":
    """Explicit-predecessor-edge tier (C2, AC1/AC4/AC9) — carries
    `deliverable_id` onto an artifact being scaffolded from an AUTHOR-
    ASSERTED explicit edge (`--predecessor` at the CLI seam), regardless of
    the referenced artifact's `kind`.

    Sibling to `resolve_session_state_parent_deliverable_id` above, and
    deliberately narrower in what it gates on: that tier admits a SESSION-
    HELD claim only when the held artifact's own `kind` marks it a roadmap
    stub, because holding a claim is not, by itself, evidence of descent
    (AC4's negative half, unchanged — see that function's docstring and
    `_ROADMAP_STUB_KINDS`). This tier sees a different signal: the author
    explicitly named `predecessor_path` as this artifact's predecessor. That
    assertion is descent evidence on its own, independent of the referenced
    artifact's `kind` — DR-294 declines a *live pickup claim* as guard
    evidence ("a claim is bookkeeping, not an attestation") for an analogous
    guard, but an author-asserted explicit edge is not a session-bookkeeping
    signal, so it sits outside DR-294's decline (see this plan's C2 body).

    ``predecessor_path`` is a caller-resolved path (mirroring every other
    tier in this module's contract of taking already-resolved paths rather
    than resolving them itself) — typically the CLI's `--predecessor` flag
    value, joined against the repo root by the caller.

    Omit-rather-than-guess: no `predecessor_path`, an unreadable file, or an
    absent/`null`/blank `deliverable_id` on the referenced artifact all
    return `None` — the caller then falls through to whatever the next rung
    (or ultimately mint-from-slug) resolves. This function NEVER raises,
    mirroring `resolve_session_state_parent_deliverable_id`'s own error
    posture: there is only one candidate value here, never two rungs that
    could disagree, so there is nothing for a raise to protect.

    Does NOT touch `resolve_deliverable_and_initiative`'s claimed-plan/
    predecessor cascade below — that cascade is the `handoff` arm's own
    ruled 2026-08-03 behaviour (claimed-plan outranks predecessor there, and
    a disagreement between them still raises `DivergentDeliverableIdError`)
    and this function is never called from within it.

    Logs the accept/reject outcome on stderr per DR-207 D1 (AC2), matching
    `resolve_session_state_parent_deliverable_id`'s own logging convention.
    """
    if not predecessor_path or not os.path.isfile(predecessor_path):
        return None

    dlvr_id = read_frontmatter_field(predecessor_path, "deliverable_id")
    if not dlvr_id:
        print(
            "deliverable_carry: explicit-predecessor-edge tier — predecessor "
            f"'{predecessor_path}' carries no deliverable_id (absent/null/blank) "
            "— falling through",
            file=sys.stderr,
        )
        return None

    print(
        "deliverable_carry: explicit-predecessor-edge tier — predecessor "
        f"'{predecessor_path}' carries deliverable_id {dlvr_id!r} — carrying",
        file=sys.stderr,
    )
    return dlvr_id


def resolve_deliverable_and_initiative(
    read_frontmatter_field,
    mint,
    plan_file: str | None,
    predecessor: str | None,
    slug_suffix: str = "handoff",
    *,
    additional_predecessors: list[str] | None = None,
    equivalence_map: dict[str, str] | None = None,
    predecessor_is_plan_input: bool = False,
    work_slug: str | None = None,
) -> tuple[str, str]:
    """Run the carry-or-mint cascade. Returns (deliverable_id, initiative_id).

    Mirrors the bash oracle's 3-step deliverable_id discovery order (active plan ->
    predecessor handoff -> mint) and its 2-step initiative fallback (plan -> predecessor),
    plus the carry/mint-from-slug stderr logging the oracle's inline comment documents.

    Both the plan rung and the predecessor rung are read unconditionally (whenever
    each source exists) BEFORE any carry/mint decision is made — this is the one site
    in the engine that can see both values at once, so it is the only place a
    divergence between them (see `DivergentDeliverableIdError`) can be caught at all.
    Reading both does not change which value wins when they agree or when only one is
    present: the plan rung still takes precedence over the predecessor rung, byte-
    identical to the prior first-hit-wins cascade in every non-divergent case.

    N-rung widening (sedge-01, `succession-edge-cardinality` roadmap): `additional_
    predecessors` — every fan-in leg beyond the primary predecessor — is compared
    against the plan/predecessor rungs for divergence too, but participates in
    divergence detection ONLY. The carry/mint WINNER is still `plan_dlvr_id or
    predecessor_dlvr_id`, unchanged from the 2-rung cascade — an additional-
    predecessor rung never becomes the carried id, it only ever proves (or fails to
    prove) that every rung agrees. Each entry is expected already-RESOLVED (archive-
    aware, qualified) by the caller — this function does no path resolution itself,
    mirroring its existing `plan_file`/`predecessor` contract. A rung whose path is
    not `os.path.isfile()` degrades silently to "" (empty/absent), exactly as the
    plan and predecessor rungs already do above — an unreadable/archived additional-
    predecessor path already fails loud further upstream, in the caller's own path
    resolution, not here (R5 of the sedge-01 EM ruling).

    Known uncovered leg (R3 of the sedge-01 EM ruling, deliberately out of reach, scope
    clarified 2026-08-11): `baton_assemble.resolve_lineage`'s `is_plan_input` branch
    discovers a SECOND set of ledger-sourced extra predecessor paths
    (`_resolve_held_handoff_for_session`'s return) only AFTER this function is called.
    Those legs are NOT included in `additional_predecessors` and are therefore
    invisible to THIS FUNCTION'S divergence check ONLY — a named, accepted gap, not a
    silent one. They are NOT dropped from `resolve_lineage`'s own
    `lineage["additional_predecessors"]` output, which resolves and appends them
    separately, after this function returns — see that assignment's own comment.

    `equivalence_map` ({loser_id: winner_id}, from `deliverable_equivalence.
    load_equivalence_map`) is consulted ONLY to decide whether two rungs' RAW ids
    are the same declared entity — every comparison canonicalizes both sides via
    `deliverable_equivalence.canonicalize()` before comparing. This is read/compare-
    side ONLY: the returned `deliverable_id` (and the id handed to `mint(...)`) stays
    the RAW winning value, and the raise message below reports RAW ids/paths, never
    canonicalized ones — mirrors `execute_plan_assemble/close_out_and_stamp.py`'s own
    canonicalize-both-sides-never-write-back discipline. `None`/omitted degrades to
    `{}` (every id canonicalizes to itself), i.e. today's raw-comparison behaviour —
    no fallback of this function's own is layered on top of `load_equivalence_map`'s
    existing missing-artifact degrade.

    `predecessor_is_plan_input` (keyword-only, defaults `False` — every existing
    call site is unaffected): the CALLER's assertion that `predecessor` is itself
    a plan input (the plan->execute trigger's own plan, arriving on the
    predecessor axis rather than as `plan_file`) — this function never reads
    `predecessor`'s frontmatter or path to decide that for itself, mirroring
    `resolve_session_state_parent_deliverable_id`'s own division of labour above.
    When set and no rung of the cascade produces a `deliverable_id`, this raises
    `DroppedDeliverableJoinError` exactly as the `plan_active` arm below does — a
    plan dropped its join whether it arrived as `plan_file` or as `predecessor`,
    and the refusal must not depend on which door it came through. See the
    module docstring's "Dropped-join refusal, two arms" block.

    `work_slug` (keyword-only, defaults `None` — every existing call site is
    unaffected): the mint-from-slug basis for a chain-root artifact, supplied
    by the caller as the WORK's own slug. When omitted the basis stays the
    date-shaped `<YYYYMMDD>-<slug_suffix>` fallback, which names the day
    rather than the work — two unrelated chain roots scaffolded in one session
    then differ only in `mint_from_slug`'s random suffix, and neither id says
    what it identifies. The CALLER owns deciding whether it has a real slug to
    give: a placeholder title must never become a durable id (coordinator-doc-
    new.py's `_is_placeholder_title`), so a caller with no author-written
    title passes nothing and takes the date fallback. Legibility and
    attribution only, never correctness — `mint_from_slug` derives its suffix
    from `sha1(slug|time|pid|random)`, so neither basis can collide.
    """
    plan_active = bool(plan_file and os.path.isfile(plan_file))
    predecessor_active = bool(predecessor and os.path.isfile(predecessor))

    plan_dlvr_id = read_frontmatter_field(plan_file, "deliverable_id") if plan_active else ""
    predecessor_dlvr_id = (
        read_frontmatter_field(predecessor, "deliverable_id") if predecessor_active else ""
    )

    _equivalence_map = equivalence_map or {}

    # Every rung of the cascade, in plan -> predecessor -> fan-in order. Each
    # additional-predecessor rung degrades to "" when its path is not a readable
    # file (R5) -- the SAME degrade the plan/predecessor rungs already apply above,
    # not a new one invented for this arity.
    rungs: list[tuple[str | None, str]] = [
        (plan_file, plan_dlvr_id),
        (predecessor, predecessor_dlvr_id),
    ]
    for _extra_path in additional_predecessors or []:
        _extra_id = (
            read_frontmatter_field(_extra_path, "deliverable_id")
            if _extra_path and os.path.isfile(_extra_path)
            else ""
        )
        rungs.append((_extra_path, _extra_id))

    present_rungs = [(path, raw_id) for path, raw_id in rungs if raw_id]
    canonical_values = {canonicalize(raw_id, _equivalence_map) for _, raw_id in present_rungs}

    if len(canonical_values) > 1:
        # Windows-portability fix (discovered live re-running this stub's own
        # test on a Windows host): `path` is already a plain path STRING, not
        # a value that benefits from `!r`'s quoting -- reprising it double-
        # escapes a Windows backslash-separated path (`repr()` renders each
        # `\` as `\\`), which breaks a caller's plain `path in message`
        # substring check on Windows only (POSIX paths have no backslash to
        # escape, so this was invisible there). `raw_id` keeps `!r` -- ids
        # never contain path separators.
        diverging = "; ".join(
            f"{path} names deliverable_id {raw_id!r}" for path, raw_id in present_rungs
        )
        raise DivergentDeliverableIdError(
            f"{len(present_rungs)} rungs of the carry-or-mint cascade disagree on "
            f"deliverable_id: {diverging} — the rungs of the carry-or-mint cascade "
            "disagree. Per DR-207 DD#1: mint once at the earliest artifact and carry "
            "it verbatim; the EARLIEST artifact's id wins — resolve by hand which "
            "artifact came first. This function will not auto-pick a winner (see "
            "DivergentDeliverableIdError's own docstring)."
        )

    dlvr_id = plan_dlvr_id or predecessor_dlvr_id

    if not dlvr_id and plan_active:
        # Windows-portability: paths are interpolated PLAIN, never `!r` --
        # `repr()` doubles each backslash in a Windows path, which breaks a
        # caller's plain `path in message` substring check on Windows only
        # (POSIX paths have no separator to escape, so it is invisible there).
        # Same fix, same reason as the DivergentDeliverableIdError message
        # above; ids keep `!r` since they contain no separators.
        raise DroppedDeliverableJoinError(
            f"active plan '{plan_file}' names no deliverable_id (absent field, "
            "unreadable file, or literal `null` — all indistinguishable at this layer), "
            "and the predecessor handoff fallback also yielded nothing — refusing to "
            "silently mint-from-slug under an active plan"
        )

    if not dlvr_id and predecessor_is_plan_input:
        raise DroppedDeliverableJoinError(
            f"predecessor '{predecessor}' arrived on the predecessor axis as a plan "
            "input and names no deliverable_id (absent field, unreadable file, or "
            "literal `null` — all indistinguishable at this layer) — refusing to "
            f"silently mint-from-slug; add deliverable_id to '{predecessor}'"
        )

    if dlvr_id:
        result, path_label = mint(deliverable_id=dlvr_id)
    else:
        mint_slug = work_slug or f"{datetime.date.today().strftime('%Y%m%d')}-{slug_suffix}"
        result, path_label = mint(slug=mint_slug)

    label_text = {
        "carry": f"carry path — using existing id: {result}",
        "mint-from-slug": f"mint-from-slug path — minted: {result}",
    }.get(path_label, f"{path_label} path — {result}")
    print(f"handoff-deliverable-carry: {label_text}", file=sys.stderr)

    initiative_id = read_frontmatter_field(plan_file or "", "initiative")
    if not initiative_id and predecessor and os.path.isfile(predecessor):
        initiative_id = read_frontmatter_field(predecessor, "initiative")

    return result, initiative_id

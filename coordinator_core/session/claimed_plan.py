"""
coordinator_core.session.claimed_plan — resolve the plan(s) THIS session has
claimed, so a downstream artifact (``/handoff``, the commit-trailer producer)
can discover a parent ``deliverable_id`` from context instead of minting a
fresh one.

Two entrypoints, two different contracts:

  - ``resolve_claimed_plan_path`` — the pre-existing single-value resolver.
    Two-tier resolution, in order:

      (a) ``<git-common-dir>/coordinator-sessions/<sid>/session-shape.json``'s
          ``plan.path`` field — the fast path, written best-effort by
          ``claims.claim_plan``'s ``session_shape_set`` call at claim time —
          returned ONLY when tier (b) still holds a claim on that same path
          (``_backed_by_claim``). Tier (a) keeps its precedence; what it lost
          is the ability to answer alone.
      (b) Fallback: every plan claim THIS session holds (via
          ``list_held_plan_claims``), earliest-``claimed_at``-first.

    Tier (a)'s validation against tier (b) is not defensive tidiness: the
    shape pointer outlives the claim on two real paths — an explicit
    ``release-artifact plan`` (now also unwritten at the source by
    ``claims._clear_shape_plan_pointer``, this being the read-side backstop)
    and a session that died mid-plan, which nothing unwinds. Tier (a) is read
    first and wins, so either left a shipped or abandoned plan resolving as
    the session's active one, and ``/handoff`` after a completed plan failed
    loud with ``DivergentDeliverableIdError`` — a handoff chain spanning
    several plans carries an id differing from every one of them, so the two
    rungs disagreeing is the expected steady state, not the anomaly the error
    text implies (doe-claude-em memo, 2026-08-10, with repro).

    Tier (b) exists, and is load-bearing (not polish), because the tier (a)
    write is DOCUMENTED best-effort: ``claim_plan`` wraps the
    ``session_shape_set`` call in a bare ``try/except Exception: pass``
    (non-fatal by design — a shape-write failure must never break the plan
    claim itself) and skips the write entirely whenever
    ``core.resolve_session_id`` returns falsy. The claim directory itself —
    written unconditionally by ``claim_artifact("plan", ...)`` before the
    shape write is even attempted — is therefore the more durable record.

  - ``list_held_plan_claims`` / ``resolve_claimed_plan_path_for_trailer`` —
    the NEW enumeration this module adds (spec: C1a of
    docs/plans/2026-08-10-a-commit-trailer-that-names-the-session.md).
    ``resolve_claimed_plan_path``'s tier-(a)-first precedence is UNCHANGED
    for its three existing callers (``baton_assemble/__init__.py`` lineage
    resolution, ``ops/handoff_normalize.py``, the hook) — see that
    function's own docstring. The commit-trailer producer (C2, the sole
    consumer of the two new entrypoints) needs "how many claims does this
    session hold, and if not more than one, which" — a question tier (a)'s
    fast-path shortcut cannot answer, since it only ever names ONE plan
    regardless of how many claims are actually held.

Spec backlink: pln-carry-the-plan-s-deliverable-i-e81513 § C1a
(DR-207 DD#1: mint a stable ``deliverable_id`` once at the earliest artifact and carry it
verbatim; ``/handoff`` derives from the session's active/predecessor plan and mints only
when no parent id is discoverable from context — this module is that discovery step.)
Second spec backlink: pln-a-commit-trailer-that-names-th-ce8a2e § C1a
(the commit-trailer producer needs the FULL set of held plan claims, not one arbitrary pick).

Negative-spec:
    - Do NOT re-derive the ``plan-claims/`` path convention (a literal
      ``"plan-claims"`` join, or a hand-rolled ``<common_dir>/coordinator-sessions/
      plan-claims"``) — import and reuse
      ``coordinator_core.ops.fleet._common.plan_claim_dir`` instead. A second
      hand-rolled copy of that convention is exactly the class of bug
      ``plan_claim_dir``'s own docstring warns against.
    - Do NOT hoist that ``plan_claim_dir`` import to module scope. Anything
      under ``coordinator_core.ops`` triggers that package's eager import of
      all ~161 op modules, four of which import back into this one
      (``handoff_normalize``, ``handoff_author_fork``, ``handoff_correct_body``,
      ``queue_scaffold_baton``). At module scope the cycle leaves this module
      partially initialized mid-sweep and those four ops silently fail to
      register — the failure is a warning on stderr, not an exception, so it
      is easy to ship. Keep the import inside the function body that needs it
      (``list_held_plan_claims``, the only place in this module that imports it).
    - Do NOT raise when no plan is claimed — that is an ordinary, legitimate
      state (a handoff genuinely unattached to a plan), not an error. Every
      failure edge (unresolvable session id, unresolvable git root, missing/
      malformed ``session-shape.json``, missing/unreadable claim dirs) falls
      through to returning ``None`` (``resolve_claimed_plan_path`` /
      ``resolve_claimed_plan_path_for_trailer``) or ``[]``
      (``list_held_plan_claims``) — never an exception.
    - Do NOT invent a second ordering primitive for multi-claim ties. The
      ``claimed_at`` file (ISO8601, written once by
      ``session.claims._write_claim_meta`` at claim time, never mutated) is
      the one durable ordering fact; this module's earliest-first sort
      mirrors ``baton_assemble/__init__.py::_resolve_held_handoff_for_session``,
      the handoff-claims analogue of this exact problem, including its
      PER-CLAIM (not set-wide) degradation when a held claim lacks a
      readable ``claimed_at`` (2026-08-13, sedge-15) — a missing timestamp
      sorts that ONE claim via the high sentinel, it does not collapse the
      whole set to name order.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional
from coordinator_core.session import core, shape

# ``plan_claim_dir`` is imported lazily inside ``list_held_plan_claims``, NOT at
# module scope. ``coordinator_core.ops`` eagerly imports all ~161 op modules on
# first touch, four of which reach back here — a module-scope import of anything
# under ``coordinator_core.ops`` makes this module partially-initialized during
# that sweep and silently DEREGISTERS those four ops. See the negative-spec.


#: Where an archived plan lands, in resolution order. `fleet.archive_completed_
#: plans` git-mv's a terminal plan into `archive/specs/<YYYY-MM>/`;
#: `archive/completed/<YYYY-MM>/` is the older destination still holding plans
#: swept before that op existed. Both are exactly ONE level deep (a `YYYY-MM`
#: directory), which is why the lookup below globs `*/<slug>.md` rather than
#: recursing -- an `rglob` over both trees is ~1000 stat calls on this repo to
#: answer a question a two-entry glob answers exactly.
#:
#: `archive/handoffs` is deliberately NOT here, though `pickup_assemble.
#: ARCHIVE_DIRS` includes it for its own basename walk. A plan slug and a
#: handoff basename collide often -- 7 of this repo's claimed slugs match a
#: file under `archive/handoffs` as well -- so searching it would hand back a
#: HANDOFF as a session's claimed PLAN. Restricted to these two, the same
#: corpus resolves 34 of 34 archived claims with zero ambiguity (measured
#: 2026-09-01). Do not widen this tuple to reuse `ARCHIVE_DIRS`; the narrower
#: set is the correctness property, not an optimization.
_PLAN_ARCHIVE_DIRS = ("archive/specs", "archive/completed")


def _resolve_plan_slug_path(root: Optional[str], slug: str) -> str:
    """Repo-relative path of the plan named by ``slug``, wherever it now lives.

    The claim store records a SLUG, never a location: ``plan-claims/<slug>/``.
    Every reader before this helper rebuilt the location by hand as
    ``f"docs/plans/{slug}.md"``, which is right until the plan is archived --
    and ``fleet.archive_completed_plans`` archives on terminal status ALONE, so
    a session holding a live claim can have its plan moved out from under it.
    The claim stays valid and correct; only the assumed location goes stale.
    Measured on this repo 2026-09-01: 44 of 79 plan claims named a
    ``docs/plans/`` path with no file behind it, and 34 of those 44 were a real
    plan sitting in ``archive/specs/``.

    That mattered because ``baton_assemble``'s mint stamps ``governing_plan``
    from this value: a baton got an edge to a path resolving to nothing,
    silently (reported by example-game-repo-em, 2026-09-01, as a follow-up on memo
    ``plan-baton-link-not-required``).

    Resolution order: the live ``docs/plans/`` path when a file is there, then
    each ``_PLAN_ARCHIVE_DIRS`` entry in order. Multiple hits inside one
    archive dir take the lexicographically first -- unobserved on this corpus,
    and a stable pick beats an arbitrary one.

    UNRESOLVABLE FALLS BACK TO THE LIVE PATH, deliberately: this function's
    contract is "a repo-relative string, always", matching what every caller
    already receives, so a slug naming nothing (a scaffold-and-discard, a test
    fixture left in the store) returns ``docs/plans/<slug>.md`` exactly as
    before. Deciding what an unresolvable claim MEANS is the caller's job --
    see ``baton_assemble``'s own existence check before it stamps
    ``governing_plan``. Returning ``None`` here would push that judgment into a
    resolver whose never-raises/always-a-string contract three call sites
    depend on.
    """
    live = f"docs/plans/{slug}.md"
    if not root:
        return live
    root_path = Path(root)
    if (root_path / live).is_file():
        return live
    for archive_dir in _PLAN_ARCHIVE_DIRS:
        base = root_path / archive_dir
        if not base.is_dir():
            continue
        try:
            hits = sorted(base.glob(f"*/{slug}.md"))
        except OSError:
            continue
        if hits:
            return hits[0].relative_to(root_path).as_posix()
    return live


def list_held_plan_claims(
    cwd: str | Path | None = None,
) -> list[tuple[str, Optional[str]]]:
    """Every plan claim THIS session holds, as ``[(plan_path, claimed_at), ...]``,
    in claim order (earliest first) — the C1a enumeration
    (docs/plans/2026-08-10-a-commit-trailer-that-names-the-session.md § C1a,
    AC1). Reads the durable claim-record store directly
    (``<sessions_dir>/plan-claims/<slug>/session_id`` +
    ``.../claimed_at``), NEVER the ``session-shape.json`` fast-path tier (a)
    consults — that field only ever names ONE plan, so it cannot answer "how
    many claims does this session hold".

    Ordering mirrors ``baton_assemble/__init__.py::
    _resolve_held_handoff_for_session``'s established precedent for the
    handoff-claims analogue of this exact problem, PER-CLAIM (2026-08-13,
    sedge-15, bringing tier (b) onto DR-291's own per-claim discipline):
    each held claim sorts on ``(claimed_at or high-sentinel, basename)`` —
    a claim with a known ``claimed_at`` always outranks one without,
    regardless of what else is in the set. A single claim missing
    ``claimed_at`` therefore no longer collapses the WHOLE set to name
    order (the pre-2026-08-13 behavior); only that one claim's own position
    degrades to "sorts after every known timestamp, tied on basename
    against any other unknown-timestamp claim". This module does not carry
    a degradation SIGNAL of its own (unlike the baton-side resolver's
    ``degraded`` return, AC-6) — ``resolve_claimed_plan_path``'s ``str |
    None`` / never-raises contract has no route to surface one without
    widening that boundary (AC-7; see that function's own docstring).

    Returns ``[]`` — never raises — whenever no plan is claimed, the session
    id is unresolvable, or the repo's git-common-dir is unresolvable (e.g.
    not inside a git repository).
    """
    cwd_str = str(cwd) if cwd is not None else None

    sid = core.resolve_session_id(cwd_str)
    if not sid:
        return []

    common = core.sessions_dir(cwd_str)
    if not common:
        return []
    common_dir = Path(common).parent

    # Imported HERE, not at module scope — see the module docstring's
    # negative-spec. ``plan_claim_dir`` returns
    # ``<...>/plan-claims/<plan_path.stem>``; a throwaway stem's ``.parent``
    # recovers the ``plan-claims/`` base dir without re-deriving that
    # convention by hand.
    from coordinator_core.ops.fleet._common import plan_claim_dir

    plan_claims_dir = plan_claim_dir(common_dir, Path("placeholder")).parent
    if not plan_claims_dir.is_dir():
        return []
    try:
        entries = sorted(plan_claims_dir.iterdir())
    except OSError:
        return []

    # Resolved ONCE, above the loop. `core.git_root` shells out to
    # `git rev-parse --show-toplevel`, so calling it per claim would be a
    # per-item git spawn inside an enumeration -- the exact shape
    # `coordinator_core/tests/test_no_unbatched_per_item_git_spawn.py` exists
    # to refuse, and a session holding a dozen claims would pay a dozen
    # process creations (~25ms each) to answer one question.
    repo_root = core.git_root(cwd_str)

    held: list[tuple[str, Optional[str]]] = []
    for entry in entries:
        if not entry.is_dir():
            continue
        try:
            held_sid = (entry / "session_id").read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if held_sid != sid:
            continue
        claimed_at: Optional[str] = None
        try:
            claimed_at = (entry / "claimed_at").read_text(encoding="utf-8").strip() or None
        except OSError:
            claimed_at = None
        held.append((_resolve_plan_slug_path(repo_root, entry.name), claimed_at))

    if not held:
        return []

    # Per-claim sentinel sort (2026-08-13, sedge-15): a claim with a known
    # `claimed_at` always outranks one without — a missing timestamp sorts
    # via the high sentinel, tied on basename against any other
    # unknown-timestamp claim, rather than collapsing the WHOLE set to name
    # order the way the pre-2026-08-13 set-wide `all(...)` gate did. Mirrors
    # `baton_assemble/__init__.py::_resolve_held_handoff_for_session`'s own
    # per-claim `claimed_at_or_sentinel` leg (DR-291).
    _SENTINEL = "￿"
    held.sort(key=lambda pair: (pair[1] or _SENTINEL, pair[0]))

    return held


def resolve_claimed_plan_path_for_trailer(cwd: str | Path | None = None) -> Optional[str]:
    """The single plan path for the commit-trailer producer (C2, its sole
    consumer) — a thin wrapper over ``list_held_plan_claims``, not a
    parallel resolver. Zero claims -> ``None``. One claim -> that claim's
    path. More than one -> the earliest-``claimed_at`` claim's path (same
    tie-break ``list_held_plan_claims`` itself orders by). C2's ambiguity
    gate (does the session hold more than one claim) reads
    ``list_held_plan_claims`` directly rather than this function — this
    function only ever names ONE path, never a count.

    Never raises; ``None`` on every unresolvable-context edge
    ``list_held_plan_claims`` itself falls through to ``[]`` on.
    """
    held = list_held_plan_claims(cwd)
    if not held:
        return None
    return held[0][0]


def _backed_by_claim(path: str, held: list[tuple[str, Optional[str]]]) -> bool:
    """Does ``path`` (a tier-(a) ``session-shape.json`` pointer) still have a
    claim behind it in tier (b)?

    A shape pointer with no backing claim is stale BY DEFINITION: ``claims.
    claim_plan`` is the only writer of ``plan.path``, and it writes the claim
    dir first, unconditionally, before attempting the best-effort shape write.
    The claim store is therefore a superset of what the shape file can
    legitimately name — a pointer outside it outlived its own claim.

    Compares on the SLUG (basename minus ``.md``), not on the full path, and
    not on filesystem identity. Both sides are repo-relative strings minted
    inside this package, but they no longer agree on the DIRECTORY: tier (a)'s
    pointer is whatever ``claim_plan`` wrote at claim time (always
    ``docs/plans/<slug>.md``), while tier (b) now reports where the plan
    actually lives, which is an ``archive/specs/`` path once the plan is
    archived (``_resolve_plan_slug_path``). A full-path comparison would call
    every archived plan's shape pointer unbacked and silently demote tier (a),
    which is precedence this function exists to preserve, not to relitigate.
    The slug is the claim store's own key, so comparing on it compares the
    thing the two tiers genuinely share.

    A stat-based comparison stays wrong for the reason it always was: it would
    additionally reject a plan file legitimately renamed or deleted after being
    claimed.
    """
    if not held:
        return False

    def _slug(value: str) -> str:
        tail = value.replace("\\", "/").strip().rsplit("/", 1)[-1]
        return tail[:-3] if tail.endswith(".md") else tail

    norm = _slug(path)
    return any(_slug(claimed) == norm for claimed, _ in held)


def resolve_claimed_plan_path(cwd: str | Path | None = None) -> str | None:
    """Repo-relative path of the plan this session has claimed, or ``None``.

    Resolves THIS session's id via ``core.resolve_session_id`` (the canonical
    4-tier chain — no second precedence ladder is added here), then tries
    tier (a) ``session-shape.json`` — accepted only when tier (b) still holds
    a claim on that same path — before falling back to tier (b) the
    ``plan-claims/`` directory scan (see module docstring for why tier (b)
    is necessary, not merely defensive, and why an unbacked tier-(a) hit is
    stale by definition rather than a competing opinion).

    Returns ``None`` — never raises — whenever no plan is claimed, the
    session id is unresolvable, or the repo's git-common-dir is
    unresolvable (e.g. not inside a git repository).
    """
    cwd_str = str(cwd) if cwd is not None else None

    sid = core.resolve_session_id(cwd_str)
    if not sid:
        return None

    common = core.sessions_dir(cwd_str)
    if not common:
        return None

    # ---- Tier (b) read, hoisted: also the validator for tier (a) below ----
    held = list_held_plan_claims(cwd_str)

    # ---- Tier (a): session-shape.json plan.path, VALIDATED against tier (b) ----
    raw = shape.session_shape_read(sid, cwd_str)
    try:
        parsed = json.loads(raw)
    except ValueError:
        parsed = None
    if isinstance(parsed, dict):
        plan_field = parsed.get("plan")
        if isinstance(plan_field, dict):
            path = plan_field.get("path")
            if isinstance(path, str) and path and _backed_by_claim(path, held):
                # Re-resolved, never returned verbatim. The shape pointer is
                # frozen at claim time and always names `docs/plans/`, so
                # handing it back would reintroduce the stale-location defect
                # on this tier alone -- tier (a) would answer with a dead path
                # for exactly the archived plans tier (b) now resolves
                # correctly. Precedence is unchanged; only the location is
                # refreshed.
                return _resolve_plan_slug_path(
                    core.git_root(cwd_str), Path(path).stem
                )

    # ---- Tier (b): every plan claim this session holds, earliest first ----
    # Delegates to `list_held_plan_claims` rather than re-scanning
    # `plan-claims/` by hand — this is the ONE behaviour change this chunk
    # (C1a) makes to `resolve_claimed_plan_path`: the N>1 tie-break becomes
    # deterministic (earliest `claimed_at`) instead of arbitrary
    # (alphabetical-by-slug). Tier (a)'s precedence above is untouched, and
    # the N<=1 return value is byte-identical to the prior hand-rolled scan.
    held = list_held_plan_claims(cwd_str)
    if not held:
        return None
    return held[0][0]

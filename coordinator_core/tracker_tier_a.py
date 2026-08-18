"""
coordinator_core.tracker_tier_a — Tier-A wiring: read commit-closure
evidence from ``cockpit-emission.json``, reduce it into
``CodeCompleteEvidence``, and ingest it into the sovereign tracker's
``code_complete`` axis (sat-07, chunk C2 — reader + ingest seam, merged
from the original C2+C4 per review finding F9).

Spec backlink: docs/plans/2026-08-18-sat-07-tier-a-wiring.md § Chunk C2,
D2, D3, D5, D6, D8, AC3-AC11, AC14, AC15, AC19.

Flat module locus (D2) — NOT ``coordinator_core/ops/tracker/``, which is a
live sibling plan's file scope and, per DR-318 §D2, the wrong architecture
for this family of modules regardless of that collision. This module
registers NO op — the op surface is sat-06's remit.

Negative-spec (mirrors ``tracker_completion_policy.py``'s own, AC7/AC15/D5):
  - Zero ``subprocess``/git calls anywhere in this module (D5, AC7). All
    git work already happened at emit time inside
    ``coordinator_core/ops/emit/sections/commit_closures.py`` — this
    module only ever reads whatever ``cockpit-emission.json`` currently
    holds on disk, and never calls ``artifact.emit`` to freshen it first
    (that would add the exact ``git log``/reachability spawn the per-op
    budget forbids).
  - No module-scope ``import coordinator_core.ops`` or
    ``import coordinator_core.contract`` (F2, AC15 — asserted by a
    source-level AST walk over this module's top-level imports, not a
    whole-tree import claim). ``envelope.DEFAULT_OUTPUT_NAME`` is
    resolved via a FUNCTION-LOCAL deferred import inside
    ``read_commit_closures``, mirroring ``tracker_transitions.py``'s own
    deferred-import discipline for ``ops.emit._slug.machine_slug``. This
    does NOT make a whole-tree import of this module clean —
    ``tracker_completion_policy``, which this module must import for
    ``CodeCompleteEvidence``/``classify_code_complete_tier``/
    ``emit_code_complete_assert``/``detect_symmetric_retract``, already
    reaches ``coordinator_core.ops.*`` transitively via
    ``tracker_transitions`` -> ``tracker_projection`` ->
    ``tracker_entities``. That transitive gap belongs to two filed,
    still-open bug rows
    (``state/bug-backlog/2026-08-18-tracker-mint-person-silently-fails-to-re-ff3202163c62.yaml``,
    ``state/bug-backlog/2026-08-05-tracker-store-s-ops-package-import-cycle-0e1d8d681251.yaml``),
    not this plan's — AC15 asserts only the source-level rule stated
    above, never that gap.
  - Do NOT use ``tracker_entities._require_local_item`` to build the
    local item_id set, even though it is exactly the predicate this seam
    needs (an executor will find it and reach for it — G8). Importing
    ``tracker_entities`` from this module at module scope would pull in
    ``ops.ceremony.completion_entry``/``ops.emit._slug`` transitively at
    IMPORT time, which is precisely what the module-scope rule above
    forbids. ``tracker_store`` is a safe dependency instead (its only
    intra-package import is ``coordinator_core.locked_write``), so
    ``_local_item_ids`` below scans ``tracker_store.read_events``
    directly for ``kind == "item_created"``.
  - Do NOT read ``contract.cockpit_schema.entities.commit_closure.
    CommitClosure`` here. This module reads a JSON dict off disk; the
    pydantic entity buys nothing at read time and imports across the
    same cycle boundary AC15 guards.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from coordinator_core import tracker_store, tracker_transitions
from coordinator_core.tracker_completion_policy import (
    CodeCompleteEvidence,
    classify_code_complete_tier,
    detect_symmetric_retract,
    emit_code_complete_assert,
)

_AUTO_TIER = "auto"


def read_commit_closures(repo_root: Path) -> tuple[list[dict], float | None]:
    """Read ``<repo_root>/state/cockpit-emission.json`` and return its
    ``commit_closures`` array plus the artifact's own mtime (D5/AC14 — the
    CALLER records staleness against that mtime; this reader never hides
    it and never triggers a fresh emit to avoid it).

    Fail-open to ``([], None)`` on a missing file, any ``OSError``, a
    malformed/non-JSON file (``json.JSONDecodeError``, a subclass of
    ``ValueError``), an envelope missing the ``commit_closures`` key or
    shaped as something un-subscriptable, or a ``commit_closures`` value
    that is not a list (AC3) — mirroring ``commit_closures.py``'s own
    fail-open porter posture.

    Resolves the artifact's filename from ``envelope.DEFAULT_OUTPUT_NAME``
    via a function-local deferred import — see the module docstring's
    negative-spec for why this must never be a module-scope import or a
    hand-copied string literal.
    """
    from coordinator_core.ops.emit.envelope import DEFAULT_OUTPUT_NAME

    path = Path(repo_root) / "state" / DEFAULT_OUTPUT_NAME
    try:
        mtime = path.stat().st_mtime
        envelope = json.loads(path.read_text(encoding="utf-8"))
        closures = envelope["commit_closures"]
    except (OSError, ValueError, KeyError, TypeError):
        return [], None
    if not isinstance(closures, list):
        return [], None
    return closures, mtime


def _reduce_close_row(item_id: str, closures: list[dict]) -> dict | None:
    """Shared reduction behind ``evidence_for_item`` (and the ingest
    seam's ``source_observation_id`` derivation, which needs the winning
    ROW, not just the evidence built from it) — one place, so the two
    never drift apart.

    Filters to CLOSE rows only (``reverts_sha`` is ``None`` — a revert row
    is never returned as evidence for an assert, AC4/H2) matching
    *item_id* via ``==`` on the raw strings, no normalization (see this
    module's own negative-spec / the plan's Anti-scope). Prefers a row
    with ``reachable_on_default_branch is True``; else the most recent
    indeterminate/false row.

    ``commit_closures`` rows ride ``git log``'s default newest-first
    commit order (see ``_extract_closure_commits`` in
    ``ops/emit/sections/commit_closures.py``, which runs no ``--reverse``)
    — this module inherits that order rather than asserting one of its
    own, so "most recent" resolves to the FIRST matching row encountered
    in the array as received.
    """
    close_rows = [
        row
        for row in closures
        if row.get("reverts_sha") is None and item_id == row.get("item_id")
    ]
    if not close_rows:
        return None
    for row in close_rows:
        if row.get("reachable_on_default_branch") is True:
            return row
    return close_rows[0]


def evidence_for_item(
    item_id: str, closures: list[dict]
) -> CodeCompleteEvidence | None:
    """Reduce *closures* to one ``CodeCompleteEvidence`` for *item_id*'s
    ``code_complete`` ASSERT arm, or ``None`` if no close row matches
    (AC4). ``None`` is what keeps a caller at ``tier: "suggest"`` via
    ``classify_code_complete_tier``.

    AC5a: ``trailer_bound=True`` is set unconditionally on any returned
    close row. This is not a second check — the row match performed by
    ``_reduce_close_row`` (a case-sensitive, unnormalized ``==`` of
    *item_id* against ``row["item_id"]``) IS the binding comparison
    sat-04's ``CodeCompleteEvidence`` docstring requires ("a case-sensitive
    EXACT match of ``item.id`` against the commit's ``Closes:`` trailer
    value"): a row existing under that match and the trailer being bound
    are the same fact, not two. The binding is exact-match against the
    WHITESPACE-TRIMMED trailer value specifically — ``ops/emit/sections/
    commit_closures.py``'s ``_extract_closure_commits`` strips each
    trailer line (``.strip()``) before it ever reaches
    ``_TRAILER_PATTERNS``, and that pattern's own ``^\\s*(...)\\s*$``
    strips again — so by the time a row's ``item_id`` lands in
    ``commit_closures``, it has already been trimmed twice upstream of
    this function; this function performs no normalization of its own
    (Anti-scope) precisely because that trimming already happened.

    ``reachable_on_default_branch`` passes through as the tri-state it is
    — never coerced to a bool, never ``None`` collapsed to ``False``
    (AC6).
    """
    row = _reduce_close_row(item_id, closures)
    if row is None:
        return None
    return CodeCompleteEvidence(
        sha=row.get("sha"),
        trailer_bound=True,
        reachable_on_default_branch=row.get("reachable_on_default_branch"),
    )


def _reduce_revert_row(item_id: str, closures: list[dict]) -> dict | None:
    """Shared reduction behind ``revert_evidence_for_item`` and the ingest
    seam's revert-side ``source_observation_id`` derivation.

    Filters to MARKED REVERT rows (``reverts_sha`` non-null, C3's marker)
    matching *item_id*. A revert row that is not
    ``reachable_on_default_branch is True`` is DROPPED rather than
    preferred-over (AC4b) — ``detect_symmetric_retract`` refuses anything
    short of that same gate, so carrying an unreachable/indeterminate
    revert row forward here would only ever be thrown away one layer up.
    Returns the first (newest, per ``_reduce_close_row``'s ordering note)
    reachable revert row, or ``None`` if none qualifies.
    """
    revert_rows = [
        row
        for row in closures
        if row.get("reverts_sha") is not None and item_id == row.get("item_id")
    ]
    for row in revert_rows:
        if row.get("reachable_on_default_branch") is True:
            return row
    return None


def revert_evidence_for_item(
    item_id: str, closures: list[dict]
) -> CodeCompleteEvidence | None:
    """Reduce *closures* to one ``CodeCompleteEvidence`` for *item_id*'s
    revert arm (AC4b) — a SEPARATE reduction from ``evidence_for_item``,
    never sharing its close-row result.

    AC5b (D8/G5): ``trailer_bound=True`` is set here for the TRANSITIVE
    reason D8 states, NOT the assert arm's "row match IS the binding"
    justification (AC5a), which does not hold on this arm — a revert
    commit carries no ``Closes:`` trailer of its own. The transitive fact:
    the commit this row's own ``sha`` verifiably reverts (C3's join,
    ``reverts_sha``, git's own auto-generated "This reverts commit ..."
    body line) was itself exact-match trailer-bound to *item_id* — that is
    how the joined-against close row was produced in the first place — and
    this row's presence in ``closures`` (filtered by ``_reduce_revert_row``
    to rows carrying that marker) is what lets this function read that
    transitivity off the row rather than re-deriving it.

    Only a row with ``reachable_on_default_branch is True`` is ever
    returned (``_reduce_revert_row`` already dropped every other
    candidate) — so this function's ``CodeCompleteEvidence`` is always
    built with ``reachable_on_default_branch=True``, never a tri-state
    pass-through the way ``evidence_for_item``'s is.
    """
    row = _reduce_revert_row(item_id, closures)
    if row is None:
        return None
    return CodeCompleteEvidence(
        sha=row.get("sha"),
        trailer_bound=True,
        reachable_on_default_branch=True,
    )


def _local_item_ids(repo_root: Path) -> set[str]:
    """The set of item ids this REPO's own tracker store created
    (``kind == "item_created"``), scanned directly off
    ``tracker_store.read_events`` — see the module docstring's
    negative-spec for why ``tracker_entities._require_local_item`` is not
    used here despite being the ready-made predicate.
    """
    return {
        event["item_id"]
        for event in tracker_store.read_events(repo_root=repo_root)
        if event.get("kind") == "item_created" and "item_id" in event
    }


def _has_stored_code_complete_assert(repo_root: Path, item_id: str) -> bool:
    """Direct-shard scan for a PRIOR ``code_complete`` assert for *item_id*
    — mirrors the test module's own ``_shard_events``/``_code_complete_
    events`` read shape rather than routing through ``tracker_store.
    read_events``, which FILTERS to ``applied_at``-populated events and
    would silently hide a stored assert whose ``applied_at`` happens to be
    null (never true for an auto-tier assert today, per D6, but this scan
    must not depend on that invariant holding forever to stay correct).

    Feeds the retract-emission gate in ``ingest_commit_closures``: a
    revert-driven retract carries a hardcoded ``from_state="asserted"``
    (``detect_symmetric_retract``), and the transition-event log it lands
    in is APPEND-ONLY — a wrong ``from_state`` can never be edited or
    deleted out once written. ``detect_symmetric_retract``'s own gate only
    inspects the REVERT row's ``trailer_bound``/``reachable_on_default_
    branch``; it has no visibility into whether the item's CLOSE row ever
    actually classified ``"auto"``, this run or any prior one. Without this
    scan, a close row that never qualifies (e.g. the completing commit was
    never merged to the default branch) paired with a revert row that DOES
    qualify (`git revert <sha>` only needs the completing sha in the
    object DB, not on the default branch) would emit a retract for an item
    that has NO assert anywhere in the log — permanently polluting the
    audit trail with a fact that never happened. Scanning the shards
    directly, rather than trusting only this run's own emission, is what
    catches the case where the qualifying assert landed in a run that
    already completed.
    """
    shard_dir = repo_root / tracker_store.EVENTS_DIR_RELPATH
    if not shard_dir.is_dir():
        return False
    for shard_file in sorted(shard_dir.glob(tracker_store.EVENTS_SHARD_GLOB)):
        for line in shard_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if not isinstance(record, dict):
                continue
            if (
                record.get("axis") == "code_complete"
                and record.get("item_id") == item_id
                and record.get("to_state") == "asserted"
            ):
                return True
    return False


def _source_observation_id(row: dict) -> str:
    """Stable, deterministic content address for *row* over its declared
    logical identity ``(repo, item_id, sha)`` (AC19) — NOT over
    ``reachable_on_default_branch``, a mutable stamped result rather than
    part of ``CommitClosure``'s own identity (see that entity's docstring).
    Two derivations over an identical row always produce the same id;
    never minted fresh per run (``secrets.token_hex`` or similar), since a
    fresh-per-run id would defeat ``tracker_transitions``'
    ``_dedup_check_address`` on both the assert arm (nearly free there,
    since ``source_observation_id`` does not participate in
    ``_code_complete_dedup_key``'s assert-arm tail) and the retract arm
    (load-bearing there, since that arm's key IS
    ``(source_observation_id, evidence_sha)``).
    """
    canonical = json.dumps(
        [row.get("repo"), row.get("item_id"), row.get("sha")], sort_keys=True
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
    return f"obs-{digest}"


def ingest_commit_closures(repo_root: Path, *, actor: str) -> list[dict]:
    """Decide WHEN a ``code_complete`` observation arrives from
    commit-closure evidence, and emit it (D4's caller, sat-07's remit).

    Derives the item_id set to consider from the INTERSECTION of this
    repo's own local ``item_created`` ids (``_local_item_ids``) and the
    trailer rows' item_ids in *closures* — never from iterating the
    closure rows directly (F6/F7). Iterating the rows alone would make
    ``trailer_bound=True`` a tautology and let a typo'd, stale, or
    foreign-repo trailer mint a ``code_complete`` for an id no local
    ``item_created`` event ever created; intersecting instead makes D3's
    self-repo boundary hold BY CONSTRUCTION (AC8's fourth degradation
    mode) — an item created in a different repo has no local
    ``item_created`` event, so it is never in the intersection and never
    produces an event here, regardless of what a foreign repo's trailer
    says.

    Per item id, in order (AC4c — the assert MUST be emitted first:
    ``detect_symmetric_retract`` hardcodes ``from_state="asserted"``, so a
    retract preceding its own assert in the same run would write a
    ``from_state`` the log never actually passed through):

      1. Close arm: reduce ``evidence_for_item``. Classify it via
         ``classify_code_complete_tier`` BEFORE emitting anything — D6
         forbids emitting for suggest-classified evidence (no cursor, no
         idempotency key, no store-side change; a stored suggest event is
         permanently invisible to ``tracker_store.read_events`` and would
         poison the later Tier-A upgrade of the same sha via
         ``_code_complete_dedup_key``'s ``tier``-blind assert-arm address
         — see D6 for the full incident this reverses). Only an
         ``"auto"``-classified close row is emitted, via
         ``emit_code_complete_assert`` — ``tier`` is never passed to it;
         the classifier's own output is the only legitimate source.
      2. Revert arm: gated on the item actually having an assert to
         retract — this run's close arm (``asserted_this_run``) or a
         prior run's stored assert (``_has_stored_code_complete_assert``,
         a direct shard scan). Without this gate, ``detect_symmetric_
         retract``'s own check (the revert row's OWN qualification only)
         cannot see whether the item's close row ever qualified, in this
         run or any prior one, and would mint a ``from_state="asserted"``
         into the append-only log for an item with no assert anywhere
         (review finding, not in the original spec). Once gated, reduce
         ``revert_evidence_for_item`` and hand it to
         ``detect_symmetric_retract`` with the REVERT observation's own
         ``source_observation_id`` (never the id of the observation that
         produced the original assert). A ``None`` return means the
         revert did not clear the verification gate — emit nothing, and
         do not log it as an error (AC10). A non-``None`` payload is
         DESTRUCTURED — ``from_state``, ``tier``, ``evidence``,
         ``source_observation_id`` (plus ``item_id``/``axis``/``to_state``
         for the same reason) are read off the returned dict and passed as
         ``tracker_transitions.emit_transition``'s kwargs, never
         re-literalized here (AC9, G4) — ``emit_transition`` constructs a
         payload itself and does not accept a prebuilt one, and
         ``_emit``/``_emit_batch`` are private and not sanctioned to reach
         into.

    ``source_observation_id`` is derived per emitted row via
    ``_source_observation_id`` — over the CLOSE row for the assert arm,
    over the REVERT row for the retract arm — never shared between the
    two arms of the same item.

    Returns every event this run actually emitted or matched via dedup
    (``emit_code_complete_assert``/``emit_transition`` both return the
    existing event on a dedup hit rather than raising) — D6/AC11 rely on
    the STORE's shard contents for idempotency, not on this return value.
    """
    repo_root = Path(repo_root)
    closures, _mtime = read_commit_closures(repo_root)
    if not closures:
        return []

    trailer_item_ids = {
        row.get("item_id") for row in closures if row.get("item_id") is not None
    }
    item_ids = _local_item_ids(repo_root) & trailer_item_ids

    emitted: list[dict] = []
    for item_id in sorted(item_ids):
        close_row = _reduce_close_row(item_id, closures)
        asserted_this_run = False
        if close_row is not None:
            close_evidence = evidence_for_item(item_id, closures)
            if classify_code_complete_tier(close_evidence) == _AUTO_TIER:
                emitted.append(
                    emit_code_complete_assert(
                        item_id,
                        close_evidence,
                        actor=actor,
                        source_observation_id=_source_observation_id(close_row),
                        repo_root=repo_root,
                    )
                )
                asserted_this_run = True
            # D6: suggest-classified evidence emits nothing — no cursor,
            # no idempotency key, no store-side change.

        revert_row = _reduce_revert_row(item_id, closures)
        if revert_row is not None:
            # Retract gate (review finding, not in the original spec): a
            # retract may only be emitted for an item that actually has an
            # assert to retract — this run's own close arm, or a prior
            # run's stored assert (`_has_stored_code_complete_assert`).
            # `detect_symmetric_retract` hardcodes `from_state="asserted"`
            # into an APPEND-ONLY log; without this gate a close row that
            # never qualifies paired with a qualifying revert row would
            # mint a `from_state` the item never actually passed through.
            # See `_has_stored_code_complete_assert`'s docstring.
            if asserted_this_run or _has_stored_code_complete_assert(
                repo_root, item_id
            ):
                revert_evidence = revert_evidence_for_item(item_id, closures)
                payload = detect_symmetric_retract(
                    item_id,
                    revert_evidence,
                    actor=actor,
                    source_observation_id=_source_observation_id(revert_row),
                )
                if payload is not None:
                    emitted.append(
                        tracker_transitions.emit_transition(
                            payload["item_id"],
                            payload["axis"],
                            payload["to_state"],
                            from_state=payload["from_state"],
                            actor=actor,
                            evidence=payload["evidence"],
                            tier=payload["tier"],
                            source_observation_id=payload["source_observation_id"],
                            repo_root=repo_root,
                        )
                    )
                # A None payload means the revert did not clear the
                # verification gate (AC10) — emit nothing, not an error.
            # else: no assert exists anywhere for this item — emit
            # nothing, not an error (mirrors AC10's "no error" posture).

    return emitted

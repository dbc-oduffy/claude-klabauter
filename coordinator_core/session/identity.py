"""
coordinator_core.session.identity — teammate identity resolution.

Port of: identity.sh (coordinator-claude 6fb5fb37, 2026-07-22).

Purpose: subagent-side reconstruction of the canonical EM-side agent id from
the harness-supplied ``agent_id`` (as seen from inside a dispatched
subagent) and that subagent's own ``session_id``. Pure functions — no
filesystem I/O, safe on hot paths (PreToolUse hooks).

FROZEN — Contract #1b (byte-identical port, no behavior changes permitted).
Freeze doc: scratch/subagent-sandbox/bash-to-python-engine-migration/
freeze-coordinator-session-identity.md
Recipe: scratch/subagent-sandbox/bash-to-python-engine-migration/
recipe-t4a-coordinator-session-hub.md § identity.py

Negative-spec: this module does NOT implement the file-backed back-pointer
chain (``agent_id`` -> ``em-session-id.txt`` -> ``dispatched-agents.txt``
column 3, the ``AMBIGUOUS`` sentinel) — that is a DISTINCT mechanism
(``_cs_my_agent_touched`` in the bash hub, ported to ``claims.py`` by a
sibling build agent) and is NOT frozen by Contract #1b. Do not fold it in
here.

Note: two pre-existing, independently-maintained ports of this exact logic
already live in this codebase (``coordinator_core.write_guards.
block_subagent_plan_body_write._resolve_subagent_identity`` /
``_cs_build_canonical_agent_id``, consumed transitively by
``coordinator_core.bash_guards.block_reviewer_bash_outside_allowlist``).
This module is the canonical ``coordinator_core.session`` home the recipe
calls for; it intentionally does NOT import from or replace those existing
call sites (out of scope for this build unit — a follow-up consolidation is
a GIVES-PAUSE candidate, not something this port silently rewires).
"""

from __future__ import annotations

import re
from typing import Any, Dict, Optional

#: CS_CANONICAL_AGENT_ID_RE — single source of truth for the bare-hex
#: unnamed-agent format predicate. Format: lowercase hex, >= 12 chars, no
#: upper bound. Port of ``CS_CANONICAL_AGENT_ID_RE``
#: (coordinator-claude coordinator-session.sh, e34f2484, 2026-07-22).
CANONICAL_AGENT_ID_RE = re.compile(r"^[a-f0-9]{12,}$")

#: Named-teammate grammar: ``a<name>-<16hex>``. The greedy ``(.+)`` capture
#: correctly extracts a dash-containing name because the fixed-length
#: 16-hex suffix anchors the boundary from the right.
_NAMED_TEAMMATE_RE = re.compile(r"^a(.+)-[a-f0-9]{16}$")


def _format_ok(agent_id: str) -> bool:
    """Port of ``_cs_canonical_agent_id_format_ok <id>``.

    Returns True iff ``agent_id`` matches ``CANONICAL_AGENT_ID_RE``.
    Pure function — no filesystem I/O; safe on the hook hot path.

    Uses ``fullmatch``, not ``match``: with ``match``, Python's trailing
    ``$`` anchor also matches immediately before a single trailing
    ``\n``, so ``"abc123456789\n"`` would satisfy ``.match()`` without
    consuming the newline — a narrow parity gap vs bash's ``=~`` (which
    has no such exception). ``fullmatch`` requires the entire string
    consumed, closing that gap. Review: code-reviewer nit.
    """
    return CANONICAL_AGENT_ID_RE.fullmatch(agent_id or "") is not None


def build_canonical_agent_id(name: str, short_session: str) -> str:
    """Port of ``cs_build_canonical_agent_id <name> <short_session>``.

    Returns the canonical EM-side agent id: ``"<name>@session-<short_session>"``.
    Format string is EXACT — literal ``@session-`` infix, no other separators.

    SHARED CONTRACT: this is the ONE place where the teammate canonical-id
    format string is constructed for the subagent-side reconstruction path
    (``resolve_subagent_identity`` below). The EM-side writer
    (``coordinator_core.hooks.track_dispatched_agents``) receives the
    canonical id directly from the harness and records it verbatim — it
    does NOT call this builder.

    Pure function — no filesystem I/O, no side effects.
    """
    if not name:
        raise ValueError("name required")
    if not short_session:
        raise ValueError("short_session required")
    return f"{name}@session-{short_session}"


def resolve_subagent_identity(agent_id: str, session_id: str) -> str:
    """Port of ``resolve_subagent_identity <agent_id> <session_id>``.

    Translates a subagent-side ``agent_id`` to the canonical EM-side id, or
    returns the empty string on no-match (fail-closed). Three paths, in
    order:

    (a) Bare hex ``^[a-f0-9]{12,}$`` — unnamed agent fast path. Returns
        ``agent_id`` unchanged; ``session_id`` is ignored entirely (not
        read, not validated).

    (b) Named teammate ``^a(.+)-[a-f0-9]{16}$`` — the greedy ``(.+)``
        correctly extracts a dash-containing name (e.g.
        ``aprobe2-teammate-64cd7f42c270a899`` -> name = ``probe2-teammate``)
        because the fixed-length 16-hex suffix anchors the boundary from
        the right. Requires ``len(session_id) >= 8`` (strict boundary —
        exactly 8 passes, 7 fails; NOT ``> 8``). On success, returns
        ``build_canonical_agent_id(name, session_id[:8])`` — truncates to
        the first 8 characters, not a hash. On a too-short/absent
        ``session_id``, returns ``""`` (fail-closed). The empty-name branch
        is dead code — ``(.+)`` guarantees a non-empty capture.

    (c) Anything else -> ``""`` (fail-closed — unrecognised shape; "don't
        guess"). Triggers on: empty ``agent_id``, garbage strings, hex
        shorter than 12 chars, uppercase hex (grammar is lowercase-only),
        malformed named-teammate shape.

    Always "returns" successfully — failure is signalled ONLY by an empty
    return value, never an exception (mirrors the bash function's
    always-exit-0 contract).

    Pure function — no filesystem I/O, no side effects; safe on the hook
    hot path.

    Forward-compat caveat (verbatim from the bash source comment): grammar
    (b) is probe-confirmed against harness 2.1.185. A harness change to the
    subagent ``session_id`` shape or the ``a<name>-<16hex>`` prefix must
    update THREE surfaces in lockstep: (1) ``build_canonical_agent_id``,
    (2) this function's grammar, (3) the
    ``^[A-Za-z0-9_.-]+@session-`` value-guard regex in
    ``coordinator_core.hooks.track_dispatched_agents``. Until updated, an
    unrecognised future shape fails closed via path (c), never silently
    mismaps.
    """
    agent_id = agent_id or ""
    session_id = session_id or ""

    # (a) Bare hex — unnamed agent fast path; session_id ignored.
    # fullmatch (not match): closes the trailing-newline-before-`$` gap —
    # see _format_ok's docstring for the mechanism. Review: code-reviewer nit.
    if CANONICAL_AGENT_ID_RE.fullmatch(agent_id):
        return agent_id

    # (b) Named teammate: a<name>-<16hex>
    named = _NAMED_TEAMMATE_RE.fullmatch(agent_id)
    if named:
        name = named.group(1)
        if len(session_id) < 8:
            return ""
        short = session_id[:8]
        return build_canonical_agent_id(name, short)

    # (c) Unrecognised shape — fail-closed.
    return ""


def resolves_em_audience(
    payload: Optional[Dict[str, Any]], git_root: Optional[str]
) -> bool:
    """Positive-EM-audience predicate for guard messages (D3 Branch A of
    ``tasks/guard-messages-keys/DECISIONS.md``, C1a).

    Lands here rather than in ``bash_guards`` because ``bash_guards ->
    session`` is an existing, heavily-used, permitted import edge (dozens of
    module-level and lazy imports across ``bash_guards/dispatch.py``,
    ``dispatch_checks.py``, etc. — verified against the actual import graph,
    not assumed), while ``session -> bash_guards`` is the edge
    ``guard_unlock_sentinel.py``'s own docstring deliberately refuses (the
    reason ``doc_display`` is passed into ``annotate_deny`` rather than
    resolved locally there). Landing the predicate here lets
    ``bash_guards._helpers.operator_override_note`` import it with no new
    edge, and ``session/guard_unlock_sentinel.py`` (C3, next wave) can adopt
    it as an in-package sibling without crossing that refused boundary
    either.

    WHY THE DEFAULT IS INVERTED (DECISIONS.md D1): the prior direction, per
    ``state/audits/2026-08-11-guard-text-injection-mechanism-proof.md``
    § "The fix, and its measurement", was "Absent ``agent_id`` means the
    main/EM session where a human is watching — emit." This predicate
    inverts that: absence of a real envelope, or any resolution failure, now
    degrades to NOT-EM (terse). The inversion is deliberate under the PM's
    2026-08-13 ruling — a survey of every candidate signal available at a
    guard's ``check()`` seam (DECISIONS.md D1 table) found none is
    EM-affirmative, so "observed a real envelope with no agent identity" is
    the closest available positive signal, and "could not observe" must NOT
    collapse into it.

    WHY TWO LEGS, NOT THREE: only ``agent_id`` (canonicalized) and
    ``subagent_type`` (backpointer-resolved) are checked — ``agent_type`` is
    deliberately EXCLUDED, per the spike
    ``docs/research/spike-verdicts/2026-08-08-agent-id-reaches-bash-guards.md``:
    ``agent_type`` is also populated when a session launches with
    ``--agent``, which would misclassify a legitimate EM as not-EM. This
    diverges intentionally from ``bash_guards/_blanket_disarm.py::
    _is_em_caller``, which keeps all three legs — that divergence is
    intentional, not a parity gap to close.

    WHY FORGEABILITY IS NOT A DEFECT HERE: a subagent that sets its own
    ``agent_id``/backpointer state to look EM-shaped could force this
    predicate True. That is out of scope by PM ruling (DECISIONS.md D1):
    these locks exist "not out of safety from malicious attack but because
    doctrine alone cannot keep amnesiac Claudes from machine-degrading or
    otherwise deleterious behavior," and coordinator-claude's own
    ``bash-guard-threat-model.md`` names the actor "an eager subagent, not
    an adversary." A predicate a determined subagent could forge still
    fully discharges that threat model — do not harden it; hardening
    ``resolve_subagent_identity``/backpointer resolution is out of scope for
    this plan.

    Contract (DECISIONS.md D1) — Review: staff-eng flagged this contract as
    previously documenting two post-resolver legs as live discriminators;
    both are unreachable given the raw ``agent_id`` short-circuit below,
    and ``git_root`` is inert at this seam. Corrected to name the one leg
    that actually runs:
      False  if ``payload`` is ``None``, not a ``dict``, or carries no
             ``session_id`` (not a real envelope).
      False  if the RAW ``agent_id`` leg is present and non-empty —
             present-but-unresolvable is treated the same as resolved
             (see "ABSENT VS UNRESOLVABLE" below). This single early
             return is the ONLY leg that actually discriminates: by the
             time the shared resolver runs, ``raw_agent_id`` is already
             known falsy, so ``resolve_effective_types``'s ``agent_id``
             leg (computed only ``if raw_agent_id``) and its
             ``subagent_type`` leg (computed only ``if agent_id and
             git_root``) both resolve empty every time control reaches
             them — the two post-resolver checks below are provably
             unreachable given the raw-``agent_id`` short-circuit above,
             and ``git_root`` is inert at this seam. They are kept as a
             belt-and-braces guard against a future change to
             ``resolve_effective_types`` that would make them live, not
             because they currently discriminate anything.
      False  [unreachable today] if the ``agent_id`` leg resolves
             non-empty.
      False  [unreachable today] if the ``subagent_type`` leg resolves
             non-empty.
      False  on ANY exception during resolution — degrade to terse, never
             to emitting.
      True   only otherwise (a well-formed envelope with the raw
             ``agent_id`` leg empty).

    ABSENT VS UNRESOLVABLE (C1i, tasks/guard-messages-keys/C1i.md):
    ``resolve_effective_types`` -> ``_canonical_agent_id`` silently
    canonicalizes BOTH "no ``agent_id`` key at all" (a genuine EM session)
    AND "an ``agent_id`` present but of an unrecognised shape" (a malformed
    or unresolvable identity) to the same empty string. Left undistinguished,
    that conflation would resolve a malformed ``agent_id`` as EM-class —
    the strongest possible false-positive, and exactly the fail-open
    direction AC-3 forbids, reintroduced one layer below the C1 fix. This
    function therefore re-derives the distinction itself, ahead of calling
    the shared resolver: it reads the RAW ``payload["agent_id"]`` first, and
    if that raw value is present/non-empty, treats it as "cannot resolve"
    (``False``) regardless of what the shared resolver's canonicalization
    does with it — never as "no agent" (which alone is entitled to fall
    through toward ``True``). The fix belongs here, not in
    ``_canonical_agent_id``/``resolve_effective_types`` themselves: those are
    shared identity resolvers with other callers, and their fail-soft-to-
    empty behaviour is load-bearing elsewhere. A future reader collapsing
    this back to "both legs empty -> EM" reopens the hole.

    ``subagent_type`` does NOT need the same treatment: it is never read
    from a raw payload key. It is derived ENTIRELY from the (already
    canonicalized) ``agent_id`` via a backpointer file lookup
    (``resolve_effective_types``: ``subagent_type`` is computed only ``if
    agent_id and git_root``, by ``_read_backpointer_subagent_type``). There
    is no independent raw ``subagent_type`` leg on the payload to be
    "present but unresolvable" — verified by reading
    ``coordinator_core.subagent_sandbox.engine.resolve_effective_types``
    (2026-08-13), not assumed symmetric with ``agent_id``.

    Reuses ``subagent_sandbox.engine.resolve_effective_types`` as the sole
    resolver (imported lazily, inside the function, to avoid adding a
    module-level ``session -> subagent_sandbox`` import edge to this
    otherwise dependency-light module) — this is NOT a fifth identity
    resolver; the plan's census already found four, and this predicate is a
    read-only classification over the SAME resolver's output, not a new
    resolution path.

    Never raises: any exception (malformed ``payload``, resolver failure,
    filesystem error inside the backpointer read) is caught and treated as
    False, matching the "degrade to terse, never to emitting" contract
    above.
    """
    try:
        if not isinstance(payload, dict):
            return False
        if not payload.get("session_id"):
            return False
        raw_agent_id = payload.get("agent_id")
        if raw_agent_id:
            # Present-but-possibly-unresolvable: distinguish from "no
            # agent_id key at all" BEFORE the shared resolver canonicalizes
            # both cases to the same empty string. See "ABSENT VS
            # UNRESOLVABLE" above.
            return False
        from coordinator_core.subagent_sandbox.engine import resolve_effective_types

        agent_id, _agent_type, subagent_type = resolve_effective_types(
            payload, git_root
        )
        if agent_id:
            return False
        if subagent_type:
            return False
        return True
    except Exception:
        return False

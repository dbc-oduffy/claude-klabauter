"""
coordinator_core.session.identity — teammate identity resolution.

Port of: identity.sh (example-doctrine-repo 6fb5fb37, 2026-07-22).

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

#: CS_CANONICAL_AGENT_ID_RE — single source of truth for the bare-hex
#: unnamed-agent format predicate. Format: lowercase hex, >= 12 chars, no
#: upper bound. Port of ``CS_CANONICAL_AGENT_ID_RE``
#: (example-doctrine-repo coordinator-session.sh, e34f2484, 2026-07-22).
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

"""
coordinator_core.ops.session_context — shared session-identity resolver.

Purpose: Provide ``resolve_current_session_id(worktree_root) -> str | None`` — the
canonical single-session identity chain extracted from
``review_trail_write.py:_resolve_session_id`` so all ops that need to identify the
calling session at fork/spawn time share one authoritative definition rather than
each reimplementing the three-tier chain.

Session-id resolution chain (KS-6, 2026-08-07 — widened to the full 3-tier
``coordinator_core.session.core.SESSION_ENV_PRECEDENCE`` ladder, the
documented canonical reference; this module previously read only tiers 2-3
of that ladder, a narrower set than the sibling resolvers it was meant to
agree with — see that constant's docstring for the prior break-class defect
two disagreeing copies caused):
    1. ``COORDINATOR_SESSION_ID`` env var — explicit test override, highest
       precedence.
    2. ``CLAUDE_SESSION_ID`` env var.
    3. ``CLAUDE_CODE_SESSION_ID`` env var.
    4. Returns ``None`` if not resolved.

Negative-spec:
    - Do NOT call ``liveness.resolve_live_session_ids()`` — that function returns a
      ``FrozenSet`` of ALL live sessions (a liveness probe), NOT the current session
      identity. If a caller also needs to assert the resolved session is currently live,
      call ``resolve_live_session_ids()`` separately and check set membership.
    - Do NOT restore a sentinel-file tier (formerly tier 3: reading
      ``{worktree_root}/.git/coordinator-sessions/.current-session-id``). It was removed
      (KS-2, 2026-08-07): the file is documented last-writer-wins
      (``coordinator_core/bash_guards/guard_inprocess_search.py`` ~L84), which is unsound
      under this fleet's ~18 concurrent sessions on one shared worktree — even a freshly
      written sentinel can hand a session the id of whichever session wrote last. Its sole
      writer, ``session-init.py`` (DoE-claude ``SessionStart`` hook), was deleted by PM
      directive 2026-07-15, so the file has no production writer and is frozen residue.
    - Do NOT add tier-4 ambiguity detection — daemon context always supplies session_id via
      env so tier-4 is never reached in practice.

Spec backlink: pln-claude-klabauter-fork-provenance-creatio-01c09f § C3
Extraction source: coordinator_core/ops/review_trail_write.py:_resolve_session_id
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from coordinator_core.session import core as _session_core


def resolve_current_session_id(worktree_root: Optional[Path] = None) -> Optional[str]:
    """Resolve the current session id from env vars.

    Canonical identity chain — delegates to
    ``coordinator_core.session.core.resolve_session_id`` (KS-6, 2026-08-07):
        1. ``COORDINATOR_SESSION_ID`` env var — explicit test override,
           highest precedence.
        2. ``CLAUDE_SESSION_ID`` env var.
        3. ``CLAUDE_CODE_SESSION_ID`` env var.
        4. Returns ``None`` if not resolved.

    Widened from the prior 2-tier (``CLAUDE_SESSION_ID``,
    ``CLAUDE_CODE_SESSION_ID``) chain to match ``core.SESSION_ENV_PRECEDENCE``
    — the canonical reference whose docstring records a prior break-class
    defect caused by exactly two disagreeing copies of this ladder.

    Used by ``handoff.author_fork`` (C3) to populate ``origin_session`` at fork-authoring
    time.  Extracted from ``review_trail_write.py:_resolve_session_id`` so the resolution
    chain is defined once and shared across all consumers (single-definition principle).

    Parameters
    ----------
    worktree_root:
        The repo worktree root (not the git common dir). Unused since the sentinel-file
        tier was removed (KS-2, 2026-08-07); retained in the signature for caller
        compatibility. ``core.resolve_session_id``'s env-var tiers are always tried
        regardless of this value.

    Returns
    -------
    str
        The resolved session id (non-empty string).
    None
        When no tier resolves — caller should treat as "session unknown" rather than raising.

    Negative-spec:
        - Do NOT use ``liveness.resolve_live_session_ids()`` as the identity source — it
          returns a ``FrozenSet[str]`` of ALL live sessions (liveness probe), not the
          current session identity.  Use it ONLY as a separate membership check if you also
          need to assert the resolved session is currently live.
        - Do NOT add tier-4 (now tier-5) ambiguity detection — see the
          ``review_trail_write.py`` module docstring for the rationale (simplified
          sentinel read; daemon context always supplies session_id via env so that tier
          is never reached in practice).
    """
    sid = _session_core.resolve_session_id()
    return sid or None

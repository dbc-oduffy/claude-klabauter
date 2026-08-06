"""
coordinator_core.ops.session_context — shared session-identity resolver.

Purpose: Provide ``resolve_current_session_id(worktree_root) -> str | None`` — the
canonical single-session identity chain extracted from
``review_trail_write.py:_resolve_session_id`` so all ops that need to identify the
calling session at fork/spawn time share one authoritative definition rather than
each reimplementing the three-tier chain.

Session-id resolution chain (strict precedence — mirrors oracle § Session-id resolution):
    1. ``CLAUDE_SESSION_ID`` env var — highest precedence.
    2. ``CLAUDE_CODE_SESSION_ID`` env var.
    3. Sentinel file: ``{worktree_root}/.git/coordinator-sessions/.current-session-id``.
    4. Returns ``None`` if not resolved.

Negative-spec:
    - Do NOT call ``liveness.resolve_live_session_ids()`` — that function returns a
      ``FrozenSet`` of ALL live sessions (a liveness probe), NOT the current session
      identity. If a caller also needs to assert the resolved session is currently live,
      call ``resolve_live_session_ids()`` separately and check set membership.
    - Do NOT implement cs_resolve_session_id tier-4 concurrent-session ambiguity detection
      here — simplified sentinel read; daemon context always supplies session_id via env
      (parity with the oracle's simplified read documented in review_trail_write.py).

Spec backlink: docs/plans/2026-07-07-claude-klabauter-fork-provenance-creation-path-tooling.md § C3
Extraction source: coordinator_core/ops/review_trail_write.py:_resolve_session_id
"""

from __future__ import annotations
import sys

import os
from pathlib import Path
from typing import Optional

# Session-id env vars (precedence order 1 and 2).
# Mirrors the constants in review_trail_write.py — defined here as well so this module
# is self-contained (both callers use the same string values without cross-import).
_CLAUDE_SESSION_ID_ENV: str = "CLAUDE_SESSION_ID"
_CLAUDE_CODE_SESSION_ID_ENV: str = "CLAUDE_CODE_SESSION_ID"


def resolve_current_session_id(worktree_root: Optional[Path] = None) -> Optional[str]:
    """Resolve the current session id from env vars or sentinel file.

    Canonical single-session identity chain (three tiers, strict precedence):
        1. ``CLAUDE_SESSION_ID`` env var — highest precedence.
        2. ``CLAUDE_CODE_SESSION_ID`` env var.
        3. Sentinel file: ``{worktree_root}/.git/coordinator-sessions/.current-session-id``.
        4. Returns ``None`` if not resolved.

    Used by ``handoff.author_fork`` (C3) to populate ``origin_session`` at fork-authoring
    time.  Extracted from ``review_trail_write.py:_resolve_session_id`` so the resolution
    chain is defined once and shared across all consumers (single-definition principle).

    Parameters
    ----------
    worktree_root:
        The repo worktree root (not the git common dir).  Used for the tier-3 sentinel read.
        Pass ``None`` to skip the sentinel tier — tiers 1 and 2 (env vars) are always tried.

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
        - Do NOT add tier-4 ambiguity detection — see the ``review_trail_write.py`` module
          docstring for the rationale (simplified sentinel read; daemon context always
          supplies session_id via env so tier-4 is never reached in practice).
    """
    for env_var in (_CLAUDE_SESSION_ID_ENV, _CLAUDE_CODE_SESSION_ID_ENV):
        val = os.environ.get(env_var, "").strip()
        if val:
            return val
    if worktree_root is not None:
        sentinel = (
            worktree_root / ".git" / "coordinator-sessions" / ".current-session-id"
        )
        try:
            content = sentinel.read_text(encoding="utf-8").strip()
            if content:
                return content
        except OSError:
            print(f"skip: resolve_current_session_id: content = sentinel.read_text(encoding=\"utf-8\").strip() failed: {sys.exc_info()[1]}", file=sys.stderr)
            pass
    return None

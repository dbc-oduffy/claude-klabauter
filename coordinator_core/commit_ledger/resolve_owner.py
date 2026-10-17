"""
coordinator_core.commit_ledger.resolve_owner -- resolves the owning baton
(``handoff_id``) for a commit, including agent commits.

Spec backlink: state/dispatch-briefs/2026-08-19-the-baton-carries-its-commits/C3.md

Own module, not a store concern -- ``store.py`` (C1) owns only the on-disk
ledger shape; this module reads the claim ledger and returns an id, nothing
more.

TWO-HOP resolution, in order:

  1. AGENT HOP. An agent commit lands on its DISPATCHING EM's baton, never
     the agent's own transient identity. Reuses
     ``coordinator_core.session.claim_index._agent_owner_sid`` (the SAME
     agent-dir -> EM-session-id back-pointer reader the claim reverse index
     already uses for the identical translation) rather than a second
     identity ladder -- two disagreeing copies of this resolution is a
     named break-class incident in this repo (KS-6).

  2. BATON HOP. Given the resolved (EM-or-plain) session id, applies
     ``coordinator_core.baton_assemble._resolve_held_handoff_for_session``'s
     THREE-outcome resolution (one held claim / zero held claims / multiple
     held claims, see that function's own docstring for the full four-leg
     ordering key) through ``_primary_held_handoff``, a mirror that takes the
     resolved sid explicitly. The original is not imported: ``baton_assemble``
     is a heavy module the commit landing does not otherwise need. Parity is
     pinned by ``test_resolve_owner_parity``.

Returns the resolved ``handoff_id`` -- the store's own keying unit (C1) --
NOT the ``state/handoffs/<basename>.md`` path
``_resolve_held_handoff_for_session`` returns. ``commit_ledger.store``'s own
``read_chain``/``_legacy_resolve_predecessor_id`` establish the same
convention already (``root / "state" / "handoffs" / f"{handoff_id}.md"``):
``handoff_id`` is basename-minus-``.md``. This module applies that identical
strip, once, rather than re-deriving a second convention.

RAISE CONTRACT -- only a genuinely unresolvable owner raises
``ValueError``:
  - ``committer_id`` is empty.
  - ``committer_id`` names a dispatched agent whose owning-session back-
    pointer (``em-session-id.txt``) is missing or unreadable ("no agent
    back-pointer").
  - the underlying ``_resolve_held_handoff_for_session`` call itself raises
    (its own no-resolvable-session-id condition, or a zero-held-claims
    session with ``allow_standalone`` NOT set -- which this module never
    does, see below).

A ZERO-held-claims session (a cross-repo-memo pickup) is explicitly NOT the
raise case (2026-08-03 break-class precedent) -- this module always calls
the reused resolver with ``allow_standalone=True`` and returns ``(None,
False)``, a caller-recognizable "standalone, no baton to bill this commit
to" signal. A MULTIPLE-held-claims session is also not the raise case --
resolved via the same four-leg ordering key, ``degraded=True``.

A ledger I/O failure is a separate, later concern (C5) that degrades to a
warning; this module performs no ledger I/O of its own and has no
I/O-failure branch.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional, Tuple

from coordinator_core.session import claim_index
from coordinator_core.session import core as session_core


def _resolve_target_session_id(committer_id: str, root: Path, sessions_dir: Optional[str]) -> str:
    base = sessions_dir if sessions_dir is not None else session_core.sessions_dir(str(root))
    if not base:
        return committer_id

    agent_dir = os.path.join(base, claim_index._AGENTS_SUBDIR, committer_id)
    if not os.path.isdir(agent_dir):
        return committer_id

    owner_sid, _complete = claim_index._agent_owner_sid(agent_dir)
    if not owner_sid:
        raise ValueError(
            f"resolve_owner: agent {committer_id!r} has no resolvable "
            "em-session-id.txt back-pointer -- cannot determine its "
            "dispatching EM's baton."
        )
    return owner_sid


def _handoff_id_from_path(path: str) -> str:
    basename = path.rsplit("/", 1)[-1]
    if basename.endswith(".md"):
        basename = basename[: -len(".md")]
    return basename


def resolve_owner_handoff_id(
    committer_id: str,
    root: Path,
    sessions_dir: Optional[str] = None,
) -> Tuple[Optional[str], bool]:
    """Resolve ``committer_id`` (a session id OR a dispatched agent id) to
    the ``handoff_id`` of the baton its commits should be filed under.

    Returns ``(handoff_id, degraded)``:
      - ``handoff_id`` is ``None`` only for the legitimate zero-held-claims
        (standalone) outcome.
      - ``degraded`` mirrors ``_resolve_held_handoff_for_session``'s own
        multi-claim-ordering signal (True iff two or more held claims tied
        on every ordering leg) -- always ``False`` for the single-claim and
        standalone outcomes.

    Raises ``ValueError`` per this module's docstring "RAISE CONTRACT".
    """
    if not committer_id:
        raise ValueError("resolve_owner: committer_id required")

    target_sid = _resolve_target_session_id(committer_id, root, sessions_dir)

    primary, degraded = _primary_held_handoff(Path(root), target_sid)
    if primary is None:
        return None, False

    return _handoff_id_from_path(primary), degraded


_SENTINEL = chr(0xFFFF)


def _primary_held_handoff(root: Path, session_id: str) -> Tuple[Optional[str], bool]:
    """Mirror of ``baton_assemble._resolve_held_handoff_for_session`` with
    ``allow_standalone=True`` and an explicit ``session_id``, reduced to the
    ``(primary, degraded)`` pair this module reads. Kept in step by
    ``test_resolve_owner_parity``; ``baton_assemble`` is a multi-thousand-line
    module whose import dominated the commit landing, so it is not imported.

    The composite ordering key is ``(stage_rank, claimed_at_or_sentinel,
    claim_mtime, basename)``; see the original's docstring for the legs.
    """
    from coordinator_core.session.claims import (
        CLAIM_STAGE_APPLY,
        claim_stamped,
        list_claims_by_session,
    )

    matches = list_claims_by_session(session_id, cwd=str(root))
    held = sorted(b for class_, b in matches if class_ == "handoff-claims")
    if not held:
        return None, False
    if len(held) == 1:
        return "state/handoffs/" + held[0], False

    sessions_dir = session_core.sessions_dir(str(root))
    ordering = []
    for basename in held:
        stage_rank = 2
        claimed_at_key = _SENTINEL
        mtime_key = float("inf")
        if sessions_dir:
            claim_dir = Path(sessions_dir) / "handoff-claims" / basename
            try:
                stage_raw = (claim_dir / "stage").read_text(encoding="utf-8").strip()
            except (OSError, UnicodeDecodeError):
                stage_raw = None
            if stage_raw == CLAIM_STAGE_APPLY:
                stage_rank = 0 if claim_stamped(claim_dir) else 1
            claimed_at_file = claim_dir / "claimed_at"
            try:
                claimed_at = claimed_at_file.read_text(encoding="utf-8").strip() or None
            except (OSError, UnicodeDecodeError):
                claimed_at = None
            if claimed_at:
                claimed_at_key = claimed_at
            try:
                mtime_key = claimed_at_file.stat().st_mtime
            except OSError:
                try:
                    mtime_key = claim_dir.stat().st_mtime
                except OSError:
                    mtime_key = float("inf")
        ordering.append((stage_rank, claimed_at_key, mtime_key, basename))

    ordering.sort()
    degraded = any(ordering[i][:3] == ordering[i + 1][:3] for i in range(len(ordering) - 1))
    return "state/handoffs/" + ordering[0][3], degraded

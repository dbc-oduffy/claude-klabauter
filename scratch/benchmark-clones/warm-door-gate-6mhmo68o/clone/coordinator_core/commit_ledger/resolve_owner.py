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

  2. BATON HOP. Given the resolved (EM-or-plain) session id, reuses
     ``coordinator_core.baton_assemble._resolve_held_handoff_for_session``'s
     THREE-outcome resolution (one held claim / zero held claims / multiple
     held claims, see that function's own docstring for the full four-leg
     ordering key) rather than reimplementing it. That function resolves
     against the CURRENT process's session identity (an env-var chain) and
     takes no explicit-session-id parameter -- this module reuses it as-is
     by scoping ``COORDINATOR_SESSION_ID`` (that chain's own highest-
     precedence, explicit-override tier -- see
     ``coordinator_core.session.core.SESSION_ENV_PRECEDENCE``) to the
     resolved sid for the duration of the call, then restoring whatever was
     there before. This is the sanctioned override tier, not a private
     monkeypatch of internal state.

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

#: The env var ``_resolve_held_handoff_for_session`` itself treats as the
#: highest-precedence, explicit-override tier of the session-identity
#: chain -- see ``coordinator_core.session.core.resolve_session_id``. This
#: module scopes it, temporarily, to drive that reused resolver against a
#: TARGET session id rather than the calling process's own identity.
_SESSION_OVERRIDE_ENV = "COORDINATOR_SESSION_ID"


def _resolve_target_session_id(committer_id: str, root: Path, sessions_dir: Optional[str]) -> str:
    """AGENT HOP: translate ``committer_id`` to its owning EM session id when
    it names a dispatched agent; otherwise ``committer_id`` is already a
    plain session id and is returned unchanged.

    Raises ``ValueError`` when ``committer_id`` names an agent directory
    whose ``em-session-id.txt`` back-pointer is missing or unreadable -- a
    genuinely unresolvable owner, per this module's raise contract.
    """
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
    """``state/handoffs/<basename>.md`` -> ``<basename>`` -- the same
    handoff_id-is-basename-minus-``.md`` convention
    ``commit_ledger.store``'s ``read_chain``/``_legacy_resolve_predecessor_id``
    already assume (``root / "state" / "handoffs" / f"{handoff_id}.md"``)."""
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

    # Local import: avoids a module-level import cycle (baton_assemble is a
    # large package with its own broad import surface) and mirrors this
    # repo's existing convention of importing baton_assemble internals at
    # call time (see baton_assemble/__init__.py's own local imports inside
    # _resolve_held_handoff_for_session).
    from coordinator_core.baton_assemble import _resolve_held_handoff_for_session

    prior = os.environ.get(_SESSION_OVERRIDE_ENV)
    os.environ[_SESSION_OVERRIDE_ENV] = target_sid
    try:
        primary, _additional, degraded = _resolve_held_handoff_for_session(
            Path(root), allow_standalone=True
        )
    finally:
        if prior is None:
            os.environ.pop(_SESSION_OVERRIDE_ENV, None)
        else:
            os.environ[_SESSION_OVERRIDE_ENV] = prior

    if primary is None:
        return None, False

    return _handoff_id_from_path(primary), degraded

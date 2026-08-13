"""
coordinator_core.session.stale_claims — FACTS-only stale-claim-handoff enumerator.

Purpose: enumerate live ``state/handoffs/*.md`` entries whose recorded claimer
session is NOT live, per ``coordinator_core.session.liveness.session_live`` (the
single shared liveness key — see that module's docstring). This exists to give
Coordinator-claude's ``workstream-complete`` Step 0 crash-recovery detector a FACTS answer it
can key its own chain-terminal decision on, without duplicating claude-klabauter's
liveness logic into a bash re-implementation (see
``cross-repo/inbox/2026-07-23-claude-klabauter-em-wsc-step0-fails-open-crash-recovery.md``).

This is a PURE IN-PROCESS LIBRARY, not an IPC op — mirrors
``coordinator_core.session.claims`` / ``coordinator_core.session.liveness``'s
own "not an IPC op" posture (neither is in ``coordinator_core/_registry_map.py``
or ``op_scopes.py``). Consumed by ``coordinator/bin/session-claim-cli``'s
``list-stale-claim-handoffs`` subcommand, itself a direct-import CLI trampoline
(template-variant #1, no ``cc_invoke``/IPC hop) — same shape as that CLI's
existing ``claim-artifact`` family.

BOUNDARY — load-bearing, read before touching this file: this module reports
FACTS ONLY and makes NO disposition decision. It does not decide chain-terminal,
does not rank candidates, does not guess which baton a session is "continuing",
and MUST NEVER mutate a handoff or any other artifact. Claude-klabauter owns the liveness
MECHANISM; coordinator-claude owns the POLICY that consumes it (their Step 0 detector). Do NOT
add a "resolve my baton" / "pick the winner" convenience here — that is the
exact boundary violation this module exists to prevent. A future caller that
wants a disposition decision belongs on coordinator-claude's side of the fence, reading this
module's output as one input among several.

Claimer resolution is ledger-first (C5, 2026-08-07) via
``coordinator_core.claim_state.resolve_claim_state`` — the ledger
(``<common_dir>/coordinator-sessions/handoff-claims/``, branch-independent)
wins over the tracked-frontmatter mirror whenever it holds a live claim, so a
handoff whose mirror reverted to ``status: open`` on a branch switch (while
the ledger still holds the claim) is no longer silently excluded from this
enumerator's output. ``resolve_claim_state`` itself stays DR-084 dual-tolerant
on the mirror side (``claimed_by`` canonical, ``consumed_by`` legacy fallback,
``claimed_by``-wins-on-both-present) — see that module's own docstring.

Spec backlink: cross-repo/inbox/2026-07-23-claude-klabauter-em-wsc-step0-fails-open-crash-recovery.md
Spec backlink: pln-claim-state-make-the-ledger-th-6641e3
"""
from __future__ import annotations

from pathlib import Path
from typing import List, NamedTuple, Optional

from coordinator_core.claim_state import resolve_claim_state
from coordinator_core.ops.fleet._common import collect_live_handoff_paths
from coordinator_core.session.liveness import session_live


class StaleClaimHandoff(NamedTuple):
    """One live handoff whose recorded claimer session is confirmed NOT live.

    ``path``: absolute filesystem path to the handoff (as returned by
    ``collect_live_handoff_paths`` — always under ``<repo_root>/state/handoffs``).
    ``claimer_sid``: the dead session id the handoff names as its claimer
    (whichever of ``claimed_by``/``consumed_by`` resolved — see module docstring).
    """

    path: str
    claimer_sid: str


def _claimer_sid(handoff_path: str, repo_root: Optional[str] = None) -> str:
    """Resolve the recorded claimer session id for ``handoff_path``,
    ledger-first via ``coordinator_core.claim_state.resolve_claim_state``.

    The ledger wins whenever it holds a live claim, regardless of what the
    tracked-frontmatter mirror (``claimed_by``/DR-084 ``consumed_by``) says —
    see ``resolve_claim_state``'s own docstring for the desync incident this
    generalizes a fix for. A handoff claimed on a branch the shared worktree
    has since left reverts its mirror to ``status: open`` with no claim
    fields, but the branch-independent ledger still holds the claim; reading
    the mirror alone (this function's prior behavior) silently dropped such a
    handoff from the stale-claim listing. Returns ``""`` when neither source
    has a claim (``ClaimState.source == "none"``) — an unclaimed (open)
    handoff or an unreadable file, mirroring the prior contract.
    """
    state = resolve_claim_state(handoff_path, repo_root=repo_root)
    return state.holder or ""


def list_stale_claim_handoffs(repo_root: Optional[str] = None) -> List[StaleClaimHandoff]:
    """Enumerate live handoffs claimed by a session that is NOT live.

    ``repo_root``: the repo whose ``state/handoffs/`` to scan — defaults to
    the current working directory when absent/empty (mirrors
    ``coordinator_core.session.claims.claim_artifact``'s own ``""``-means-cwd
    convention). Also passed as the ``cwd`` argument to ``session_live`` so the
    liveness lookup resolves the SAME repo's session hub, not the process's
    ambient cwd.

    Read-only: never mutates a handoff or any other artifact (see module
    docstring's BOUNDARY note). An unclaimed (open) handoff — no
    ``claimed_by``/``consumed_by`` at all — is silently excluded; it has no
    claimer to ask liveness about. A permission-denied ``state/handoffs/``
    scan degrades to an empty result (mirrors
    ``collect_live_handoff_paths``'s own documented OSError-on-scan-failure
    contract; callers get "nothing found" rather than a crash) — this module
    is a best-effort FACTS reporter, not a hard-failing gate.
    """
    root = Path(repo_root).resolve() if repo_root else Path.cwd()
    cwd_str = str(root)

    stale: List[StaleClaimHandoff] = []
    try:
        handoff_paths = collect_live_handoff_paths(root)
    except OSError:
        return stale

    for p in handoff_paths:
        sid = _claimer_sid(str(p), repo_root=cwd_str)
        if not sid:
            continue
        if not session_live(sid, cwd_str):
            stale.append(StaleClaimHandoff(path=str(p), claimer_sid=sid))
    return stale

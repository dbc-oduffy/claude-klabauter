"""
coordinator_core.hooks.sessionend_auto_commit — SessionEnd warm-door op,
DEREGISTERED.

Purpose: warm-door counterpart of DoE-claude's
`coordinator/hooks/scripts/sessionend-auto-commit.py`. That script is itself
DEREGISTERED (2026-08-27 PM ruling, relayed by claude-klabauter-em: "commits
are for EMs and when they choose to commit") — kept on disk, unregistered,
so re-arming is one hooks.json entry rather than a rewrite. This module
mirrors that state: it is landed (nothing in DoE's tree should still hold
the executable), but nothing dials it today — no hooks.json entry names
`hooks.sessionend_auto_commit`, and this chunk adds none.

WHY DEREGISTERED (unchanged from the source script — not re-derived here):
`SessionEnd` fires with no EM present to author a commit message; the
data-loss case the mechanism was built against is covered instead by
`/workday-complete`'s dirty-tree sweep
(`coordinator_core.ops.workday_complete_step2_5_dirty_tree`, EM-run daily
ceremony). The `/handoff`/quick-wrap ceremony callers of
`session.safe_commit_offer` are untouched by this deregistration — an EM ran
those.

RE-ARMING SHAPE: the source script's own DELETED SPAWN TARGET note states
the CLI it used to shell out to (`coordinator/bin/safe-commit-offer.py`) was
deleted and replaced by the registered engine op `session.safe_commit_offer`
— re-arming requires dialing that op in-process, never resurrecting a CLI
spawn. This op does exactly that (`session.safe_commit_offer`'s own
`_handler`, called via `params` shaped `{"cwd": ..., "session_id": ...}`, no
`message`/`groups_json` — mechanical-grouping default, matching the source
script's own no-EM-present posture) so re-arming is a hooks.json edit alone
(DoE's, out of this chunk's scope), not a second engine change.

Op contract: `params["payload"]` is the SessionEnd payload dict
(`session_id`, `cwd`, `reason`, ...) — never `os.environ` or this process's
own `cwd`. Always returns `no_advisory()` (empty dict) — this hook never
surfaces advisory text and never blocks, matching the source script's own
always-exit-0 contract. A missing/empty `session_id` is a silent no-op
(never guessed), matching the source script's own "no session_id means
nothing to rescue, not license to guess whose dirty tree it is".

Graceful degradation: any failure calling `session.safe_commit_offer`
degrades to `no_advisory()`, never raised — a SessionEnd hook must never
brick session teardown, matching the source script's own always-exit-0
contract on every failure path (subprocess timeout, unresolvable engine,
push-retry exhaustion, etc. — none of which exist any more on this side of
the port, since there is no subprocess left to fail).

Spec backlink: docs/plans/2026-09-18-doe-holds-no-scripts.md § W4-C14
DoE source: coordinator/hooks/scripts/sessionend-auto-commit.py
"""

from __future__ import annotations

from coordinator_core._hook_envelope import payload_of
from coordinator_core.hooks._envelope import no_advisory
from coordinator_core.ipc import register_op


@register_op("hooks.sessionend_auto_commit")
def _handler(params: dict, repo_root=None) -> dict:
    """SessionEnd: best-effort mechanical-grouping commit+push for this
    session's dirty tree — DEREGISTERED, matching the source script (module
    docstring). Nothing dials this op today; it exists so re-arming is a
    hooks.json edit alone.
    """
    payload = payload_of(params)

    session_id = payload.get("session_id") or ""
    if not isinstance(session_id, str) or not session_id:
        return no_advisory()

    cwd = payload.get("cwd") or ""
    if not isinstance(cwd, str):
        cwd = ""

    try:
        from coordinator_core.ops.session.safe_commit_offer import (
            _handler as _safe_commit_offer_handler,
        )

        _safe_commit_offer_handler({"cwd": cwd or None, "session_id": session_id})
    except Exception:
        pass

    return no_advisory()

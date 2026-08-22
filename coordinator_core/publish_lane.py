"""coordinator_core.publish_lane — the one lane the 2s bar does not govern.

Purpose: name the closed set of ops a percolate/publish round must reach, and the
end-to-end bound they get inside that round, so the publish path is not governed by
a number sized for the close ceremony. Outside a declared round, nothing here fires
and every op resolves exactly as it did before this module existed.

PM ruling, 2026-08-21: *"Percolate and publish should not be subject to a time cap
of 2s, that's silly. It needs as long as 10 minutes."*

WHAT THIS IS NOT. It is not a widening knob, and the difference is the whole design:

  - It is a CLOSED LIST, not a prefix or a pattern. `PUBLISH_LANE_OPS` names one op
    today. An op joins by edit, in a commit, against a ratchet test that fails on a
    silent addition — the same shape `op_budget_suspension`'s roster uses, for the
    same reason.
  - It is a BOOLEAN, not a number. A caller declares "I am a publish round"; it does
    not get to say how long. `PUBLISH_LANE_BUDGET_SECS` lives here, is not readable
    from the environment, and cannot be raised by anything outside this file.
  - It does not touch `CEREMONY_BUDGET_SECS`, `DISPATCH_TIMEOUT_SECS`, or
    `SUSPENSION_BAR_MS`. All three keep their values and their ratchets. A ceremony
    op called from anywhere that is not a declared publish round still resolves at
    2s, and a suspended op called from anywhere else is still off.

WHY A LANE RATHER THAN REINSTATEMENT. `ceremony.scoped_git_commit` has not earned its
way back under `op_budget_suspension`'s bar and this module does not claim it has:
its reinstatement case is still owed, cold and under load, with spawn count. What the
ruling establishes is narrower — that a publish round is not the caller that bar was
written to protect. A close ceremony runs on every session's hot path ~50 times over;
a publish round is a deliberate, PM-invoked, once-in-a-while external action whose
whole job is moving a few thousand paths into a mirror. Holding the second to a bound
sized for the first is what stopped publishing working at all.

WHAT IS STILL A DEFECT, SAID PLAINLY. The 150s sample behind that suspension was
**53 `git` subprocess spawns for one ~2100-path commit**. The spawn count is the
defect; this bound accommodates it and fixes nothing. Per CLAUDE.md § brightline,
process time and spawn count are the axes — a wall-clock bound ranks damage and sets
no budget. This constant comes down when the spawn count does.

TWO SIGNALS, because the warm engine has two processes:

  - `PUBLISH_LANE_ENV` in the environment. The round CLI sets it once and every child
    it spawns — the engine subprocess on the cold path, the `--dump-op-timeouts` probe
    the client sizes its own kill ceiling from, the nested `scoped-git-commit` CLI —
    inherits it for free. This covers every in-process door too, including
    `ipc.get_op_handler`, which resolves in the CALLER's own process.
  - `PUBLISH_LANE_FIELD` on the JSON-RPC envelope. A warm server's `os.environ`
    reflects whoever SPAWNED it, never the caller of any given request — the same
    reason `_session_id` crosses the wire in `warm/client.py` rather than being
    re-resolved server-side. The warm client stamps this field when it is itself a
    lane process, so the server sees the lane the request was made in.

`is_active()` ORs them. Neither is authoritative alone and neither needs to be: both
say the same thing about the same round, and a request that carries the field while
its server lacks the env is the normal warm shape, not an anomaly.

Spec backlink: docs/decisions/DR-350-the-publish-lane-is-not-the-close-ceremony.md
Governs, unchanged: docs/decisions/DR-348-the-ceremony-budget-is-a-ratchet.md
                    docs/decisions/DR-349-one-budget-governs-every-constructed-op.md
"""

from __future__ import annotations

import os
from typing import Any, Dict, Optional

__all__ = [
    "PUBLISH_LANE_ENV",
    "PUBLISH_LANE_FIELD",
    "PUBLISH_LANE_BUDGET_SECS",
    "PUBLISH_LANE_OPS",
    "declare_lane",
    "env_declares_lane",
    "request_declares_lane",
    "is_active",
    "budget_for",
]

#: The environment marker a percolate/publish round sets on itself, inherited by every
#: process it spawns. Deliberately NOT a duration: it is read for truthiness and never
#: parsed as a number, so there is no value of it that buys an op more time than
#: `PUBLISH_LANE_BUDGET_SECS` below.
PUBLISH_LANE_ENV = "COORDINATOR_PUBLISH_LANE"

#: The JSON-RPC envelope field carrying the same fact across the warm-engine pipe,
#: underscore-prefixed like `_session_id` and `_origin_worktree` to mark it transport
#: metadata rather than an op param.
PUBLISH_LANE_FIELD = "_publish_lane"

#: Ten minutes, end-to-end, for a lane op inside a declared round. The PM's number,
#: not a measurement — see the module docstring on what it accommodates and what it
#: does not fix. It may be LOWERED freely; raising it is a PM ruling recorded in
#: DR-350, and `coordinator_core/tests/test_publish_lane_budget.py` pins it against an
#: independent second literal so lifting it here alone leaves the tree red.
PUBLISH_LANE_BUDGET_SECS = 600.0

#: The closed list. One row, because one is what a round actually reaches: every
#: publish CLI in `coordinator/bin` was swept for the suspension roster and
#: `ceremony.scoped_git_commit` is the only member any of them names.
#:
#: A row here is load-bearing in two directions at once — it lifts the ceremony clamp
#: AND it lifts the suspension refusal — so adding one is admitting an op to the
#: whole carve-out, never to half of it. The ratchet test fails on an unratified
#: addition for exactly that reason.
PUBLISH_LANE_OPS = frozenset({
    "ceremony.scoped_git_commit",
})


def declare_lane(environ: Optional[Dict[str, str]] = None) -> None:
    """Mark THIS process, and everything it spawns, as a percolate/publish round.

    Called once by a round's own entrypoint. Idempotent, and deliberately silent:
    a round that declares the lane twice is not a condition worth a message.
    """
    env = os.environ if environ is None else environ
    env[PUBLISH_LANE_ENV] = "1"


def env_declares_lane(environ: Optional[Dict[str, str]] = None) -> bool:
    """True when this process's environment marks it a publish round.

    Falsy-string aware, so `COORDINATOR_PUBLISH_LANE=0` reads as "not a round"
    rather than as the presence of a variable. An operator who explicitly turns the
    lane off gets it off; that direction narrows and is always allowed.
    """
    env = os.environ if environ is None else environ
    raw = env.get(PUBLISH_LANE_ENV)
    if raw is None:
        return False
    return raw.strip().lower() not in ("", "0", "false", "no", "off")


def request_declares_lane(msg: Any) -> bool:
    """True when a JSON-RPC request envelope carries the lane field.

    Tolerant of a non-dict `msg` because `dispatch_message` validates the envelope
    AFTER this is consulted on some paths; a malformed request is not a lane request.
    """
    if not isinstance(msg, dict):
        return False
    return bool(msg.get(PUBLISH_LANE_FIELD))


def is_active(msg: Any = None, environ: Optional[Dict[str, str]] = None) -> bool:
    """True when the caller is inside a declared percolate/publish round.

    Either signal suffices — see the module docstring's "TWO SIGNALS". `msg` is
    optional so the in-process doors (`ipc.get_op_handler`) can ask the same question
    without inventing an envelope.
    """
    return request_declares_lane(msg) or env_declares_lane(environ)


def budget_for(
    method: object,
    msg: Any = None,
    environ: Optional[Dict[str, str]] = None,
) -> Optional[float]:
    """`PUBLISH_LANE_BUDGET_SECS` when *method* is a lane op in a live lane, else None.

    None means "this module has no opinion" — the caller keeps whatever it would have
    resolved on its own. Returning the ordinary budget instead would make this
    function a second source of truth for every op in the tree, which is precisely
    the shape a carve-out must not take.
    """
    if not isinstance(method, str) or method not in PUBLISH_LANE_OPS:
        return None
    if not is_active(msg, environ):
        return None
    return PUBLISH_LANE_BUDGET_SECS

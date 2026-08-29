"""
coordinator_core.ops.delegation_check — JSON-RPC "delegation.check"
operation: the read path a session calls FOR ITSELF when it reaches an
"ask the PM" step, to settle who to ask by reading an artifact it opens
itself.

Purpose: docs/plans/2026-08-28-the-ask-the-pm-step-gets-an-artifact-to-
check.md, chunk C5. Wraps
``coordinator_core.session.fleet_delegation.check_fleet_delegation`` (C2)
as a warm op — adds NO policy of its own. Every refusal reason a caller can
observe comes from what C2 already computed (granted/record), so this op
and any CLI consumer of the same C2 function cannot disagree about what a
grant means; this module does not re-derive expiry, authorship, or
liveness judgments, it only labels the ``(granted, record)`` pair C2
already returned.

TESTABLE OBLIGATION (criterion clause 3 / LEG 3 — Review: staff-eng
(the Staff Engineer), finding 3): ``check_delegation``'s parameter list is exactly
``(decision_class)`` — no authority argument, no claimed-holder argument,
no session-supplied token, no caller-asserted identity of any kind. A
peer's claim to hold the relay has no parameter to arrive through this
function; calling it with any extra/authority-shaped keyword argument
raises ``TypeError`` from Python's own argument binding, not a runtime
check this module would have to remember to enforce.

Wire params (JSON-RPC ``delegation.check``):
    decision_class (str, required) -- the decision class the caller is
        about to ask the PM to settle.

Reply fields (result object in JSON-RPC response, and ``check_delegation``'s
own return value):
    granted     (bool)            -- True iff a live, unexpired grant
                                      (C2's ``check_fleet_delegation``)
                                      covers ``decision_class``.
    designated  (dict | None)     -- the raw ``{"pid", "create_time"}``
                                      pair from the grant record, verbatim,
                                      whenever a record was found (parsed
                                      as an object) -- REGARDLESS of
                                      ``granted``, mirroring C2's own
                                      "record whenever found" shape. None
                                      when no record was found.
    reason      (str)             -- "granted" when granted; "no-grant"
                                      when C2 found no record at all
                                      (byte-identical to the no-file case,
                                      per C2's own contract); "not-granted"
                                      when a record was found but C2 did
                                      not grant it. This module does NOT
                                      discriminate the finer reason
                                      (expired vs malformed vs wrong class
                                      vs not-live) -- doing so would
                                      require re-deriving C2's own
                                      judgment, which is the policy
                                      duplication this op is built to
                                      avoid.

BUDGET, ASSERTED NOT ASSUMED (Review: staff-eng (the Staff Engineer), finding 9): the
check body is <=5ms process time -- one ``stat``, one small JSON read, one
clock read, one ``psutil`` liveness probe of the designated pair -- inside
a <=50ms end-to-end warm reach. ``check_delegation`` does nothing beyond
calling C2's ``check_fleet_delegation`` and shaping its return value; the
budget is C2's to keep, this module adds no additional I/O, subprocess, or
interpreter start on the read path.

Self-registration: importing this module calls register_op(
"delegation.check", ...) as a side-effect. This module is listed in
``_EAGER_OP_MODULES`` (``coordinator_core/ops/__init__.py``) and in
``OP_MODULE_MAP`` (``coordinator_core/ops/_registry_map.py``), so the
registration fires when ``import coordinator_core.ops`` executes and the op
is reachable via ``coordinator_core.ipc._lazy_import_and_lookup``. It also
carries an ``_OP_KEY_SCOPE`` entry (``coordinator_core/op_scopes.py``) and an
``OP_CLASSIFICATION`` entry (``coordinator_core/authz/classification.py``),
so all five registration surfaces checked by
``coordinator_core.authz.registration_quad.check_registration_quad`` are
present.

Spec backlink: docs/plans/2026-08-28-the-ask-the-pm-step-gets-an-artifact-to-check.md § chunk C5
Precedent (plain function + thin wire wrapper shape): coordinator_core/ops/percolate_identity_check.py
Policy source (never restated here): coordinator_core/session/fleet_delegation.py :: check_fleet_delegation

Negative-spec:
  - Does NOT accept an authority/claimed-holder/session-token/identity
    keyword argument on ``check_delegation`` -- there is no parameter for
    one to arrive through (see TESTABLE OBLIGATION above).
  - Does NOT re-derive expiry, authorship-verdict, class-membership, or
    liveness judgments -- those live in C2 and ONLY in C2.
  - Does NOT call any subprocess, ``git``, or interpreter start on the
    read path.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Optional

from coordinator_core.ipc import register_op
from coordinator_core.session.fleet_delegation import check_fleet_delegation


def check_delegation(decision_class):
    """Settle whether a live, unexpired grant covers ``decision_class``.

    Parameter list is exactly ``(decision_class)`` -- see module docstring's
    TESTABLE OBLIGATION. Returns {granted, designated, reason} (see module
    docstring "Reply fields"). Adds no policy: the granted boolean and the
    record come from ``check_fleet_delegation`` (C2) unchanged.
    """
    granted, record = check_fleet_delegation(decision_class)
    designated = record.get("designated") if isinstance(record, dict) else None
    if record is None:
        reason = "no-grant"
    elif granted:
        reason = "granted"
    else:
        reason = "not-granted"
    return {"granted": granted, "designated": designated, "reason": reason}


@register_op("delegation.check")
async def _delegation_check(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "delegation.check" handler.

    Required params: decision_class.

    Returns: {granted, designated, reason} (see module docstring "Reply
    fields"). Raises ValueError (propagated as a JSON-RPC error) for a
    missing `decision_class` param.
    """
    decision_class = params.get("decision_class")
    if not decision_class:
        raise ValueError("delegation.check requires params: decision_class")

    return await asyncio.to_thread(check_delegation, decision_class)

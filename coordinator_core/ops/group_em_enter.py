"""
coordinator_core.ops.group_em_enter — JSON-RPC "groupem.enter".

Purpose: the single composed, warm entry op for the Group EM mode. It fires
the read pass (`group_em.read_pass.build_candidate_roster`, itself layered
over the already-live `session.peer_roster` read rather than a second
harness enumeration), the send pass
(`group_em.send_pass.build_send_digest`), the nomination claim
(`group_em.nomination.claim`), and the peer-set baseline diff
(`group_em.baseline.diff_and_persist`) in one call, replacing the
hand-run three-script entry the `group-em` skill document describes today.
Ported per `docs/plans/2026-08-30-group-em-entry-fires-one-warm-op.md`
chunk C5 — the reference registration convention is
`coordinator_core/ops/session_peer_roster.py`, followed exactly here.

Self-registration: importing this module calls
register_op("groupem.enter", _group_em_enter) as a side-effect. Registered
in `coordinator_core/ops/__init__.py`'s `_EAGER_OP_MODULES`,
`coordinator_core/ops/_registry_map.py`'s `OP_MODULE_MAP`,
`coordinator_core/op_scopes.py` (scope "none" -- same resolution story as
`session.peer_roster`: the harness peer registry is machine-global, not
per-worktree), and `coordinator_core/authz/classification.py` (MUTATING --
see that module's comment on this entry for the reason).

Returns exactly one payload:
    {"nomination": {...}, "roster": [...], "digest": {...},
     "baseline": {...}}

DEGRADE, NEVER RAISE. Each leg's own exception is caught HERE (not inside
the leg module, which is untouched) and reported as `null` for that key
plus a `<key>_error` sibling string, via the shared `_leg` write helper
(overengineering review finding 9: the four legs used to repeat this
result-write by hand). Two independent legs, not four: nomination is
independent of the roster; digest and baseline both consume the roster
leg's output and fan out to three keys total, so a raising roster leg
cascades -- digest and baseline both go `null` too, each carrying
`"roster-leg-failed"` rather than their own exception text. This mirrors
`session.peer_roster`'s own degrade-not-raise discipline (that op relies on
`peer_roster.build_roster`'s internal degrade; this op applies the same
per-leg degrade at this composition layer, with the roster dependency named
above rather than four fully isolated legs). A reader adding a fifth leg
expecting the same isolation nomination gets should not: it inherits
whatever the roster chain does.

Negative-spec:
    - Never auto-supersedes a crown. `nomination.claim`'s verdict --
      including a live OR dead `superseded_incumbent` -- is passed through
      verbatim; this op never claims over anyone.
    - Never resolves GATE 1 / GATE 2. `digest["gate_declaration_required"]`
      is carried through from `build_send_digest` unmodified.
    - Never re-enumerates the harness. The roster leg is built over
      `group_em.read_pass.build_candidate_roster`, which itself only reads
      `claude agents --json` / the receiver-state reader -- no second
      enumeration is added at this layer.
    - No fallback beyond the one named per-leg degrade above -- a failing
      leg is reported null-plus-reason, never guessed at or retried.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Optional

from coordinator_core.ipc import register_op
from coordinator_core.group_em import baseline as group_em_baseline
from coordinator_core.group_em import nomination as group_em_nomination
from coordinator_core.group_em import read_pass as group_em_read_pass
from coordinator_core.group_em import send_pass as group_em_send_pass


def _leg_error(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"


def _leg(result: dict[str, Any], key: str, outcome: tuple[Any, Optional[str]]) -> None:
    """Write one leg's `(value, error)` outcome into `result` as `result[key]` plus,
    only when `error` is not `None`, `result[f"{key}_error"]`. The one write shape
    all four legs share (overengineering review finding 9) -- collapsed here instead
    of repeated at each call site in `_group_em_enter`."""
    value, error = outcome
    result[key] = value
    if error is not None:
        result[f"{key}_error"] = error


def _run_nomination(repo_root: str, caller_session_id: str) -> tuple[Optional[dict], Optional[str]]:
    try:
        return group_em_nomination.claim(repo_root, caller_session_id), None
    except Exception as exc:  # noqa: BLE001 -- degrade-never-raise per module docstring
        return None, _leg_error(exc)


def _run_roster(repo_root: str, caller_session_id: str) -> tuple[Optional[list], Optional[str]]:
    try:
        return (
            group_em_read_pass.build_candidate_roster(
                repo_root, caller_session_id_value=caller_session_id
            ),
            None,
        )
    except Exception as exc:  # noqa: BLE001
        return None, _leg_error(exc)


def _run_digest(
    repo_root: str, roster: Optional[list], caller_session_id: str
) -> tuple[Optional[dict], Optional[str]]:
    if roster is None:
        return None, "roster-leg-failed"
    try:
        return group_em_send_pass.build_send_digest(repo_root, roster, caller_session_id), None
    except Exception as exc:  # noqa: BLE001
        return None, _leg_error(exc)


def _run_baseline(
    repo_root: str, roster: Optional[list], caller_session_id: str
) -> tuple[Optional[dict], Optional[str]]:
    if roster is None:
        return None, "roster-leg-failed"
    try:
        repo_key = group_em_nomination.repo_key(repo_root)
        current_peers = {
            verdict["session_id"]: {"state": verdict.get("state"), "reason": verdict.get("reason")}
            for verdict in roster
            if isinstance(verdict.get("session_id"), str)
        }
        return (
            group_em_baseline.diff_and_persist(
                current_peers,
                repo_key=repo_key,
                session_id=caller_session_id,
                repo_root=Path(repo_root),
            ),
            None,
        )
    except Exception as exc:  # noqa: BLE001
        return None, _leg_error(exc)


@register_op("groupem.enter")
def _group_em_enter(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "groupem.enter" handler.

    Params:
        repo_root (str, optional) -- the repo whose peer set is entered.
            Defaults to the CALLING process's own `os.getcwd()` when
            omitted, matching `session.peer_roster`'s own convention.
        caller_session_id (str, optional) -- defaults to
            `read_pass.caller_session_id()` (the `CLAUDE_CODE_SESSION_ID`
            env var) when omitted.

    Returns:
        {"nomination": {...} | None, "roster": [...] | None,
         "digest": {...} | None, "baseline": {...} | None}
    A failed leg is `None` with a `"<key>_error"` sibling string carrying
    the reason; the other legs still populate (see module docstring).

    Scope "none" (op_scopes.py): the engine-injected `repo_root` kwarg is
    never resolved/injected for a "none"-scoped op -- the `repo_root` WIRE
    PARAM above (read from `params`) is the only way a caller narrows the
    target, matching `session.peer_roster`'s own convention exactly.
    """
    override = params.get("repo_root") if isinstance(params, dict) else None
    target_root = override if isinstance(override, str) and override else os.getcwd()

    sid_override = params.get("caller_session_id") if isinstance(params, dict) else None
    caller_session_id: Optional[str] = (
        sid_override if isinstance(sid_override, str) and sid_override else None
    )
    if caller_session_id is None:
        caller_session_id = group_em_read_pass.caller_session_id()

    result: dict[str, Any] = {}

    _leg(
        result,
        "nomination",
        _run_nomination(target_root, caller_session_id)
        if caller_session_id
        else (None, "no-caller-session-id"),
    )

    _leg(result, "roster", _run_roster(target_root, caller_session_id))
    roster = result["roster"]

    _leg(result, "digest", _run_digest(target_root, roster, caller_session_id))

    _leg(
        result,
        "baseline",
        _run_baseline(target_root, roster, caller_session_id)
        if caller_session_id
        else (None, "no-caller-session-id"),
    )

    return result

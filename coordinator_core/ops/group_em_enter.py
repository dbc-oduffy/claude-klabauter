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
result-write by hand). THREE independent legs, not four and not two:
nomination stands alone; baseline consumes the peer ENUMERATION; only
digest consumes the roster leg's output. So a raising roster leg cascades
to digest ALONE -- digest goes `null` carrying `"roster-leg-failed"` rather
than its own exception text, while nomination and baseline still populate.
A failing ENUMERATION is the wider blast radius: it takes roster and
baseline together, and digest after roster.

  Corrected 2026-08-30. This paragraph previously said digest AND baseline
  both cascade from the roster, which was true of the code as written and
  wrong as a design. Baseline was being fed the classified roster, so
  `exited` meant "stopped being a paused candidate" -- a peer that resumed
  work, or that the classifier simply failed to reach a verdict on, was
  reported as having left. Baseline now diffs the enumeration directly.

This mirrors
`session.peer_roster`'s own degrade-not-raise discipline (that op relies on
`peer_roster.build_roster`'s internal degrade; this op applies the same
per-leg degrade at this composition layer, with the roster dependency named
above rather than four fully isolated legs). A reader adding a fifth leg
expecting the same isolation nomination gets should not: it inherits
whatever the roster chain does.

ORDER IS LOAD-BEARING: crown, then roster, then digest. A REFUSED crown -- `claimed`
false in the nomination verdict -- stops the op BEFORE the roster leg is built, not
merely before the digest. `send_pass.build_send_digest` arms each emitted peer's
cooldown as it emits, so a session with no standing to hold the crown building one
anyway would burn an hour of throttle state on peers it had no right to offer, silently
degrading the legitimate holder's next digest. An AUTO-REPLACED crown (see below) is
NOT a refusal -- `claimed` is true, so roster/digest/baseline all run normally.

FIVE NOMINATION OUTCOMES, TWO OF THEM REFUSALS. `nomination.claim` (see that module's
own docstring for the full table) returns one of:
    1. fresh claim (no prior record)                        -> claimed=True
    2. re-entry by the current holder                        -> claimed=True, already_held=True
    3. LIVE incumbent                                         -> claimed=False, REFUSAL
    4. incumbent with POSITIVE evidence of death
       (`live_reason: "pid_not_running"`)                     -> claimed=True, AUTO-REPLACE,
                                                                  `replaced_holder` populated
    5. incumbent with only ABSENCE of registry evidence
       (`live_reason: "no_registry_record"`)                  -> claimed=False, REFUSAL
Only 3 and 5 stop this op before the roster leg. Case 4 claims and proceeds -- a record
whose session is PROVABLY gone (not merely unaccounted for) takes nothing from anyone,
and refusing it forever would fire on essentially every invocation after the first
(a dead-but-unreaped record is this mode's steady state). The replacement is reported
LOUDLY via `nomination["replaced_holder"]`, never folded into `superseded_incumbent`.

PAYLOAD SHAPE ON REFUSAL -- ABSENT KEYS, NOT NULL VALUES. On a refused crown (cases 3
and 5 above), `roster` and `digest` (and `baseline`, which itself consumes the roster)
are OMITTED from the returned dict entirely -- `"roster" not in result`, never
`result["roster"] is None`. This is deliberate and load-bearing for the consumer, not a
cosmetic choice: an EMPTY roster (`[]`) is a live fact -- "I looked, nobody is there" --
while an ABSENT roster means "I had no standing to look, do not reason about peers from
this." Collapsing the two into "falsy" would tell a session "no peers need you" when
this op never looked. No `roster_error` / `digest_error` / `baseline_error` sibling is
written on this path either, for the same reason the value key itself is omitted -- the
per-leg degrade convention (`_leg`, below) is for a leg that RAN and failed, not one
that never ran.

NOMINATION FIELDS ARE PASSED THROUGH VERBATIM, NEVER COLLAPSED. `already_held`, the
nested `superseded_incumbent.live_reason` (one of `"live"`, `"no_registry_record"`,
`"pid_not_running"`), and `replaced_holder` are the only way a consumer distinguishes,
from the payload alone, all five cases above -- a boolean `claimed` alone cannot
express them. This op adds no new names for these fields and re-derives nothing:
`result["nomination"]` is exactly `nomination.claim()`'s return value, unmodified.

Negative-spec:
    - Never auto-supersedes a crown. `nomination.claim`'s verdict --
      including a live OR dead `superseded_incumbent`, its `already_held`
      flag, and its `live_reason` -- is passed through verbatim; this op
      never claims over anyone, never lets a caller pass a supersede flag
      on its own initiative, and never reduces any of those fields to a
      bool or renames them.
    - Never builds the roster or digest on a refused crown, and never
      reports them as present-but-null. See "PAYLOAD SHAPE ON REFUSAL"
      above -- a refusal is direction-class and reaches the human via the
      passed-through `superseded_incumbent`, never retried or swallowed
      here, and never disguised as an empty/absent-but-keyed result.
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


def _run_roster(
    repo_root: str, caller_session_id: str, agents: Optional[list] = None
) -> tuple[Optional[list], Optional[str]]:
    try:
        return (
            group_em_read_pass.build_candidate_roster(
                repo_root, agents=agents, caller_session_id_value=caller_session_id
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
    repo_root: str, agents: Optional[list], caller_session_id: str
) -> tuple[Optional[dict], Optional[str]]:
    """Diff the ENUMERATED PEER SET -- never the candidate roster.

    The population this leg tracks is "every peer session in this repo", not
    "the peers currently eligible to be nudged". Feeding it the roster (as
    this leg did until 2026-08-30) makes `exited` mean "stopped being a
    paused candidate", so a peer that merely resumed work is reported as
    having left, and a peer the reader failed to classify this tick is
    reported as having left twice over. Both were observed: the roster
    oscillated 0 -> 3 -> 0 across three ticks while only ONE of the three
    sessions had actually gone away.

    `state` carries the live harness status, so a busy -> idle transition
    lands in the diff's `changed` list. That transition is the signal a Group
    EM is watching for; deriving `state` from the roster's paused-verdict
    instead made it unobservable.
    """
    if agents is None:
        return None, "enumeration-leg-failed"
    try:
        repo_key = group_em_nomination.repo_key(repo_root)
        peers = group_em_read_pass.enumerate_repo_peers(agents, caller_session_id)
        current_peers = {
            peer["sessionId"]: {"state": peer.get("status"), "reason": None}
            for peer in peers
            if isinstance(peer.get("sessionId"), str)
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
    A failed leg (one that RAN and raised) is `None` with a `"<key>_error"`
    sibling string carrying the reason; the other legs still populate (see
    module docstring). On a REFUSED crown, `roster`/`digest`/`baseline` are
    OMITTED from the dict entirely -- a leg that never ran is absent, not
    `None` -- see module docstring § PAYLOAD SHAPE ON REFUSAL.
    `nomination["already_held"]` and `nomination["superseded_incumbent"]
    ["live_reason"]` are passed through from `nomination.claim()` verbatim.

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

    nomination_outcome = (
        _run_nomination(target_root, caller_session_id)
        if caller_session_id
        else (None, "no-caller-session-id")
    )
    _leg(result, "nomination", nomination_outcome)
    nomination_value = nomination_outcome[0]

    # ORDER IS LOAD-BEARING: crown, then roster, then digest. A REFUSED crown --
    # `claimed` false, whether the incumbent is live or dead -- stops here, before the
    # roster leg even runs, not just before the digest. `send_pass.build_send_digest`
    # arms each emitted peer's cooldown as it emits; a session with no standing to hold
    # the crown must not burn that throttle state on peers it had no right to offer.
    # Roster and digest are reported ABSENT with a reason, distinguishable from "ran and
    # found nothing" -- never an empty list, never a partially-built digest.
    crown_refused = isinstance(nomination_value, dict) and nomination_value.get("claimed") is False

    if crown_refused:
        # ABSENT, not null: `roster`/`digest`/`baseline` are OMITTED from `result`
        # entirely on this path -- never written as `None`, never given an `_error`
        # sibling. An empty roster is a fact ("looked, found nobody"); an absent one
        # means "had no standing to look" -- collapsing the two loses that distinction
        # for the consumer. See module docstring § PAYLOAD SHAPE ON REFUSAL.
        return result

    # ONE enumeration, two consumers. The roster leg classifies it down to
    # nudge candidates; the baseline leg diffs it whole. Fetching twice would
    # bill the box twice for the same read and let the two legs disagree about
    # who exists within a single tick.
    try:
        agents: Optional[list] = group_em_read_pass.fetch_live_agents(Path(target_root))
    except Exception:  # noqa: BLE001
        agents = None

    _leg(result, "roster", _run_roster(target_root, caller_session_id, agents))
    roster = result["roster"]

    _leg(result, "digest", _run_digest(target_root, roster, caller_session_id))

    # Baseline does NOT consume the roster, so a roster-leg failure does not
    # cascade here -- it consumes the enumeration directly.
    _leg(
        result,
        "baseline",
        _run_baseline(target_root, agents, caller_session_id)
        if caller_session_id
        else (None, "no-caller-session-id"),
    )

    return result

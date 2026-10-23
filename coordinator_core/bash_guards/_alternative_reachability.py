"""Reachability of an AlternativeRecord member for the caller class that receives it.

Grades whether the receiving caller class can express a member, against the
allowlist ruleset block_reviewer_bash_outside_allowlist enforces for it. Kept
apart from _alternative_liveness: that gate fires the guard in its own process
and proves readability; this one asks whether the recipient could run the offer.
Pure computation, no spawn.
"""

from __future__ import annotations

import enum
import json
from typing import Any, Optional

from coordinator_core.bash_guards import block_reviewer_bash_outside_allowlist as _allow
from coordinator_core.bash_guards.block_reviewer_bash_outside_allowlist import (
    _first_token_is_allowlisted_binary,
    _is_confined_type,
    _resolve_ruleset,
    _segment_is_tier_a_allowlisted,
)
from coordinator_core.guard_messages.alternative_record import (
    _MANIFEST_PATH,
    AdaptableOffer,
    Audience,
    CapabilityRef,
    EscalationRoute,
    Member,
    Override,
    ReadyCommand,
)


class Verdict(enum.Enum):
    REACHABLE = "reachable"
    UNREACHABLE = "unreachable"
    PRE_LAUNCH_ONLY = "pre_launch_only"


def _command_reachable(cmd: str, ruleset: dict) -> bool:
    """Same predicates the confining guard applies; a scan failure or a bare
    unquoted metacharacter grades unreachable."""
    try:
        found, splits, _fixup = _allow._scan_for_unquoted_metacharacter(cmd)
    except Exception:
        return False
    if found is not None:
        return False
    if splits:
        return _allow._evaluate_pipeline_segments(cmd, splits, ruleset) is None
    return _segment_is_tier_a_allowlisted(cmd, ruleset) or _first_token_is_allowlisted_binary(
        cmd, ruleset
    )


def _capability_reachable(member: CapabilityRef, caller_class: Optional[str]) -> bool:
    with open(_MANIFEST_PATH, encoding="utf-8") as fh:
        entry = json.load(fh).get("capabilities", {}).get(member.capability_id)
    if not entry or not entry.get("available", False):
        return False
    agent_types = entry.get("agent_types")
    return agent_types is None or caller_class in agent_types


def reachable(
    member: Member, caller_class: Optional[str], policy: Any, audience: Audience
) -> Verdict:
    """Grade ``member`` for the receiver. ``audience`` is required: S2 returns
    ``caller_class`` None for both the EM and an unresolved subagent, so None
    alone never means EM."""
    if isinstance(member, EscalationRoute):
        return Verdict.REACHABLE
    if isinstance(member, Override):
        return Verdict.PRE_LAUNCH_ONLY if audience is Audience.EM else Verdict.UNREACHABLE
    if audience is Audience.EM:
        return Verdict.REACHABLE
    if isinstance(member, CapabilityRef):
        ok = audience is Audience.SUBAGENT and _capability_reachable(member, caller_class)
        return Verdict.REACHABLE if ok else Verdict.UNREACHABLE
    if audience is not Audience.SUBAGENT or caller_class is None:
        return Verdict.UNREACHABLE
    if not _is_confined_type(caller_class, policy):
        return Verdict.REACHABLE
    if isinstance(member, ReadyCommand):
        cmd = member.rendered()
    elif isinstance(member, AdaptableOffer):
        cmd = member.template
    else:
        return Verdict.UNREACHABLE
    ruleset = _resolve_ruleset(caller_class, policy)
    return Verdict.REACHABLE if _command_reachable(cmd, ruleset) else Verdict.UNREACHABLE

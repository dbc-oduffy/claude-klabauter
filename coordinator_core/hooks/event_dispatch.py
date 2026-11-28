"""coordinator_core.hooks.event_dispatch — contract for `hooks.dispatch_event`: one op frame per
tool-call event, replacing one cold process per matching hook registration.

`EVENT_TABLE` rows are (event, matcher regex, ordered hook ops, advisory flag, leg class). It is
seeded from the existing per-family aggregators, which stay registered and are the bodies a row
calls; no leg is re-folded here. `merge_envelopes` is the one merge rule: first deny wins and
short-circuits, otherwise the first rewrite is kept and advisory text concatenates in table order.

`dispatch_event` is a stub returning `no_advisory()`: the body (run every matching op in-process
under `_preuse_phases.run_phased`'s deadline, then merge) and the registry set members belong to the
op's registering row. Stdlib plus the dependency-free envelope leaf only at import.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable, Optional, Tuple

from coordinator_core._hook_envelope import (
    COORDINATOR_PROVENANCE_MARKER,
    context_only,
    event_advisory,
    no_advisory,
    payload_of,
    post_advisory,
    rewrite_input,
)
from coordinator_core.ipc import register_op

OP_NAME = "hooks.dispatch_event"

#: Leg classes per docs/reference/warm-hook-migration.md: a Blocking leg answers a dead-engine
#: refusal on the cold path; a Reconstructable leg re-fires at the next boundary, so its miss is
#: a loud allow.
BLOCKING = "blocking"
RECONSTRUCTABLE = "reconstructable"


@dataclass(frozen=True)
class EventRow:
    """One hooks.json registration collapsed into the table.

    `matcher` is a `re.fullmatch` regex over the tool name; the empty string matches every tool
    (and an event with no tool). `ops` run in order. `advisory` is True only when no op in the row
    can deny; `leg_class` is the row's strongest class.
    """

    event: str
    matcher: str
    ops: Tuple[str, ...]
    advisory: bool
    leg_class: str

    def matches(self, event: str, tool_name: Optional[str]) -> bool:
        if event != self.event:
            return False
        if not self.matcher:
            return True
        return re.fullmatch(self.matcher, tool_name or "") is not None


EVENT_TABLE: Tuple[EventRow, ...] = (
    EventRow("PreToolUse", "Bash|PowerShell", ("hooks.preuse_bash_dispatch",), False, BLOCKING),
    EventRow("PreToolUse", "Agent", ("hooks.preuse_agent_dispatch",), False, BLOCKING),
    EventRow("PreToolUse", "Skill", ("hooks.preuse_skill_dispatch",), True, RECONSTRUCTABLE),
    EventRow(
        "PreToolUse",
        "Write|Edit|MultiEdit|NotebookEdit",
        ("hooks.preuse_write_dispatch",),
        False,
        BLOCKING,
    ),
    EventRow("PreToolUse", "Read|Grep|Glob", ("hooks.preuse_search_dispatch",), False, BLOCKING),
    EventRow("PreToolUse", "SendMessage", ("hooks.preuse_sendmessage_dispatch",), False, BLOCKING),
    EventRow(
        "PostToolUse",
        "Write|Edit|MultiEdit|NotebookEdit|Agent|Workflow",
        ("hooks.postuse_advisory_dispatch",),
        True,
        RECONSTRUCTABLE,
    ),
    EventRow("PostToolUse", "Agent", ("hooks.agent_postuse_dispatch",), True, RECONSTRUCTABLE),
    EventRow(
        "PostToolUse",
        "Write|Edit|MultiEdit",
        ("hooks.postuse_stop_family_dispatch",),
        True,
        RECONSTRUCTABLE,
    ),
    EventRow("Stop", "", ("hooks.stop_dispatch",), False, BLOCKING),
)


def rows_for(event: str, tool_name: Optional[str] = None) -> Tuple[EventRow, ...]:
    """The table rows matching this event and tool, in table order."""
    return tuple(row for row in EVENT_TABLE if row.matches(event, tool_name))


def ops_for(event: str, tool_name: Optional[str] = None) -> Tuple[str, ...]:
    """The ordered, de-duplicated ops one frame runs for this event and tool."""
    seen: dict[str, None] = {}
    for row in rows_for(event, tool_name):
        for op in row.ops:
            seen.setdefault(op, None)
    return tuple(seen)


def _hso(envelope: Any) -> dict:
    hso = envelope.get("hookSpecificOutput") if isinstance(envelope, dict) else None
    return hso if isinstance(hso, dict) else {}


def _is_deny(envelope: Any) -> bool:
    if not isinstance(envelope, dict):
        return False
    return _hso(envelope).get("permissionDecision") == "deny" or envelope.get("decision") == "block"


def _text_of(envelope: Any) -> str:
    if not isinstance(envelope, dict):
        return ""
    text = _hso(envelope).get("additionalContext") or envelope.get("systemMessage") or ""
    text = str(text).strip()
    if text.startswith(COORDINATOR_PROVENANCE_MARKER):
        text = text[len(COORDINATOR_PROVENANCE_MARKER):].strip()
    return text


def merge_envelopes(event: str, envelopes: Iterable[Any]) -> dict:
    """Fold per-op envelopes, in table order, into the one envelope the harness reads.

    First deny wins and is returned unchanged. Otherwise the first `updatedInput` rewrite is kept,
    and every advisory text concatenates (blank-line separator) into one envelope of the event's
    shape. Non-dict and empty envelopes contribute nothing; nothing at all is `no_advisory()`.
    """
    texts: list[str] = []
    rewrite: Optional[dict] = None
    for envelope in envelopes:
        if _is_deny(envelope):
            return envelope
        if rewrite is None and _hso(envelope).get("updatedInput"):
            rewrite = _hso(envelope)["updatedInput"]
        text = _text_of(envelope)
        if text:
            texts.append(text)
    context = "\n\n".join(texts)
    if rewrite is not None:
        return rewrite_input(event, rewrite, context)
    if not context:
        return no_advisory()
    if event == "PostToolUse":
        return post_advisory(context)
    if event in ("Stop", "SubagentStop"):
        return event_advisory(event, context)
    return context_only(event, context)


def dispatch_event(event: dict) -> dict:
    """Contract: `event` is the flat hook payload (`hook_event_name`, `tool_name`, ...); returns the
    merged envelope of every op `ops_for(hook_event_name, tool_name)` names. Stub: answers `no_advisory()`."""
    return no_advisory()


@register_op(OP_NAME)
async def _handler(params: dict, repo_root=None) -> dict:
    return dispatch_event(payload_of(params))

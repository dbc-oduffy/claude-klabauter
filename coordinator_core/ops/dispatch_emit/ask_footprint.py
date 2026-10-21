"""Footprint leg for an existing XS sizing: when the leg is owed, its return schema and its prompt.

Pure: no I/O, no spawn. `ask_compose.compose_ask_script` wires the leg ahead of the gate agent.
"""

from __future__ import annotations

from typing import Optional, Sequence

from coordinator_core.ops.dispatch_emit.sizing_fire import ARM_XS

#: Same shape `ask_compose._obj` emits.
FOOTPRINT_SCHEMA = {
    "type": "object",
    "required": ["writes"],
    "properties": {"writes": {"type": "array", "items": {"type": "string"}}},
}


def footprint_owed(*, arm: Optional[str], writes: Sequence[str], sized_in_run: bool) -> bool:
    """True when an existing XS sizing reaches the gate with no file footprint.

    A raw or intake ask is sized in run and its scout already authors `writes`, so it is never owed.
    """
    return arm == ARM_XS and not writes and not sized_in_run


def footprint_prompt(*, sizing_rel: str, baton_path: Optional[str]) -> str:
    """The prompt asking an agent for the repo-relative files the ask writes."""
    parts = [
        f"Read the XS sizing at {sizing_rel} (intent, premise.evidence, exit_criterion)."
    ]
    if baton_path:
        parts.append(
            f"Also read the baton record at {baton_path} (its ## Specification and ## Acceptance criteria)."
        )
    parts.append(
        "Return `writes`: the repo-relative FILE paths this ask will create or modify, tests included. "
        "List files only: no globs and no directories. Every path is inside this repo. "
        "Write nothing yourself and run no command."
    )
    return " ".join(parts)

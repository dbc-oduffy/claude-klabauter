"""Pre-dispatch check agent specs for an emitted inventory script.

One check agent per live row, never batched. Invariants: this module never
imports ``emit`` at module level (``emit`` imports it); a review prompt built
from an ``AgentSpec`` never carries ``key``, which is JS-side only.
"""

from __future__ import annotations

from typing import NamedTuple, Optional, Sequence

from coordinator_core.ops.dispatch_emit.wave_map import WaveRow

CHECK_PHASE_TITLE = "Pre-dispatch check"
CHECK_AGENT_MODEL = "sonnet"

ALREADY_DONE_RULE = (
    "Verdict rule. Answer `already-done` only when, for EACH clause of the row's "
    "summary and verification text, you can quote an excerpt of content read at HEAD "
    "that discharges that clause; list each as an evidence item {path, anchor, "
    "excerpt, discharges}. A file's existence, a test file's presence, a commit "
    "subject, a branch name or a PR title is never evidence. When in doubt the "
    "verdict is `still-open`."
)


class AgentSpec(NamedTuple):
    key: str
    label: str
    phase: str
    agent_type: Optional[str]
    model: str
    prompt: str
    schema: str


#: Placeholder every check prompt carries where the rule goes. The emitter
#: replaces it with a ``SharedBlocks`` reference to ``ALREADY_DONE_RULE``, so
#: the rule text is declared once per script, never inlined per prompt.
ALREADY_DONE_RULE_MARKER = "{{ALREADY_DONE_RULE}}"

CHECK_SCHEMA = "predispatch_check_result"


def _footprint_text(writes: object) -> str:
    if isinstance(writes, (list, tuple, set, frozenset)):
        paths = sorted(str(p) for p in writes)
        return ", ".join(paths) if paths else "(none declared)"
    return "(undeclared)"


def _check_prompt(row: WaveRow) -> str:
    return (
        f"Pre-dispatch check for row `{row.id}`: {row.title}\n\n"
        "Decide whether this row's work is already done at HEAD. Read the "
        "files named below as they stand at HEAD; do not rely on commit "
        "history, branch names or file presence.\n\n"
        f"Footprint: {_footprint_text(row.writes)}\n\n"
        "Row spec (summary and verification text):\n"
        f"{row.body}\n\n"
        f"{ALREADY_DONE_RULE_MARKER}\n\n"
        "Return the verdict as structured output: verdict, evidence items, note."
    )


def check_specs(rows: Sequence[WaveRow]) -> list[AgentSpec]:
    """One check AgentSpec per row, key = row id, label ``check:<id>``.

    Prompts carry ``ALREADY_DONE_RULE_MARKER``, not the rule text.
    """
    return [
        AgentSpec(
            key=row.id,
            label=f"check:{row.id}",
            phase=CHECK_PHASE_TITLE,
            agent_type=None,
            model=CHECK_AGENT_MODEL,
            prompt=_check_prompt(row),
            schema=CHECK_SCHEMA,
        )
        for row in rows
    ]

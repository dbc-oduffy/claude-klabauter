"""coordinator_core.hooks.general_purpose_code_handoff_deny -- the main loop's handoff of a code
edit to an untyped ``general-purpose`` agent is refused.

Purpose: leaf of ``hooks.block_unenumerated_agent_type.check``. An EM denied a product-code write
must route the edit to a typed executor, never to the harness default agent.
Invariant: no module-scope import beyond stdlib, ``hooks._envelope`` and
``hooks.doc_data_extensions`` (``test_fleet_mode_reader_set.py`` counts every import edge into the
guard closure). ``GUARD_NAME`` is the code-dispatch guard's own name, so both share one level.

Order is load-bearing: tool, agent_id and the type are checked before the prompt is read, so no
other Agent dispatch pays anything.
"""

from __future__ import annotations

import re
from typing import Any, Mapping, Optional

from coordinator_core.hooks._envelope import deny
from coordinator_core.hooks.doc_data_extensions import _DOC_DATA_EXTENSIONS

GUARD_NAME = "nudge-em-code-dispatch"
GENERAL_PURPOSE_TYPE = "general-purpose"
_CODE_EXTENSIONS = frozenset(
    {
        ".py", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".sh", ".bash", ".rb", ".go", ".rs",
        ".java", ".kt", ".c", ".cpp", ".cc", ".h", ".hpp", ".swift", ".sql", ".cs", ".php", ".lua",
    }
) - _DOC_DATA_EXTENSIONS
_EDIT_VERB = re.compile(
    r"(?<![\w-])(?:edit|change|fix|apply|implement|write|modify|replace|patch|refactor|add)"
    r"(?:s|es|ed|ing|d)?(?![\w-])",
    re.IGNORECASE,
)
_CODE_PATH = re.compile(r"(?<![\w./:@-])((?:[\w.-]+/)*[\w-][\w.-]*(\.[A-Za-z0-9]+))(?![\w-])")
FORK_TYPE = "fork"
_DEFERRED_TYPES = frozenset({GENERAL_PURPOSE_TYPE, FORK_TYPE})
_REASON = (
    "Code edits go to a typed executor: dispatch subagent_type coordinator:executor "
    "with the file and the change in its brief; general-purpose is untyped and unguarded. "
    "coordinator:executor (Sonnet) is also the lane for scratch-only forensics "
    "(scratch/ writes, git hash-object -w)."
)
_FORK_REASON = (
    "A fork inherits the parent model (Opus) and ignores a model override, so it is not a "
    "route around the tier rule: dispatch subagent_type coordinator:executor (Sonnet) with "
    "the file and the change in its brief. coordinator:executor also covers scratch-only "
    "forensics (scratch/ writes, git hash-object -w)."
)


def _names_code_path(prompt: str) -> bool:
    """True when the prompt names an in-repo path whose extension is a code extension."""
    text = prompt.replace("\\", "/")
    return any(
        m.group(2).lower() in _CODE_EXTENSIONS and "://" not in m.group(1)
        for m in _CODE_PATH.finditer(text)
    )


def general_purpose_code_handoff_deny(
    payload: Mapping[str, Any], subagent_type: str
) -> Optional[dict]:
    """None unless a top-level general-purpose or fork Agent dispatch hands over a code edit.

    The prompt must name a code-extension path and carry an edit verb; the deny goes through
    ``apply_guard_level`` so the guard level governs it.
    """
    if (
        payload.get("tool_name") != "Agent"
        or payload.get("agent_id")
        or subagent_type not in _DEFERRED_TYPES
    ):
        return None
    tool_input = payload.get("tool_input")
    prompt = tool_input.get("prompt") if isinstance(tool_input, Mapping) else None
    if not isinstance(prompt, str) or not _EDIT_VERB.search(prompt) or not _names_code_path(prompt):
        return None
    from coordinator_core.machine_profile import apply_guard_level

    return apply_guard_level(GUARD_NAME, deny("PreToolUse", _FORK_REASON if subagent_type == FORK_TYPE else _REASON)
    )

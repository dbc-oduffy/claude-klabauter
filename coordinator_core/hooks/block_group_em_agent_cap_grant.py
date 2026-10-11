"""coordinator_core.hooks.block_group_em_agent_cap_grant — PreToolUse(SendMessage)
deny for a Group EM GO/slot/terms message that caps agent width.

Box capacity limits what agents run (vitest workers, tsc, builds, recursive
package runs), never how many agents, executors, rows or waves a session
dispatches. Fires only when the sending session holds live Group EM standing
and one message line carries both a grant word (GO, slot, terms, grant) and an
agent-unit cap ("max 3 executors", "at most 4 concurrent rows", "<=2 agents in
parallel", "OK-5"). Command-class caps ("maxWorkers=2", "one tsc") never match.
Precision over recall: the regex gate runs first and the standing read (two
small file reads, no spawn) runs only on a match; every internal error allows.

Op contract: `params` is the flat PreToolUse payload dict (`tool_name`,
`session_id`, `cwd`, `tool_input.message`).

Hook wiring: PreToolUse, matcher `SendMessage`, op `hooks.block_group_em_agent_cap_grant`.
"""

from __future__ import annotations

import re
from pathlib import Path

from coordinator_core._hook_envelope import deny, no_advisory, payload_of
from coordinator_core.hooks.support.message_envelope import compose, render
from coordinator_core.ipc import register_op
from coordinator_core.machine_profile import apply_guard_level

GUARD_NAME = "block-group-em-agent-cap-grant"

_UNIT = r"(?:executors?|agents?|subagents?|rows?|waves?|workflows?)"
_NUM = r"(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten)"
_MOD = r"(?:(?:concurrent|parallel|simultaneous|concurrently)\s+)?"

_GRANT_RE = re.compile(r"\bGO\b|\b(?:slots?|terms|grants?|granted)\b", re.IGNORECASE)
_GO_CASED_RE = re.compile(r"\bGO\b")

_CAP_RES = (
    re.compile(
        r"(?:\bmax(?:imum)?\b|\bat\s+most\b|\bup\s+to\b|\bno\s+more\s+than\b"
        r"|\blimit(?:ed)?\s+to\b|\bcap(?:ped)?(?:\s+at)?\b|<=|≤)"
        r"\s*(?:of\s+)?" + _NUM + r"\s+" + _MOD + _UNIT + r"\b",
        re.IGNORECASE,
    ),
    re.compile(r"\b" + _NUM + r"\s+" + _UNIT + r"\s+(?:max|maximum|at\s+a\s+time)\b", re.IGNORECASE),
    re.compile(r"\b" + _NUM + r"\s+(?:concurrent|parallel|simultaneous)\s+" + _UNIT + r"\b", re.IGNORECASE),
    re.compile(
        r"(?:<=|≤)\s*" + _NUM + r"\s+" + _UNIT + r"?\s*(?:in\s+parallel|concurrent(?:ly)?)\b",
        re.IGNORECASE,
    ),
    re.compile(r"\bOK-\d+\b"),
)

_DENY_PROSE = (
    "GO caps agent width; the box limits commands, not agents. "
    "Cap commands: vitest --maxWorkers=2, one tsc, builds on a slot, no pnpm -r."
)


def _gate(text: str) -> bool:
    for line in text.splitlines():
        m = _GRANT_RE.search(line)
        if m and any(rx.search(line) for rx in _CAP_RES):
            if m.group(0).lower() != "go" or _GO_CASED_RE.search(line):
                return True
    return False


def _holds_standing(session_id: str, cwd: str) -> bool:
    from coordinator_core.group_em import nomination

    holder = nomination.box_holder()
    if holder and holder.get("session_id") == session_id and holder.get("live"):
        return True
    record = nomination.standing(cwd, session_id)
    return bool(record and record.get("standing") == "live")


def _deny_message(env: object = None) -> str:
    return render(compose(_DENY_PROSE), env=env if isinstance(env, dict) else None)


@register_op("hooks.block_group_em_agent_cap_grant")
def _handler(params: dict, repo_root=None) -> dict:
    params = payload_of(params)
    try:
        if params.get("tool_name") != "SendMessage":
            return no_advisory()
        tool_input = params.get("tool_input")
        text = tool_input.get("message") if isinstance(tool_input, dict) else None
        if not isinstance(text, str) or not _gate(text):
            return no_advisory()
        session_id = params.get("session_id")
        if not (isinstance(session_id, str) and session_id):
            return no_advisory()
        cwd = params.get("cwd")
        if not (isinstance(cwd, str) and cwd):
            cwd = str(Path.cwd())
        if not _holds_standing(session_id, cwd):
            return no_advisory()
        return apply_guard_level(GUARD_NAME, deny("PreToolUse", _deny_message(params.get("env")))) or no_advisory()
    except Exception:
        return no_advisory()

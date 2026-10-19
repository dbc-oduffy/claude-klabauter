"""Fan-out grouping rule and agent-cost reporting shared by every dispatch_emit composer.

`propose_groups` answers which small adjacent units share one agent; `FanoutCost` answers how many
agents a script starts and what their startup costs; `with_fanout` / `read_fanout` carry that into
the script's `export const meta` literal and back out to the emit reply. A group is one agent that
owns several members' assignments and ends with one `<ID>: <STATUS> <one line>` line per member
(`group_prompt`, `GROUP_MEMBER_RESULT_JS`); a member whose line is missing reads as no result.

Constants:
- `UNITS_PER_AGENT` 2: three scouts over six questions, so a group's wall clock is two small units.
- `GROUP_MIN_UNITS` 3: under three units there is nothing to batch.
- `LARGE_UNIT_BYTES` 16000 (about 4k tokens of prompt): a unit past this does enough work on its
  own to amortise its startup and stays solo.
- `GROUP_CONTEXT_CAP_BYTES` 24000 (about 6k tokens): a pair of mid-size units past this stays split
  so one agent's prompt context stays well under its own startup load.
- `STARTUP_TOKENS_PER_AGENT` 31000: median of the first assistant usage record (input +
  cache_creation + cache_read) over 23 local workflow-subagent transcripts measured 2026-10-19
  (n=23, min 14.8k, p10 24.0k, p90 50.1k, max 50.8k). A calibrated literal, never read at emit time.

Imports stay stdlib plus `_js_string_literal`; no composer, subprocess or path read.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from coordinator_core.ops.workflow_scaffold import _js_string_literal

FANOUT_GROUP_MODES = ("auto", "split")
UNITS_PER_AGENT = 2
GROUP_MIN_UNITS = 3
LARGE_UNIT_BYTES = 16_000
GROUP_CONTEXT_CAP_BYTES = 24_000
STARTUP_TOKENS_PER_AGENT = 31_000

BASES = ("exact", "lower-bound")

# One `agent(` dispatch site. Unlike `_workflow_contract._AGENT_CALL_SITE` (`\bagent\s*\(`, which
# also matches a `.agent(` member call), the lookbehind refuses `_rows.agent(` and `xagent(`.
# Count over `_workflow_contract.scrub(text)` so an `agent(` inside a string or comment is no site.
AGENT_CALL_RE = re.compile(r"(?<![\w.])agent\(")

_META_START = re.compile(r"export\s+const\s+meta\s*=\s*\{")
# Trap: also matches `fanout: {` inside a quoted meta value such as the description.
_FANOUT_KEY = re.compile(r"\bfanout\s*:\s*(?=\{)")
# Shapes check_meta_pure_literal refuses: interpolation, backtick, spread, a call.
_IMPURE = re.compile(r"\$\{|`|\.\.\.|[A-Za-z_$][\w$]*\s*\(")


@dataclass(frozen=True)
class Unit:
    """One fan-out unit: `key` is its adjacency key (units group only with equal-key neighbours),
    `size_bytes` the bytes of the prompt or brief the agent would carry."""

    id: str
    key: str
    size_bytes: int


def propose_groups(units: "list[Unit] | tuple[Unit, ...]", mode: str = "auto") -> tuple[tuple[str, ...], ...]:
    """Contiguous, order-preserving groups of unit ids; every unit lands in exactly one group.

    `mode="split"` or fewer than `GROUP_MIN_UNITS` units: one group per unit. Otherwise a unit joins
    its predecessor's group only when keys match, neither is over `LARGE_UNIT_BYTES`, the group has
    fewer than `UNITS_PER_AGENT` members and the pair stays within `GROUP_CONTEXT_CAP_BYTES`.
    """
    if mode not in FANOUT_GROUP_MODES:
        raise ValueError(f"fanout_group must be one of {FANOUT_GROUP_MODES}, got {mode!r}")
    if mode == "split" or len(units) < GROUP_MIN_UNITS:
        return tuple((u.id,) for u in units)
    groups: list[list[Unit]] = []
    for unit in units:
        open_group = groups[-1] if groups else None
        if (
            open_group is not None
            and len(open_group) < UNITS_PER_AGENT
            and open_group[0].key == unit.key
            and unit.size_bytes <= LARGE_UNIT_BYTES
            and open_group[0].size_bytes <= LARGE_UNIT_BYTES
            and sum(m.size_bytes for m in open_group) + unit.size_bytes <= GROUP_CONTEXT_CAP_BYTES
        ):
            open_group.append(unit)
        else:
            groups.append([unit])
    return tuple(tuple(m.id for m in g) for g in groups)


@dataclass(frozen=True)
class FanoutCost:
    """How many agents a script starts: `units` fan-out units in `agents` agents. `basis` is
    "exact" when the count is fixed at emit and "lower-bound" when run-time legs add more."""

    units: int
    agents: int
    basis: str
    startup_tokens_per_agent: int = STARTUP_TOKENS_PER_AGENT
    groups: tuple[tuple[str, ...], ...] = ()

    def __post_init__(self) -> None:
        if self.basis not in BASES:
            raise ValueError(f"basis must be one of {BASES}, got {self.basis!r}")

    @property
    def startup_tokens_total(self) -> int:
        return self.agents * self.startup_tokens_per_agent

    def meta_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "expectedAgents": self.agents,
            "basis": self.basis,
            "units": self.units,
            "startupTokensPerAgent": self.startup_tokens_per_agent,
            "startupTokensTotal": self.startup_tokens_total,
        }
        if self.groups:
            out["groups"] = [list(g) for g in self.groups]
        return out


def _meta_span(text: str) -> tuple[int, int] | None:
    """(index after the opening brace, index of the matching close brace) of the meta literal,
    skipping braces inside quoted strings."""
    m = _META_START.search(text)
    if not m:
        return None
    depth, quote, i = 1, "", m.end()
    while i < len(text):
        ch = text[i]
        if quote:
            if ch == "\\":
                i += 1
            elif ch == quote:
                quote = ""
        elif ch in "'\"":
            quote = ch
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return m.end(), i
        i += 1
    return None


def with_fanout(meta_text: str, cost: FanoutCost) -> str:
    """`meta_text` (a script or its meta block) with one pure-literal `fanout: {...}` entry added
    inside the `export const meta` literal. Raises ValueError when there is no meta literal, it
    already has a `fanout` entry, or the rendered value would not be a pure literal."""
    span = _meta_span(meta_text)
    if span is None:
        raise ValueError("no `export const meta = {` literal to add fanout to")
    start, close = span
    if _FANOUT_KEY.search(meta_text, start, close):
        raise ValueError("meta already carries a fanout entry")
    rendered = json.dumps(cost.meta_dict(), ensure_ascii=True, separators=(", ", ": "))
    if _IMPURE.search(rendered):
        raise ValueError("fanout ids must not contain a call, backtick, spread or interpolation shape")
    body = meta_text[start:close].rstrip()
    comma = "" if body.endswith(("{", ",")) else ","
    return f"{meta_text[:start]}{body}{comma}\n  fanout: {rendered},\n{meta_text[close:]}"


def read_fanout(script: str) -> dict[str, Any] | None:
    """The `fanout` object of the script's meta literal, or None when it carries none."""
    span = _meta_span(script)
    if span is None:
        return None
    m = _FANOUT_KEY.search(script, span[0], span[1])
    if not m:
        return None
    try:
        value, _ = json.JSONDecoder().raw_decode(script, m.end())
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


GROUP_MEMBER_RESULT_JS = """\
function _memberResult(text, id) {
  const lines = String(text == null ? '' : text).split('\\n');
  const head = id + ':';
  for (let i = lines.length - 1; i >= 0; i--) {
    const line = lines[i].trim();
    if (line.startsWith(head)) return line;
  }
  return null;
}
"""


#: The group prompt's header after its member ids; the ask composer builds the same header at run time.
GROUP_HEADER_TAIL = (
    ". Do each one fully and separately, as if it were your only task. End your reply with exactly one "
    "line per assignment, in the form `<ID>: <STATUS> <one line>` (STATUS is DONE, PARTIAL or BLOCKED), "
    "after all the work.\n\n"
)


def group_prompt(member_prompts_js: list[str] | tuple[str, ...], member_ids: list[str] | tuple[str, ...]) -> str:
    """A JS expression building one agent's prompt over several members.

    `member_prompts_js` are JS expressions for each member's own filled prompt, in `member_ids`
    order. The header tells the agent it owns every assignment, in order, and must end its reply
    with one `<ID>: <STATUS> <one line>` line per member.
    """
    if len(member_prompts_js) != len(member_ids) or not member_ids:
        raise ValueError("group_prompt needs one prompt expression per member id")
    ids = ", ".join(member_ids)
    header = f"You own {len(member_ids)} assignments, in this order: {ids}{GROUP_HEADER_TAIL}"
    parts = [_js_string_literal(header)]
    for member_id, prompt in zip(member_ids, member_prompts_js):
        parts.append(_js_string_literal(f"=== Assignment {member_id} ===\n"))
        parts.append(f"({prompt})")
        parts.append(_js_string_literal("\n\n"))
    return " + ".join(parts)

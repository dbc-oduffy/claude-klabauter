"""coordinator_core.hooks.block_workflow_unmodeled_agent — PreToolUse
(Workflow) op.

Ported from DoE-claude `coordinator/hooks/scripts/block-workflow-unmodeled-
agent.py` per docs/plans/2026-09-18-doe-holds-no-scripts.md chunk W4-C9.
Closes the un-modeled `agent()` cost trap documented in DoE-claude's
`coordinator/docs/wiki/workflow-orchestration.md` § "Model selection:
Sonnet by default, Opus is PM-gated": "an un-modeled agent() in an Opus
session is a defect, not a shortcut... there is no warning, no gate, and no
retry." This op is the gate.

Danger condition (deliberately narrow — an offer, not a nag): the EM's main
loop is running on an OPUS-tier model AND launches an inline-script/
scriptPath Workflow whose `agent()` calls carry no `model:` override.
Every un-modeled `agent()` then silently inherits Opus, running every
worker at ~4x cost with zero visible signal in the script or the tool
output.

Behavior (unchanged from source — see that module's own extensive
docstring for the full string/comment-aware JS scanner rationale, the
ternary-undefined fix, and the documented residual parser limitations,
none of which changed in this port):
  - AGENT_N `agent(` call sites; MODELED_N the subset carrying their own
    direct `model:` key.
  - AGENT_N >= 1 AND MODELED_N == 0: each call site's own `agentType:`
    (if any) is resolved against its agent definition
    (`<agents-dir>/<name>.md`, `coordinator:` namespace stripped) and rung
    on that definition's own frontmatter `model:`. If every call site
    resolves this way (whether to sonnet or opus): silent allow, UNLESS a
    resolved-opus call site's agentType is not on the published review
    roster, in which case DENY. If any call site's agentType is absent,
    non-literal, or names no resolvable definition: DENY, leading with the
    fix.
  - AGENT_N >= 1 AND 0 < MODELED_N < AGENT_N: WARN via additionalContext
    advisory only, never a deny.
  - Otherwise: silent allow.

ADAPTATION, THREE PATH-RESOLUTION SITES (the class this package's own
`__init__.py` docstring calls out — every other symbol below is a verbatim
port):
  - `_AGENTS_DIR` (source: `Path(__file__).resolve().parents[2] /
    "agents"`, a `coordinator/agents/` sibling-directory walk valid only
    inside the doctrine-plane checkout the source script shipped from).
  - `_ROSTER_FRAGMENT` (source: `.../contract/review-roster-fragment.json`).
  - `_REVIEW_SIGNALS` (source: `.../contract/review-signals.json`).
  All three are doctrine-plane assets this engine does not ship, resolved
  the same way `oss_operative_strings._resolve_mcp_topology_path` and
  `cater_subagent_start._resolve_role_append_snippet_path` already resolve
  their own doctrine-plane artifacts: probe
  `<claude-config-dir>/plugins/coordinator-claude/coordinator/<rel>` and
  the marketplace-root sibling shape, then fall back to the `.doe-root`
  pointer + `coordinator_core.data_root.content_root_for` rung for a
  dev-clone box. Fail-open to `None`/absent on any miss — `_resolve_call_
  site_tier`, `_tier_walked_agent_types`, `_signal_selected_agent_types`
  already treat an unresolvable asset as "nothing found" (fail-CLOSED to
  the empty roster set for the latter two — see their own docstrings,
  unchanged).

`_git_root()`'s in-process-walk-then-subprocess-fallback is replaced with
`coordinator_core.git.repo_root.show_toplevel` (zero-spawn only, subprocess
fallback dropped) — the same adaptation already made at W4-C4 for the
sibling worktree-strip module.

Session/env reads: `agent_id`, `env`, `transcript_path`, `cwd` are read
from `params` only, never `os.environ` — per this package's established
payload-only-input convention. The override env var
`COORDINATOR_OVERRIDE_WORKFLOW_MODEL_GUARD` and the sentinel-file override
are both preserved; the env leg reads `params["env"]`.

Fail-open guards (all `no_advisory()`), in order, unchanged from source:
explicit override (env or sentinel); `tool_name != "Workflow"`; subagent/
nested call (`agent_id` present); no script text extractable (inline
`script`, or a readable file at `scriptPath`; a `name:`-only launch is out
of scope); no `transcript_path`, or the file at it is missing; session
model undetected or not Opus-tier; `agent_n < 1`.

Spec backlink: cross-repo/inbox/2026-07-13-example-store-repo-em-workflow-
sonnet-default-guard.md (DoE-claude);
docs/plans/2026-09-18-doe-holds-no-scripts.md § W4-C9
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Mapping, Optional

from coordinator_core._hook_envelope import context_only, deny, no_advisory, payload_of
from coordinator_core.git.repo_root import show_toplevel
from coordinator_core.hooks.support.message_envelope import compose, render
from coordinator_core.ipc import register_op

_WIKI_ANCHOR = (
    "coordinator/docs/wiki/guard-message-concision.md"
    "#workflow-model-guard-override-hatches-and-pm-gate"
)

_IDENTIFIER_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_$"
)


# Doctrine-plane asset resolution — see module docstring "ADAPTATION".


def _resolve_doctrine_asset(*rel_parts: str) -> Optional[Path]:
    try:
        from coordinator_core._settings_home import claude_config_dir
        from coordinator_core.data_root import content_root_for
        from coordinator_core.doe_root_pointer import read_doe_root_pointer
    except Exception:
        return None

    rel = Path(*rel_parts)
    plugin_base = claude_config_dir() / "plugins" / "coordinator-claude"
    for candidate_root in (plugin_base / "coordinator", plugin_base):
        candidate = candidate_root / rel
        if candidate.exists():
            return candidate

    try:
        doe_root = read_doe_root_pointer()
    except Exception:
        doe_root = ""
    if doe_root:
        content_root = content_root_for(doe_root)
        if content_root is not None:
            candidate = content_root / rel
            if candidate.exists():
                return candidate

    return None


def _agents_dir() -> Optional[Path]:
    return _resolve_doctrine_asset("agents")


def _roster_fragment_path() -> Optional[Path]:
    return _resolve_doctrine_asset("contract", "review-roster-fragment.json")


def _review_signals_path() -> Optional[Path]:
    return _resolve_doctrine_asset("contract", "review-signals.json")


def _scan_masks(script: str) -> "tuple[bytearray, bytearray]":
    n = len(script)
    string_mask = bytearray(n)
    comment_mask = bytearray(n)
    stack = [("code", 0)]
    i = 0
    while i < n:
        kind, depth = stack[-1]

        if kind in ("code", "interp"):
            two = script[i:i + 2]
            if two == "//":
                comment_mask[i] = 1
                i += 1
                while i < n and script[i] != "\n":
                    comment_mask[i] = 1
                    i += 1
                continue
            if two == "/*":
                comment_mask[i] = 1
                comment_mask[i + 1] = 1
                end_idx = script.find("*/", i + 2)
                end = end_idx + 2 if end_idx != -1 else n
                for k in range(i + 2, end):
                    comment_mask[k] = 1
                i = end
                continue

            ch = script[i]
            if ch == "'":
                string_mask[i] = 1
                stack.append(("squote", 0))
                i += 1
                continue
            if ch == '"':
                string_mask[i] = 1
                stack.append(("dquote", 0))
                i += 1
                continue
            if ch == "`":
                string_mask[i] = 1
                stack.append(("template", 0))
                i += 1
                continue
            if kind == "interp":
                if ch == "{":
                    stack[-1] = ("interp", depth + 1)
                elif ch == "}":
                    if depth == 0:
                        stack.pop()
                    else:
                        stack[-1] = ("interp", depth - 1)
            i += 1
            continue

        if kind in ("squote", "dquote"):
            quote_char = "'" if kind == "squote" else '"'
            ch = script[i]
            string_mask[i] = 1
            if ch == "\\" and i + 1 < n:
                string_mask[i + 1] = 1
                i += 2
                continue
            if ch == quote_char:
                stack.pop()
            i += 1
            continue

        ch = script[i]
        if ch == "\\" and i + 1 < n:
            string_mask[i] = 1
            string_mask[i + 1] = 1
            i += 2
            continue
        if ch == "`":
            string_mask[i] = 1
            stack.pop()
            i += 1
            continue
        if ch == "$" and i + 1 < n and script[i + 1] == "{":
            string_mask[i] = 1
            stack.append(("interp", 0))
            i += 2
            continue
        string_mask[i] = 1
        i += 1

    return string_mask, comment_mask


def _string_mask(script: str) -> bytearray:
    return _scan_masks(script)[0]


def _strip_comments(script: str) -> str:
    _, comment_mask = _scan_masks(script)
    n = len(script)
    out = []
    for i in range(n):
        if comment_mask[i]:
            if script[i] == "\n":
                out.append("\n")
            continue
        out.append(script[i])
    return "".join(out)


_UNDEFINED_NULL_RE = re.compile(r"\b(?:undefined|null)\b")


def _extract_model_value(buf: str, mask: "bytearray", start: int, n: int) -> str:
    local_paren = 0
    local_brace = 0
    k = start
    while k < n:
        if mask[k]:
            k += 1
            continue
        ch = buf[k]
        if ch == "(":
            local_paren += 1
        elif ch == ")":
            if local_paren == 0:
                break
            local_paren -= 1
        elif ch == "{":
            local_brace += 1
        elif ch == "}":
            if local_brace == 0:
                break
            local_brace -= 1
        elif ch == "," and local_paren == 0 and local_brace == 0:
            break
        k += 1
    return buf[start:k]


def _extract_string_literal(value: str) -> Optional[str]:
    trimmed = value.strip()
    if len(trimmed) < 2:
        return None
    quote = trimmed[0]
    if quote not in ("'", '"', "`") or trimmed[-1] != quote:
        return None
    inner = trimmed[1:-1]
    if "${" in inner:
        return None
    return inner


def _walk_agent_calls(buf: str, mask: "bytearray") -> "list[tuple[bool, Optional[str]]]":
    n = len(buf)
    i = 0
    calls: "list[tuple[bool, Optional[str]]]" = []

    while i < n:
        pos = buf.find("agent(", i)
        if pos == -1:
            break
        if mask[pos] or (pos > 0 and buf[pos - 1] in _IDENTIFIER_CHARS):
            i = pos + 1
            continue
        call_start = pos
        paren_open = call_start + 5

        depth = 0
        brace_depth = 0
        frame_braces = {1: 0}
        past_prompt = False
        j = paren_open
        close_idx = -1
        has_direct_model = False
        agent_type: Optional[str] = None
        while j < n:
            if mask[j]:
                j += 1
                continue
            ch = buf[j]
            if ch == "(":
                depth += 1
                frame_braces[depth] = 0
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    close_idx = j
                    break
            elif ch == "{":
                frame_braces[depth] = frame_braces.get(depth, 0) + 1
                if depth == 1:
                    brace_depth += 1
            elif ch == "}":
                frame_braces[depth] = frame_braces.get(depth, 0) - 1
                if depth == 1:
                    brace_depth -= 1
            elif ch == "," and depth == 1 and brace_depth == 0:
                past_prompt = True
            elif (
                ch == "m"
                and depth >= 2
                and past_prompt
                and frame_braces.get(depth) == 1
                and buf[j:j + 6] == "model:"
                and not any(mask[j:j + 6])
                and not (j > 0 and not mask[j - 1] and buf[j - 1] in _IDENTIFIER_CHARS)
            ):
                value = _extract_model_value(buf, mask, j + 6, n)
                masked_value = "".join(
                    ch2 if not mask[j + 6 + idx] else " " for idx, ch2 in enumerate(value)
                )
                if not _UNDEFINED_NULL_RE.search(masked_value):
                    has_direct_model = True
            elif ch == "m" and depth == 1 and brace_depth == 1:
                if (
                    buf[j:j + 6] == "model:"
                    and not any(mask[j:j + 6])
                    and not (j > 0 and not mask[j - 1] and buf[j - 1] in _IDENTIFIER_CHARS)
                ):
                    value = _extract_model_value(buf, mask, j + 6, n)
                    masked_value = "".join(
                        ch2 if not mask[j + 6 + idx] else " "
                        for idx, ch2 in enumerate(value)
                    )
                    if not _UNDEFINED_NULL_RE.search(masked_value):
                        has_direct_model = True
            elif ch == "a" and depth == 1 and brace_depth == 1:
                if (
                    buf[j:j + 10] == "agentType:"
                    and not any(mask[j:j + 10])
                    and not (j > 0 and not mask[j - 1] and buf[j - 1] in _IDENTIFIER_CHARS)
                ):
                    raw_value = _extract_model_value(buf, mask, j + 10, n)
                    literal = _extract_string_literal(raw_value)
                    if literal is not None:
                        agent_type = literal
            j += 1

        if close_idx == -1:
            i = n
            continue

        calls.append((has_direct_model, agent_type))
        i = close_idx + 1

    return calls


def _count_agent_modeled_with_types(buf: str) -> "tuple[int, int, list[Optional[str]]]":
    mask = _string_mask(buf)
    calls = _walk_agent_calls(buf, mask)
    agent_n = len(calls)
    modeled_n = sum(1 for has_model, _ in calls if has_model)
    agent_types = [agent_type for _, agent_type in calls]
    return agent_n, modeled_n, agent_types


_COORDINATOR_AGENT_TYPE_PREFIX = "coordinator:"
_FRONTMATTER_MODEL_RE = re.compile(r"^model:\s*(\S+)\s*$", re.MULTILINE)


def _tier_walked_agent_types() -> "frozenset[str]":
    try:
        path = _roster_fragment_path()
        if path is None:
            return frozenset()
        with open(path, "r", encoding="utf-8") as fh:
            fragment = json.load(fh)
        tiers = fragment.get("tiers")
        if not isinstance(tiers, dict):
            return frozenset()
        names = set()
        for tier in tiers.values():
            if not isinstance(tier, dict):
                continue
            for stage in tier.get("stages") or []:
                if isinstance(stage, dict):
                    names.update(a for a in stage.get("agents") or [] if isinstance(a, str))
        return frozenset(names)
    except Exception:
        return frozenset()


def _signal_selected_agent_types() -> "frozenset[str]":
    try:
        path = _review_signals_path()
        if path is None:
            return frozenset()
        with open(path, "r", encoding="utf-8") as fh:
            contract = json.load(fh)
        signals = contract.get("signals")
        if not isinstance(signals, dict):
            return frozenset()
        names = set()
        for signal in signals.values():
            if not isinstance(signal, dict):
                continue
            selects = signal.get("selects")
            if isinstance(selects, str):
                names.add(selects)
        return frozenset(names)
    except Exception:
        return frozenset()


def _rostered_agent_types() -> "frozenset[str]":
    return _tier_walked_agent_types() | _signal_selected_agent_types()


def _resolve_call_site_tier(agent_type: Optional[str]) -> Optional[str]:
    if not agent_type:
        return None
    name = agent_type
    if name.startswith(_COORDINATOR_AGENT_TYPE_PREFIX):
        name = name[len(_COORDINATOR_AGENT_TYPE_PREFIX):]
    if not name:
        return None
    agents_dir = _agents_dir()
    if agents_dir is None:
        return None
    path = agents_dir / f"{name}.md"
    try:
        text = path.read_text(encoding="utf-8")
    except Exception:
        return None
    if not text.startswith("---"):
        return None
    end = text.find("\n---", 3)
    if end == -1:
        return None
    m = _FRONTMATTER_MODEL_RE.search(text[:end])
    if not m:
        return None
    return m.group(1).strip().lower()


_MODEL_LINE_RE = re.compile(r'"model"\s*:\s*"(claude-[^"]*)"')


def _detect_opus(transcript_path: str) -> bool:
    if not transcript_path or not os.path.isfile(transcript_path):
        return False
    try:
        with open(transcript_path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                m = _MODEL_LINE_RE.search(line)
                if m:
                    return "opus" in m.group(1).lower()
    except Exception:
        return False
    return False


_OVERRIDE_SENTINEL_NAME = ".coordinator-override-workflow-model-guard"


def _sentinel_override_active() -> bool:
    try:
        root = show_toplevel()
    except Exception:
        root = None
    if not root:
        return False
    try:
        return os.path.isfile(os.path.join(root, _OVERRIDE_SENTINEL_NAME))
    except Exception:
        return False


def _env_value(env: object, key: str) -> Optional[str]:
    if not isinstance(env, Mapping):
        return None
    value = env.get(key)
    return value if isinstance(value, str) else None


def _compose_zero_modeled_deny_reason(agent_n: int, env: object = None) -> str:
    message = compose(
        f"{agent_n} agent() call(s), no model: -- inherits Opus (~4x cost). "
        "Add model: 'sonnet', or override.",
        anchor=_WIKI_ANCHOR,
    )
    return render(message, env=env)


def _compose_unrostered_opus_deny_reason(
    agent_types: "list[Optional[str]]", env: object = None
) -> str:
    named = ", ".join(sorted({a for a in agent_types if a})[:3]) or "an agentType"
    message = compose(
        f"{named} resolves to Opus and is not on the review roster -- Opus in "
        "a workflow is for review (~4x cost). Use a rostered reviewer, or "
        "model: 'sonnet'.",
        anchor=_WIKI_ANCHOR,
    )
    return render(message, env=env)


def _compose_partial_modeled_context(
    agent_n: int, modeled_n: int, env: object = None
) -> str:
    message = compose(
        f"{agent_n} agent() calls, only {modeled_n} set model: -- rest may "
        "inherit Opus (~4x cost) unless intended, with PM approval.",
        anchor=_WIKI_ANCHOR,
    )
    return render(message, env=env)


@register_op("hooks.block_workflow_unmodeled_agent")
def _handler(params: dict, repo_root=None) -> dict:
    params = payload_of(params)
    env = params.get("env")
    if _env_value(env, "COORDINATOR_OVERRIDE_WORKFLOW_MODEL_GUARD") == "1":
        return no_advisory()
    if _sentinel_override_active():
        return no_advisory()

    if params.get("tool_name", "") != "Workflow":
        return no_advisory()

    if params.get("agent_id"):
        return no_advisory()

    tool_input = params.get("tool_input")
    if not isinstance(tool_input, Mapping):
        tool_input = {}

    script = tool_input.get("script", "") or ""

    if not script:
        script_path = tool_input.get("scriptPath", "") or ""
        if not script_path:
            return no_advisory()
        try:
            if not os.path.isfile(script_path):
                return no_advisory()
            with open(script_path, "r", encoding="utf-8", errors="replace") as fh:
                script = fh.read(1_000_000)
        except Exception:
            return no_advisory()
        if not script:
            return no_advisory()

    transcript_path = params.get("transcript_path", "") or ""
    if not transcript_path or not os.path.isfile(transcript_path):
        return no_advisory()

    if not _detect_opus(transcript_path):
        return no_advisory()

    stripped = _strip_comments(script)
    agent_n, modeled_n, agent_types = _count_agent_modeled_with_types(stripped)

    if agent_n < 1:
        return no_advisory()

    if modeled_n == 0:
        tiers = [_resolve_call_site_tier(agent_type) for agent_type in agent_types]
        if all(tier is not None for tier in tiers):
            if all(
                tier != "opus" or agent_type in _rostered_agent_types()
                for tier, agent_type in zip(tiers, agent_types)
            ):
                return no_advisory()
            reason = _compose_unrostered_opus_deny_reason(
                [
                    agent_type
                    for tier, agent_type in zip(tiers, agent_types)
                    if tier == "opus" and agent_type not in _rostered_agent_types()
                ],
                env,
            )
            return deny("PreToolUse", reason)
        reason = _compose_zero_modeled_deny_reason(agent_n, env)
        return deny("PreToolUse", reason)

    if modeled_n < agent_n:
        msg = _compose_partial_modeled_context(agent_n, modeled_n, env)
        return context_only("PreToolUse", msg)

    return no_advisory()

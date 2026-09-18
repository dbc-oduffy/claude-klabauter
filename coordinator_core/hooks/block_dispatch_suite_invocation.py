"""coordinator_core.hooks.block_dispatch_suite_invocation — PreToolUse
(Agent, Workflow) op.

Ported from DoE-claude `coordinator/hooks/scripts/block-dispatch-suite-
invocation.py` per docs/plans/2026-09-18-doe-holds-no-scripts.md chunk
W4-C9. Layer 2 of the DR-088 ladder: closes the "EM typed a suite command
into a dispatch brief" gap that layer 3 (PreToolUse(Bash), this repo's own
`coordinator_core.bash_guards.check_test_suite_invocation`) cannot reach,
because layer 3 fires on the dispatched subagent's OWN Bash call, not on
the dispatching EM's Agent()/Workflow() call that hands the subagent its
prompt in the first place.

THIS IS A REGISTRATION HOOK, NOT A CLASSIFIER (unchanged from source). All
suite-shaped-command judgement lives in this repo's own
`coordinator_core.bash_guards.check_test_suite_invocation.classify_text` /
`classify_text_precision` — this module owns ZERO test-runner names, ZERO
command grammar, ZERO regex over suite invocations.

ADAPTATION, load-bearing: the source script lived DOCTRINE-side and
resolved `coordinator_core` across a repo boundary (`_engine_root.
resolve_claude_klabauter_root()` + a `sys.path` insert) before it could import the
classifier at all — an infra leg that could itself fail open. This module
now lives INSIDE the engine that ships the classifier, so `_classify`/
`_classify_precision` import `coordinator_core.bash_guards.
check_test_suite_invocation` directly; the whole resolve-a-sibling-root
rung is deleted, not merely bypassed. Failure of the import itself (should
never happen for an in-tree sibling, but kept fail-open per the source's own
contract) still degrades to `[]` (silent allow) rather than raising.

`position` handling — gate on "imperative" only, unchanged: a brief that
quotes a suite command inside a fence, inline code, or a negation is
legitimate authoring content. Only `position == "imperative"` denies.

Two legs, at most one envelope per call (unchanged): the IDENTITY leg (a
suite-shaped imperative command) denies first; only when it does NOT fire
does the PRECISION leg (a bare directory positional in imperative position
— Tier T precision an executor is owed, refused downstream at R9, so
denying it here saves a silent breadth-narrowing round trip) get a chance.

Override — THREE hatches, unchanged in shape and precedence: (1) an
in-prompt marker (`COORDINATOR-OVERRIDE-DISPATCH-SUITE-GUARD: <reason>`,
own line, non-empty reason), honored ONLY in text the dispatching EM
authored in THIS tool call (Agent `prompt`, Workflow inline `script`) —
never text read from a `scriptPath` file on disk; (2)
`COORDINATOR_OVERRIDE_DISPATCH_SUITE_GUARD=1`, read from `params["env"]`
(never `os.environ` — the resident engine serves ~50 concurrent sessions
and its own process environment belongs to none of them); (3) a repo-root
`.coordinator-override-dispatch-suite-guard` sentinel file.

`_git_root()` for the sentinel leg is replaced with `coordinator_core.git.
repo_root.show_toplevel` (zero-spawn, `params["cwd"]`-anchored via that
function's own cwd-walk contract) rather than the source's raw
`os.getcwd()` walk — this process's own cwd belongs to no particular
session; `show_toplevel` is the established zero-spawn primitive every
sibling op in this package already uses for the identical resolution.

Fail-open guards (in order, all `no_advisory()`): both override legs;
`tool_name` not in {"Agent", "Workflow"}; subagent/nested call (`agent_id`
present); no dispatch prompt text extractable; the in-prompt override
marker (payload-dependent, checked after text extraction); any classifier
import/call failure (degrades to `[]`, treated as "no matches").

Spec backlink: cross-repo/inbox/2026-07-28-example-market-data-repo-em-
dispatched-agent-scoped-test-breadth.md (DoE-claude);
docs/plans/2026-07-23-dr-088-ladder-enforcement-layers.md § C8 (DoE-claude);
docs/plans/2026-09-18-doe-holds-no-scripts.md § W4-C9
"""

from __future__ import annotations

import os
import re
from typing import Any, Mapping, NamedTuple, Optional

from coordinator_core._hook_envelope import deny, no_advisory
from coordinator_core.git.repo_root import show_toplevel
from coordinator_core.hooks.support.message_envelope import compose, render
from coordinator_core.ipc import register_op

_OVERRIDE_ENV = "COORDINATOR_OVERRIDE_DISPATCH_SUITE_GUARD"
_OVERRIDE_SENTINEL_NAME = ".coordinator-override-dispatch-suite-guard"

_OVERRIDE_MARKER_PREFIX = "COORDINATOR-OVERRIDE-DISPATCH-SUITE-GUARD:"
_OVERRIDE_MARKER_RE = re.compile(
    r"^[ \t]*" + re.escape(_OVERRIDE_MARKER_PREFIX) + r"[ \t]*(\S.*)?$",
    re.MULTILINE,
)

_WIKI_ANCHOR = (
    "coordinator/docs/wiki/guard-message-concision.md"
    "#dispatch-suite-guard-overrides-and-directory-breadth-advisory"
)


class _DispatchText(NamedTuple):
    text: str
    caller_authored: bool


def _env_value(env: object, key: str) -> Optional[str]:
    if not isinstance(env, Mapping):
        return None
    value = env.get(key)
    return value if isinstance(value, str) else None


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


def _extract_dispatch_text(tool_name: str, tool_input: Mapping) -> _DispatchText:
    if tool_name == "Agent":
        prompt = tool_input.get("prompt", "") or ""
        text = prompt if isinstance(prompt, str) else ""
        return _DispatchText(text, caller_authored=True)

    script = tool_input.get("script", "") or ""
    if isinstance(script, str) and script:
        return _DispatchText(script, caller_authored=True)

    script_path = tool_input.get("scriptPath", "") or ""
    if not script_path or not isinstance(script_path, str):
        return _DispatchText("", caller_authored=False)
    try:
        if not os.path.isfile(script_path):
            return _DispatchText("", caller_authored=False)
        with open(script_path, "r", encoding="utf-8", errors="replace") as fh:
            return _DispatchText(fh.read(1_000_000), caller_authored=False)
    except Exception:
        return _DispatchText("", caller_authored=False)


def _has_override_marker(text: str) -> bool:
    for match in _OVERRIDE_MARKER_RE.finditer(text):
        reason = match.group(1)
        if reason and reason.strip():
            return True
    return False


def _classify(text: str, cwd: Optional[str]) -> "list[Any]":
    try:
        from coordinator_core.bash_guards.check_test_suite_invocation import (
            classify_text,
        )
    except Exception:
        return []
    try:
        return list(classify_text(text, cwd=cwd))
    except Exception:
        return []


def _classify_precision(text: str, cwd: Optional[str]) -> "list[Any]":
    try:
        from coordinator_core.bash_guards.check_test_suite_invocation import (
            classify_text_precision,
        )
    except Exception:
        return []
    try:
        return list(classify_text_precision(text, cwd=cwd))
    except Exception:
        return []


def _precision_deny_envelope(text: str, cwd: Optional[str]) -> Optional[dict]:
    try:
        precision_matches = _classify_precision(text, cwd)
        imperative = [
            m for m in precision_matches if getattr(m, "position", "") == "imperative"
        ]
        directory_hits = [m for m in imperative if getattr(m, "directory_args", None)]
        if not directory_hits:
            return None

        hit = directory_hits[0]
        detected = getattr(hit, "detected", "a test-runner invocation")
        directory_args = list(getattr(hit, "directory_args", []) or [])
        dirs_desc = ", ".join(repr(d) for d in directory_args) or "a directory"

        message = compose(
            f"{detected} targets directory {dirs_desc} -- a brief may not "
            "carry a Tier-F/U command. Name the touched test files or node "
            "ids, or use the documented per-dispatch override.",
            anchor=_WIKI_ANCHOR,
        )
        return deny("PreToolUse", render(message))
    except Exception:
        return None


def _compose_precision_deny_reason(tool_name: str, detected: str, tier: str) -> str:
    message = compose(
        f"{tool_name}: Tier-{tier} suite command ({detected}) -- overridable "
        "for this one dispatch; see the doc for how.",
        anchor=_WIKI_ANCHOR,
    )
    return render(message)


@register_op("hooks.block_dispatch_suite_invocation")
async def _handler(params: dict, repo_root=None) -> dict:
    """PreToolUse(Agent, Workflow) op: deny a dispatch brief carrying a
    suite-shaped imperative command or a directory-scoped Tier-F/U one."""
    env = params.get("env")
    if _env_value(env, _OVERRIDE_ENV) == "1":
        return no_advisory()
    if _sentinel_override_active():
        return no_advisory()

    tool_name = params.get("tool_name") or ""
    if tool_name not in ("Agent", "Workflow"):
        return no_advisory()

    if params.get("agent_id"):
        return no_advisory()

    tool_input = params.get("tool_input")
    if not isinstance(tool_input, Mapping):
        tool_input = {}

    dispatch = _extract_dispatch_text(tool_name, tool_input)
    text = dispatch.text
    if not text:
        return no_advisory()

    if dispatch.caller_authored and _has_override_marker(text):
        return no_advisory()

    cwd = params.get("cwd") if isinstance(params.get("cwd"), str) else None

    matches = _classify(text, cwd)
    imperative = (
        [m for m in matches if getattr(m, "position", "") == "imperative"]
        if matches
        else []
    )
    if not imperative:
        envelope = _precision_deny_envelope(text, cwd)
        if envelope is None:
            return no_advisory()
        return envelope

    hit = imperative[0]
    detected = getattr(hit, "detected", "a test-suite command")
    tier = getattr(hit, "tier", "U")

    reason = _compose_precision_deny_reason(tool_name, detected, tier)
    return deny("PreToolUse", reason)

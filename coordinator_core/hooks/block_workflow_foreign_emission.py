"""coordinator_core.hooks.block_workflow_foreign_emission — PreToolUse
(Workflow) op.

Ported from DoE-claude `coordinator/hooks/scripts/block-workflow-foreign-
emission.py` per docs/plans/2026-09-18-doe-holds-no-scripts.md chunk W4-C9.
Refuses to fire a `Workflow` script this session did not emit.

The window this closes (unchanged from source). `dispatch.emit`/the
`emit-dispatch-workflow` launcher writes its `.mjs` to a path that is a
deterministic function of the plan alone, so every session emitting that
plan targets the same file. `/execute-plan` then fires it with
`Workflow({scriptPath})` from the EM's own loop — a SEPARATE read of the
file, minutes after the emit in a busy session. A peer writing that path in
between is invisible: the run executes their wave map under this session's
handle, and neither the tool result nor the run handle says so.

Fail-open guards (all `no_advisory()`), in order: `tool_name != "Workflow"`;
no `scriptPath` (an inline `script` carries its own bytes — no disk read for
a peer to race); the script file does not exist (the tool's own not-found
error owns this); no receipt beside the script (a hand-authored or
checked-in workflow was never emitted and has no provenance to check);
receipt unreadable or malformed; this session's id is undetectable, or the
receipt recorded none.

DENIES on three conditions: (1) sha mismatch — the file changed after it was
emitted, by any route; (2) session mismatch — the receipt names a DIFFERENT
session as emitter; (3) no receipt at all while at least one `agent()` call is
untyped or write-capable — a hand-rolled dispatch with no provenance and no
per-agent cost/write attribution.

§ Read-only exemption. A script whose every `agent()` call resolves to a
read-only `agentType` (e.g. `Explore`, or a read-only coordinator role such
as `premise-checker`) — or that dispatches no `agent()` at all — poses none
of the risk a receipt exists to track, and is exempt. Resolution order:
`_READ_ONLY_AGENT_TYPE_ALLOWLIST` FIRST (curated: `premise-checker` carries
`Write`/`Bash` for scratch output, not repo writes, so its raw `tools:`
would misclassify it), then the agent definition's own `tools:` frontmatter
(`_agents_dir()`, the probe `block_workflow_unmodeled_agent` uses) for
everything the allowlist does not name. An agentType this cannot positively confirm read-only either
way is NOT exempt — fails closed, not open, because the guard this exemption
carves into is itself the fail-SAFE default.

§ Sanctioned emitters. The receipt SHAPE is producer-agnostic (module
docstring above, `dispatch_emit.op` § "The receipt is a property of
emitting, not of one repo's wrapper") — any `<script>.emitted.json` that
verifies (`sha256` matches, `session_id` matches or is absent) is honoured
regardless of which emitter wrote it. `coordinator/bin/emit-wave-fire.py`
writes this same shape beside every fire it emits (see that module's own
`_write_fire_receipt`), so its fires are sanctioned by construction — no
special-casing by emitter name is needed or done here.

REMEDIATION STRING NAMES THE S1-SHIPPED LAUNCHER (per this row's own body):
`coordinator/bin/emit-dispatch-workflow.py` is now a thin door-served CLI
(S1-C7) invoked through its settings-home launcher, not run directly with
`python3 <plugin-root>/bin/emit-dispatch-workflow.py` — the source script's
own `_emitter_invocation`/`_emitter_root_candidates` (a `CLAUDE_PLUGIN_ROOT`/
`.doe-root`-probing `python3 <path>` prefix) is REPLACED with
`_emitter_launcher_invocation`, which names
`<settings-home>/bin/emit-dispatch-workflow` — the launcher
`scripts/setup.py` writes for every warm-allowlisted entrypoint (this
package's own W4-C9 arrival record and S1-C9's "Register slice 1" row). No
on-disk probing is needed: `coordinator_core._settings_home.settings_home()`
resolves the same settings-home root the launcher itself lives under,
independent of whether this hot-path process's cwd is inside any particular
checkout.

Session id is read from `params` only — never `os.environ` — per this
package's established payload-only-input convention.

Spec backlink: docs/plans/2026-09-18-doe-holds-no-scripts.md § W4-C9
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Mapping, Optional

from coordinator_core._hook_envelope import deny, no_advisory, payload_of
from coordinator_core._settings_home import settings_home
from coordinator_core.ipc import register_op

#: Named fallback for a read-only `agentType` whose own agent definition
#: cannot be resolved in-process (§ Read-only exemption). Bare and
#: `coordinator:`-prefixed forms both admitted, case-insensitively.
_READ_ONLY_AGENT_TYPE_ALLOWLIST = frozenset({"explore", "premise-checker"})

#: A `tools:` frontmatter entry naming any of these makes the agent
#: write-capable — the same four tool names `guard_host_subagent_bash_ban`/
#: this package's other write-capability checks key on.
_WRITE_CAPABLE_TOOLS = frozenset({"Edit", "Write", "MultiEdit", "NotebookEdit", "Bash"})

_TOOLS_LINE_RE = re.compile(r"^tools:\s*(.+)$", re.MULTILINE)


def _session_id(params: dict) -> "str | None":
    session_id = params.get("session_id")
    if isinstance(session_id, str) and session_id:
        return session_id
    env = params.get("env")
    if isinstance(env, Mapping):
        for key in ("CLAUDE_SESSION_ID", "CLAUDE_CODE_SESSION_ID"):
            value = env.get(key)
            if isinstance(value, str) and value:
                return value
    return None


def _emitter_launcher_invocation() -> str:
    """A copy-pasteable settings-home launcher invocation — see module
    docstring "REMEDIATION STRING NAMES THE S1-SHIPPED LAUNCHER"."""
    try:
        launcher = settings_home() / "bin" / "emit-dispatch-workflow"
        return f'"{launcher}"'
    except Exception:
        return "emit-dispatch-workflow  # settings-home unresolvable here"


def _sha_mismatch_reason(
    script: Path, receipt_path: Path, recorded_sha: str, actual_sha: str
) -> str:
    launcher = _emitter_launcher_invocation()
    return (
        f"{script.name} changed after emission (sha {recorded_sha[:12]}; disk "
        f"{actual_sha[:12]}) -- refusing to fire.\n\n"
        "Use instead: edited it, re-stamp --\n"
        f"  {launcher} --restamp "
        f'"{script}"\n'
        "Use instead: didn't edit it, re-emit --\n"
        f"  {launcher} --plan <plan-path>"
    )


def _session_mismatch_reason(script: Path, session: str, recorded_session: str) -> str:
    launcher = _emitter_launcher_invocation()
    return (
        f"{script.name} was emitted by a DIFFERENT session -- refusing to fire.\n\n"
        f"Its receipt names session {recorded_session[:8]}; this session is "
        f"{session[:8]}. The emitted path is a deterministic function of the plan, "
        "so a peer working the same plan targets the same file. Firing it would "
        "run THEIR wave map under your handle, and nothing in the handle would "
        "say so.\n\n"
        "Fix: re-emit before firing --\n"
        f"  {launcher} --plan <plan-path>\n"
        "The emitter refuses to overwrite a differing emission, so a refusal "
        "there means coordinate with that session rather than --force past it."
    )


def _no_receipt_deny_reason(script_name: str) -> str:
    launcher = _emitter_launcher_invocation()
    return (
        f"{script_name}: hand-rolled Workflow with an untyped or write-capable "
        f"agent() call and no receipt. Emit it: {launcher} --plan <plan-path> "
        "(read-only fan-outs are exempt)."
    )


def _agent_type_tools(agent_type: str) -> "Optional[frozenset[str]]":
    """The `tools:` frontmatter set an agent definition declares, or `None`
    when the definition (or its `tools:` line) cannot be resolved in-process.

    `agent_type` is expected already stripped of a `coordinator:` prefix --
    `_agent_type_is_read_only` (this function's one caller) normalizes that
    once before calling.

    `None` is deliberately distinct from an empty set: an unresolvable
    definition says nothing about write-capability, while a definition that
    resolves and carries `tools: []` (or no `tools:` line at all, read as
    "unrestricted" per the harness's own default) is a positive answer.
    """
    try:
        from coordinator_core.hooks.block_workflow_unmodeled_agent import _agents_dir
    except Exception:
        return None
    agents_dir = _agents_dir()
    if agents_dir is None:
        return None
    name = agent_type
    if not name:
        return None
    try:
        text = (agents_dir / f"{name}.md").read_text(encoding="utf-8")
    except Exception:
        return None
    if not text.startswith("---"):
        return None
    end = text.find("\n---", 3)
    if end == -1:
        return None
    m = _TOOLS_LINE_RE.search(text[:end])
    if not m:
        # No `tools:` line at all reads as "every tool available" per the
        # harness's own frontmatter default -- NOT read-only.
        return None
    raw = m.group(1).strip()
    if raw.startswith("[") and raw.endswith("]"):
        raw = raw[1:-1]
    return frozenset(t.strip() for t in raw.split(",") if t.strip())


def _agent_type_is_read_only(agent_type: "Optional[str]") -> bool:
    """§ Read-only exemption: does `agent_type` resolve to a role with no
    write-capable tool? Fails CLOSED (not read-only) whenever it cannot
    positively confirm the answer -- see module docstring."""
    if not agent_type:
        return False
    name = agent_type.strip()
    if name.lower().startswith("coordinator:"):
        name = name[len("coordinator:"):]
    # Allowlist before frontmatter: see module docstring (premise-checker).
    if name.lower() in _READ_ONLY_AGENT_TYPE_ALLOWLIST:
        return True
    tools = _agent_type_tools(name)
    if tools is not None:
        return not (tools & _WRITE_CAPABLE_TOOLS)
    return False


def _every_agent_call_is_read_only(script_text: str) -> bool:
    """True when `script_text` dispatches no `agent()` at all, or every call
    resolves to a read-only `agentType` (§ Read-only exemption). An unparsed
    scanner failure fails CLOSED -- the guard keeps firing, never silently
    exempted by a scan it could not complete."""
    try:
        from coordinator_core.hooks.block_workflow_unmodeled_agent import (
            _string_mask,
            _strip_comments,
            _walk_agent_calls,
        )

        stripped = _strip_comments(script_text)
        calls = _walk_agent_calls(stripped, _string_mask(stripped))
    except Exception:
        return False
    if not calls:
        return True
    return all(_agent_type_is_read_only(agent_type) for _has_model, agent_type in calls)


@register_op("hooks.block_workflow_foreign_emission")
def _handler(params: dict, repo_root=None) -> dict:
    """PreToolUse(Workflow) op: refuse to fire a script this session did not
    emit."""
    # Normalize the two params shapes
    # both engine doors and the cold chain send (see block_worktree_tool).
    params = payload_of(params)
    if params.get("tool_name") != "Workflow":
        return no_advisory()

    tool_input = params.get("tool_input")
    if not isinstance(tool_input, Mapping):
        return no_advisory()

    script_path = tool_input.get("scriptPath")
    inline_script = tool_input.get("script")

    if not script_path:
        # No scriptPath at all: an inline `script:` can never carry a
        # receipt (nothing on disk to write one beside) -- § Read-only
        # exemption is the only thing that can silence this guard for it.
        # A `name:`-only launch (neither key) has no text to scan and stays
        # out of scope, same as before this row.
        if not isinstance(inline_script, str) or not inline_script.strip():
            return no_advisory()
        if _every_agent_call_is_read_only(inline_script):
            return no_advisory()
        return deny("PreToolUse", _no_receipt_deny_reason("this inline script"))

    script = Path(script_path)
    if not script.is_absolute():
        script = Path(params.get("cwd") or ".") / script
    if not script.is_file():
        return no_advisory()  # the tool's own not-found error owns this

    receipt_path = script.with_name(script.name + ".emitted.json")
    if not receipt_path.is_file():
        try:
            script_text = script.read_text(encoding="utf-8", errors="replace")
        except Exception:
            return no_advisory()  # cannot read the script -- nothing to scan or deny
        if _every_agent_call_is_read_only(script_text):
            return no_advisory()
        return deny("PreToolUse", _no_receipt_deny_reason(script.name))

    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        recorded_sha = receipt["sha256"]
        recorded_session = receipt.get("session_id")
    except Exception:
        return no_advisory()

    try:
        actual_sha = hashlib.sha256(script.read_bytes()).hexdigest()
    except Exception:
        return no_advisory()

    if actual_sha != recorded_sha:
        return deny(
            "PreToolUse",
            _sha_mismatch_reason(script, receipt_path, recorded_sha, actual_sha),
        )

    session = _session_id(params)
    if session is None or recorded_session is None:
        return no_advisory()
    if session != recorded_session:
        return deny(
            "PreToolUse",
            _session_mismatch_reason(script, session, recorded_session),
        )

    return no_advisory()

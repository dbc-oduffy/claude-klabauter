"""coordinator_core.hooks.block_workflow_foreign_emission — PreToolUse
(Workflow) op.

Ported from coordinator-content-repo `coordinator/hooks/scripts/block-workflow-foreign-
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

A fourth DENY, checked last (after sha and session): a `resumeFromRunId` fire of
`<root>/scratch/warp/<run_id>.workflow.mjs` whose record dir holds a `halted` marker
stamped `revised_at` (`halted_run.revised_halt`) — a resume replays the stages without the
revision; the reason names the fresh `fire_command`. Any failure there falls through.

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
pointer-probing `python3 <path>` prefix) is REPLACED with
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
from pathlib import Path, PurePosixPath
from typing import Mapping, Optional

from coordinator_core._hook_envelope import deny, no_advisory, payload_of
from coordinator_core._settings_home import settings_home
from coordinator_core.ipc import register_op
from coordinator_core.machine_profile import apply_guard_level

GUARD_NAME = "block-workflow-foreign-emission"


def _resolved_deny(reason: str) -> dict:
    return apply_guard_level(GUARD_NAME, deny("PreToolUse", reason)) or no_advisory()

_READ_ONLY_AGENT_TYPE_ALLOWLIST = frozenset({"explore", "premise-checker"})

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


def _plan_amended_reason(script: Path, receipt: Mapping) -> "Optional[str]":
    """Deny reason when the receipt's plan body sha differs from the plan file's current
    canonical body sha, else None. A receipt without the keys, a missing plan, or an
    unreadable plan yields None. The repo root is walked from the SCRIPT's parents, so a script
    emitted outside the checkout is never checked (fail-open)."""
    try:
        recorded = receipt.get("plan_body_sha")
        rel = receipt.get("plan_relpath")
        if not (isinstance(recorded, str) and recorded and isinstance(rel, str) and rel):
            return None
        from coordinator_core.frontmatter.primitives import canonical_body_sha

        root = next(
            (p for p in script.resolve().parents if (p / ".git").exists()), None
        )
        if root is None:
            return None
        plan = root / rel
        if not plan.is_file():
            return None
        current = canonical_body_sha(plan.read_text(encoding="utf-8"))
        if not current or current == recorded:
            return None
        launcher = _emitter_launcher_invocation()
        return (
            f"{script.name} embeds a plan body that changed since emission "
            f"(plan sha {recorded[:12]}; now {current[:12]}) -- refusing to fire.\n"
            "Fix: re-emit --\n"
            f"  {launcher} --plan {rel}"
        )
    except (OSError, UnicodeDecodeError, ValueError):
        return None


def _no_receipt_deny_reason(script_name: str) -> str:
    launcher = _emitter_launcher_invocation()
    return (
        f"{script_name}: hand-rolled Workflow with an untyped or write-capable "
        f"agent() call and no receipt. Emit it: {launcher} --plan <plan-path>, "
        f"or {launcher} --pipeline <name> --brief <text> "
        "(read-only fan-outs are exempt)."
    )


def _agent_type_tools(agent_type: str) -> "Optional[frozenset[str]]":
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
        return None
    raw = m.group(1).strip()
    if raw.startswith("[") and raw.endswith("]"):
        raw = raw[1:-1]
    return frozenset(t.strip() for t in raw.split(",") if t.strip())


def _agent_type_is_read_only(agent_type: "Optional[str]") -> bool:
    if not agent_type:
        return False
    name = agent_type.strip()
    if name.lower().startswith("coordinator:"):
        name = name[len("coordinator:"):]
    if name.lower() in _READ_ONLY_AGENT_TYPE_ALLOWLIST:
        return True
    tools = _agent_type_tools(name)
    if tools is not None:
        return not (tools & _WRITE_CAPABLE_TOOLS)
    return False


def _every_agent_call_is_read_only(script_text: str) -> bool:
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


def _resume_after_revise_reason(script: Path) -> "Optional[str]":
    """Deny reason when `script` is `<root>/scratch/warp/<run_id>.workflow.mjs` of a halted run
    revised after its halt, else None. Any failure yields None."""
    try:
        from coordinator_core.ops.dispatch_emit import halted_run
        from coordinator_core.ops.dispatch_emit.ask_contract import RUN_DIR_ROOT, record_dir
        from coordinator_core.warp_fire import fire_command

        parts = PurePosixPath(str(script).replace("\\", "/")).parts
        suffix = ".workflow.mjs"
        run_root = tuple(RUN_DIR_ROOT.split("/"))
        n = len(run_root)
        if len(parts) < n + 2 or parts[-1 - n:-1] != run_root or not parts[-1].endswith(suffix):
            return None
        run_id = parts[-1][: -len(suffix)]
        norm = str(script).replace("\\", "/")
        root = Path(norm[: len(norm) - len("/".join(parts[-1 - n:]))].rstrip("/") or "/")
        rec = record_dir(root, run_id)
        if not halted_run.revised_halt(rec):
            return None
        marker = halted_run._read(rec) or {}
        sizing_rel = marker.get("sizing_rel")
        call = fire_command(sizing_rel if isinstance(sizing_rel, str) and sizing_rel else None)
        return (
            f"Run {run_id} was revised after it halted; a resume replays its stages "
            f"without the revision.\nFire fresh: {call}."
        )
    except Exception:
        return None


@register_op("hooks.block_workflow_foreign_emission")
def _handler(params: dict, repo_root=None) -> dict:
    params = payload_of(params)
    if params.get("tool_name") != "Workflow":
        return no_advisory()

    tool_input = params.get("tool_input")
    if not isinstance(tool_input, Mapping):
        return no_advisory()

    script_path = tool_input.get("scriptPath")
    inline_script = tool_input.get("script")

    if not script_path:
        if not isinstance(inline_script, str) or not inline_script.strip():
            return no_advisory()
        if _every_agent_call_is_read_only(inline_script):
            return no_advisory()
        return _resolved_deny(_no_receipt_deny_reason("this inline script"))

    script = Path(script_path)
    if not script.is_absolute():
        script = Path(params.get("cwd") or ".") / script
    if not script.is_file():
        return no_advisory()

    receipt_path = script.with_name(script.name + ".emitted.json")
    if not receipt_path.is_file():
        try:
            script_text = script.read_text(encoding="utf-8", errors="replace")
        except Exception:
            return no_advisory()
        if _every_agent_call_is_read_only(script_text):
            return no_advisory()
        return _resolved_deny(_no_receipt_deny_reason(script.name))

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
        return _resolved_deny(
            _sha_mismatch_reason(script, receipt_path, recorded_sha, actual_sha),
        )

    session = _session_id(params)
    if session is not None and recorded_session is not None and session != recorded_session:
        return _resolved_deny(
            _session_mismatch_reason(script, session, recorded_session),
        )

    # Runs before the resume branch: resuming a run whose plan was amended is refused too.
    amended = _plan_amended_reason(script, receipt)
    if amended:
        return _resolved_deny(amended)

    if tool_input.get("resumeFromRunId"):
        reason = _resume_after_revise_reason(script)
        if reason:
            return _resolved_deny(reason)

    return no_advisory()

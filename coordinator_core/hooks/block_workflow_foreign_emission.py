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

DENIES on exactly two conditions, both of which mean the bytes about to run
are not the bytes this session emitted: (1) sha mismatch — the file changed
after it was emitted, by any route; (2) session mismatch — the receipt
names a DIFFERENT session as emitter.

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
from pathlib import Path
from typing import Mapping

from coordinator_core._hook_envelope import deny, no_advisory, payload_of
from coordinator_core._settings_home import settings_home
from coordinator_core.ipc import register_op


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


@register_op("hooks.block_workflow_foreign_emission")
async def _handler(params: dict, repo_root=None) -> dict:
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
    if not script_path:
        return no_advisory()

    script = Path(script_path)
    if not script.is_absolute():
        script = Path(params.get("cwd") or ".") / script
    if not script.is_file():
        return no_advisory()  # the tool's own not-found error owns this

    receipt_path = script.with_name(script.name + ".emitted.json")
    if not receipt_path.is_file():
        return no_advisory()

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

"""coordinator_core.hooks.allow_emitted_workflow_fire — PreToolUse(Workflow)
op.

Ported from DoE-claude `coordinator/hooks/scripts/allow-emitted-workflow-
fire.py` per docs/plans/2026-09-18-doe-holds-no-scripts.md chunk W4-C9.
Auto-approves a `Workflow` fire whose script carries a VERIFYING emission
receipt, and stays silent for everything else — the friction-removal
counterpart of `block_workflow_foreign_emission`, which denies the sha-
mismatch/session-mismatch cases this module withholds its allow on.

CENTRAL INVARIANT (unchanged from source): auto-approval has exactly ONE
path, and it is the receipt verifying — `<script>.emitted.json` must exist,
parse, carry a `sha256`, and that digest must equal the sha256 of the
script's bytes on disk right now. Every other input (inline `script`, a
saved `name`, a missing/malformed/stale receipt, an unreadable file, an
unexpected exception) returns `no_advisory()`, which falls through to the
human prompt that happens today.

FAILS OPEN TOWARD THE PROMPT, NEVER TOWARD APPROVAL — the whole decision is
wrapped in a broad exception guard; a surprise means the operator is still
asked.

NEVER DENIES. This op removes friction on the sanctioned path; it adds no
block — that job belongs to `block_workflow_foreign_emission` alone.

Per-payload-shape decision table (`tool_input` key -> decision), unchanged
from source:
  - `scriptPath`, receipt verifies      -> allow, reason names plan + emitter
  - `scriptPath`, no/bad/stale receipt  -> silent (normal prompt)
  - `script` (inline text)              -> silent (hand-rolled, no receipt possible)
  - `name` (a saved workflow)           -> silent (resolved by the tool's own store)
  - `resumeFromRunId`                   -> whatever `scriptPath` says

Coherence with the deny-side op on this same matcher: a sha mismatch is
where the two meet — this op stays silent and `block_workflow_foreign_
emission` denies with the `--restamp` remediation. The session legs are kept
disjoint the same way — when the receipt names a DIFFERENT session than
this one, that op denies, so this one withholds its allow rather than
emitting an approval its neighbour is simultaneously refusing.

Session id and cwd are read from `params` only — never `os.environ` — per
this package's established payload-only-input convention (see
`nudge_autonomous_askuserquestion`'s module docstring): the resident engine
serves ~50 concurrent sessions and its own process environment belongs to
none of them.

Spec backlink: docs/plans/2026-09-18-doe-holds-no-scripts.md § W4-C9
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Mapping, Optional

from coordinator_core._hook_envelope import allow_advisory, no_advisory, payload_of
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


def _compose_allow_reason(plan: str, emitter: str, restamped: bool) -> str:
    """The one prose site — verbatim port of the source's own composer. The
    script itself is not named here; the tool call being approved already
    carries its path."""
    stamp = "re-stamped" if restamped else "emitted"
    return (
        f"Receipt verifies: {stamp} from {plan} by session {emitter}. "
        "Hand-rolled or edited scripts prompt."
    )


def _decide(params: dict) -> Optional[str]:
    """Return the auto-approval reason, or None to let the prompt happen."""
    if params.get("tool_name") != "Workflow":
        return None

    tool_input = params.get("tool_input")
    if not isinstance(tool_input, Mapping):
        return None
    script_path = tool_input.get("scriptPath")
    if not script_path:
        return None

    script = Path(script_path)
    if not script.is_absolute():
        script = Path(params.get("cwd") or ".") / script
    if not script.is_file():
        return None

    receipt_path = script.with_name(script.name + ".emitted.json")
    if not receipt_path.is_file():
        return None

    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    recorded_sha = receipt.get("sha256")
    if not recorded_sha:
        return None

    if hashlib.sha256(script.read_bytes()).hexdigest() != recorded_sha:
        return None

    recorded_session = receipt.get("session_id")
    session = _session_id(params)
    if session and recorded_session and session != recorded_session:
        return None

    return _compose_allow_reason(
        str(receipt.get("plan") or "unrecorded"),
        str(recorded_session)[:8] if recorded_session else "unrecorded",
        bool(receipt.get("restamped_from_sha256")),
    )


@register_op("hooks.allow_emitted_workflow_fire")
def _handler(params: dict, repo_root=None) -> dict:
    """PreToolUse(Workflow) op: auto-approve a fire whose script carries a
    verifying emission receipt."""
    try:
        # Normalize the two params
        # shapes both engine doors and the cold chain send (see
        # block_worktree_tool).
        reason = _decide(payload_of(params))
    except Exception:
        return no_advisory()
    if reason is None:
        return no_advisory()
    return allow_advisory("PreToolUse", reason)

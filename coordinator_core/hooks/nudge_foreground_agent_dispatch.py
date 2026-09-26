"""
coordinator_core.hooks.nudge_foreground_agent_dispatch — REROUTE gate: background-by-default
enforcement for Agent tool dispatches.

Purpose: Rewrites foreground Agent dispatches into backgrounded ones via
hookSpecificOutput.updatedInput (PreToolUse gate). Background dispatch is the coordinator
default; foreground blocks the EM until the subagent returns, preventing parallel wave
processing and PM-message handling.

RE-LANDED (2026-07-31) — the 2026-07-30 revert's root cause was wrong:
    Between 2026-07-29 and 2026-07-30 this gate answered a foreground dispatch by rewriting
    the tool input in place (hookSpecificOutput.updatedInput with run_in_background: True)
    instead of denying it. It was reverted 2026-07-30 on the conclusion that updatedInput
    does not bind run_in_background for the Agent tool — measured on harness 2.1.220, a
    dispatch sent with run_in_background: false took the reroute branch, the notice fired,
    the tool result reported a successful async launch, and the agent ran in the FOREGROUND
    regardless (the EM's next tool call landed 9.5s AFTER the subagent's own completion
    stamp, i.e. blocked for its whole run).

    That conclusion was wrong. The actual root cause: this op's rewrite was a THIRD
    updatedInput emitter on DoE's Agent PreToolUse matcher, racing enforce-agent-dispatch-
    mode.py's own updatedInput (mode elevation / sidecar / role framing, which fires on
    essentially every Agent dispatch) via the DoE-side relay shim nudge-foreground-agent-
    dispatch.py. Claude Code runs same-event PreToolUse hooks in parallel with undefined
    completion order, and updatedInput is last-writer-wins — so whichever hook finished
    second silently clobbered the other's rewrite. That race is now closed on the DoE side
    (2026-07-31): the relay shim is deregistered from hooks.json's Agent matcher entirely,
    and the foreground-reroute DECISION is a byte-faithful pure-Python port living in DoE's
    coordinator/hooks/scripts/_foreground_dispatch_strip.py, called directly by
    enforce-agent-dispatch-mode.py (the matcher's sole live updatedInput emitter) and folded
    into its single merged emission. Re-probed live on harness 2.1.220 with the rewrite
    computed as that single-emitter fold-in (no competing emitter): the EM regained control
    at 12:16:03.346 while the subagent ran until 12:16:35.633 — genuinely backgrounded, 32.3s
    of reclaimed control.

    This module — the reroute LOGIC — is therefore the reference implementation of the
    algorithm the DoE-side pure module ports, not itself wired into any live hooks.json
    matcher any more (the DoE relay shim that used to call it, nudge-foreground-agent-
    dispatch.py, stays on disk deregistered, still exercising this op directly via its own
    test suite). Its own semantics are unchanged by the parallel-emitter finding — they were
    never the defect; the emission SITE was. Preserve them exactly here, and keep this
    module's own test suite (coordinator_core/tests/test_hooks_roundtrip.py) exercising them
    directly, as the algorithm's source of truth.

    Making the correct path cheaper than the wrong one, rather than walling off the wrong
    one, is the § North star ergonomics-over-enforcement rule this reroute exists to serve.
    deny() survives as the fallback for the one case where no correct rewrite exists: the
    caller could not supply the tool_input to rewrite (see D8). Absent tool_input must never
    silently pass — that would let the very foreground dispatch this gate exists to prevent
    through unremarked.

Port of: nudge-foreground-agent-dispatch.sh (DoE d39ab164, 2026-07-16)

Three-state run_in_background logic:
    - present-and-true  → silent pass (already backgrounded — correct shape).
    - present-and-false → reroute unconditionally (foreground deliberately chosen on a
                          build that provably supports the param); deny only as the
                          no-safe-rewrite fallback (D8).
    - absent            → reroute only if calibrated (session previously seen with param
                          present), deny-fallback rule as above; otherwise PASS.

Calibration (D7 — session-scoped; D7b made it durable 2026-07-29):
    Key PRESENCE (either value — "true" or "false") proves the build exposes
    run_in_background. A later absent-key dispatch on the same session then reads that
    calibration to tell two very different situations apart: a build with no such param
    (every dispatch omits it → PASS, brick-proof, example-retrieval-repo-ue-addon memo 2026-06-21)
    versus an EM that dropped it on a build that has it (→ act on it as deliberate
    foreground). Calibration is keyed by session_id so one session cannot calibrate another.

    D7b — the marker is durable, and this is a FIX, not a refinement. D7 recorded
    calibration only in the module-level _BG_CAPABLE_SESSIONS set, on the stated premise
    that "the engine is a resident long-lived process so the set persists across calls."
    DR-215 retired the resident daemon: every PreToolUse fire is now a fresh interpreter,
    so the set re-initialized empty on every call and NO absent-key dispatch could ever
    find itself calibrated. The whole absent-key leg was dead code that always fell to
    PASS — the hole DoE's state/bug-backlog/2026-07-22-nudge-foreground-calibration-not-
    cross-process.yaml records. Calibration now also writes
        .git/coordinator-sessions/<session_id>/.harness-bg-capable
    which is what actually survives between processes; the in-memory set is kept as a
    same-interpreter fast path. Do NOT re-collapse this to the set alone — that is the
    exact regression, and it is invisible, because the gate keeps passing.

    Accepted residual risk — OVER-calibration, not just under-calibration (review Finding 2,
    2026-07-29): the marker is written once and never expires or re-validates against the
    harness build. If a session_id persists across a harness downgrade mid-session (resumed
    session, or any scenario where the build changes without a new session_id), a marker
    written while run_in_background was exposed survives into a build that no longer exposes
    it — the next absent-key dispatch on that session then reads _is_bg_capable() == True and
    is acted on (reroute/deny) even though the build genuinely omits the param on every call,
    which is exactly the "gate every Agent call on the machine" outcome the brick-proof PASS
    rule exists to prevent. Accepted as-is because it is narrow: it needs a mid-session
    harness downgrade under a session_id that survives that downgrade, not merely a stale
    file. Stronger fix, not built: stamp the marker with a build/harness identifier and treat
    a mismatch as uncalibrated (write-time cost: one extra string comparison; read-time cost:
    parse the stamp instead of a bare exists() check) — left for whoever hits this in
    practice, since the corner case this narrow did not clear the cost/value bar to build
    speculatively.

ENV-HATCH RE-PLUMB (D6):
    The bash hook checked COORDINATOR_AGENT_FOREGROUND_OK from the process env. The
    resident engine runs at server-spawn time with a fixed env, so that env var is
    unreachable at hook-call time. Re-plumbed to a session-scoped sentinel file:
        .git/coordinator-sessions/<session_id>/.foreground-ok
    An operator enables intentional foreground dispatch for a session by touching that
    file directly. As of 2026-08-13 (docs/plans/2026-08-13-guard-messages-stop-handing-
    agents-the-keys.md), neither rendered message names this path, the touch command, or
    the session id any more — a dispatched subagent must never be handed a pasteable
    unlock recipe. This docstring names the mechanism because it is source prose, not
    rendered text; the check() logic below still reads the same sentinel unchanged.

NOTICE ON EVERY REROUTE (D9, standing rule since the 2026-07-30 revert):
    A prior version of this reroute (2026-07-29) suppressed the escape-hatch advisory after
    the first reroute of a session ("bark-once", marked by a session-scoped
    .foreground-reroute-noticed sentinel). That suppression is exactly why the 2026-07-30
    non-binding race went a whole session undetected: the notice and the tool result were
    the ONLY feedback channels a broken reroute has, and silencing the notice after the
    first call removed the one channel that could have surfaced the mechanism failing.

    This module carries NO notice-once state and never will: the reroute advisory is
    returned on EVERY reroute this handler computes, full stop. Bark-once is legitimate for
    advice, never for the evidence that a mechanism fired.

ABSENT-tool_input FALLBACK (D8):
    Rewriting requires the COMPLETE tool input, because updatedInput REPLACES the tool's
    argument object rather than merging into it (harness contract — see _hook_envelope.
    rewrite_input). A caller that does not forward tool_input, forwards an empty one, or
    forwards a dict missing the load-bearing `prompt` key leaves nothing safe to rewrite:
    emitting an updatedInput without `prompt` would dispatch a subagent with no
    instructions, silently — worse than the deny it would replace. `subagent_type` is NOT
    required (it is genuinely optional in the Agent tool schema). Any of these cases falls
    back to the historical deny envelope, so a stale or partial caller degrades to the old
    bounce-back behaviour rather than to a silent foreground pass or a corrupted rewrite.

# UNDOCUMENTED-DENY: deny over mcp_tool is spike-verified on harness 2.1.193 but NOT a
# documented Claude Code hooks contract. See _envelope.deny() and
# docs/plans/2026-07-04-pcore-04-advisory-hook-ops-claude-klabauter-engine.md § Known-risk.
# (updatedInput, by contrast, IS documented and is verified present in 2.1.220 — and, as of
# the 2026-07-31 re-land, verified BINDING when it is the sole emitter on its matcher.)

Negative-spec:
    This op is MUTATING: it writes a sentinel under .git/coordinator-sessions/, which fails
    question 3 of the DR-208 checklist. It writes exactly ONE marker (.harness-bg-capable)
    and nothing else — no coordinator substrate (handoffs, review-trail, commits) and never
    rag's relational store. Do NOT "restore" that marker to in-memory module state to win
    COMPUTE_ONLY back: that is precisely the regression D7b above records, and it fails
    SILENTLY — the calibration leg goes permanently dead while the gate still reports
    healthy passes.
    The .foreground-ok read stays on the reroute/deny path only. The calibration marker is
    the one deliberate exception: it is written whenever run_in_background arrives PRESENT
    (either value — true or false), on the has_bg branch, because presence of the param is
    exactly what proves the build supports it, regardless of which value it carries (fixed
    2026-07-31, review Finding 1 — a bg_true-only write left present-and-false dispatches
    permanently uncalibrated). Cost is one guarded exists() + at most one touch per session,
    on Agent dispatches only — never on the Bash hot path, and still no git subprocess
    (repo_root is a direct param).
    Do NOT re-add a notice-once marker (.foreground-reroute-noticed or any equivalent) — see
    "NOTICE ON EVERY REROUTE" above. That suppression is a standing negative-spec, not a
    style preference.
    Do NOT re-wire this op's caller back onto a live Agent-matcher hooks.json entry in DoE
    without also verifying (AC-1 style) that it remains the sole updatedInput emitter for
    that matcher — see the module docstring of DoE's enforce-agent-dispatch-mode.py, Concern
    G, for why the emission site (not this op's logic) was the actual defect.

Spec backlink: pln-pcore-04-advisory-hook-ops-mak-b219a8 § C1 / D6 / D7
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from coordinator_core.hooks._envelope import deny, no_advisory, payload_of, rewrite_input
from coordinator_core.hooks._payload import field
from coordinator_core.ipc import register_op

logger = logging.getLogger(__name__)

_SESSION_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{4,}$")

_BG_CAPABLE_SESSIONS: set[str] = set()

_DENY_MSG_TEMPLATE = (
    "FOREGROUND AGENT DISPATCH BLOCKED — retry with `run_in_background: true`. "
    "Doctrine: coordinator/snippets/em-operating-doctrine.md § How to Dispatch."
)


_REROUTE_NOTICE = (
    "FOREGROUND AGENT DISPATCH AUTO-REROUTED TO BACKGROUND. Result arrives as "
    "a task notification, not inline. "
    "Doctrine: coordinator/snippets/em-operating-doctrine.md § How to Dispatch."
)


def _foreground_ok_path(git_root: str, session_id: str) -> Path:
    session_dir = Path(git_root) / "coordinator-sessions" / session_id
    return session_dir / ".foreground-ok"


def _bg_capable_path(git_root: str, session_id: str) -> Path:
    return Path(git_root) / "coordinator-sessions" / session_id / ".harness-bg-capable"


def _mark_bg_capable(git_root: str, session_id: str) -> None:
    if not git_root or not session_id:
        return
    marker = _bg_capable_path(git_root, session_id)
    try:
        if marker.exists():
            return
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.touch()
    except Exception:
        logger.warning("bg-capable marker unwritable (%s)", marker, exc_info=True)


def _is_bg_capable(git_root: str, session_id: str) -> bool:
    if not session_id:
        return False
    if session_id in _BG_CAPABLE_SESSIONS:
        return True
    if not git_root:
        return False
    try:
        return _bg_capable_path(git_root, session_id).exists()
    except Exception:
        logger.warning("bg-capable marker unreadable — treating as uncalibrated", exc_info=True)
        return False


def _resolve_git_root() -> str:
    return ""


@register_op("hooks.nudge_foreground_agent_dispatch")
def _handler(params: dict, repo_root=None) -> dict:
    """PreToolUse REROUTE gate: enforce background-by-default on Agent dispatches.

    Pinned input fields (mcp_tool forwards only declared fields; ""=absent):
        tool_name         — must be "Agent" to fire; otherwise no-op.
        run_in_background — "true" | "false" | "" (absent).
        session_id        — session identifier for calibration scoping.
        tool_input        — the COMPLETE Agent tool-input dict, forwarded verbatim; the
                            rewrite target. Read directly (not via field(), which
                            stringifies). Absent/empty → deny fallback (D8).

    Returns:
        no_advisory()      — silent pass (background already set, or uncalibrated absent,
                             or .foreground-ok escape hatch present).
        rewrite_input(...) — foreground rewritten to background, with the escape-hatch
                             advisory attached on EVERY reroute (no bark-once — see module
                             docstring's NOTICE ON EVERY REROUTE section).
        deny(...)          — fallback only: foreground detected but tool_input was not
                             forwarded, so no correct rewrite could be built (D8).

    Handler ordering (Review: code-reviewer — A-F1; re-verified against control flow,
    review Finding 4, 2026-07-29):
        parse → validate session_id → has_bg/bg_true → calibrate → resolve git_root
        → bg_true early-exit → uncalibrated-absent early-exit → THEN check .foreground-ok
        → reroute (or deny).
        The escape-hatch file check runs only on the reroute/deny path; zero-spawn on the
        common bg_true pass path (repo_root direct param, no git subprocess).
    """
    params = payload_of(params)
    tool_name = field(params, "tool_name")
    if tool_name != "Agent":
        return no_advisory()

    run_in_background = field(params, "run_in_background")
    session_id = field(params, "session_id")

    if session_id and not _SESSION_ID_RE.match(session_id):
        session_id = ""

    has_bg = run_in_background != ""
    bg_true = run_in_background == "true"

    if has_bg and session_id:
        _BG_CAPABLE_SESSIONS.add(session_id)

    try:
        git_root = str(repo_root) if repo_root else ""
    except Exception:
        git_root = ""

    if has_bg and session_id:
        _mark_bg_capable(git_root, session_id)

    if bg_true:
        return no_advisory()

    if not has_bg:
        if not _is_bg_capable(git_root, session_id):
            return no_advisory()

    if session_id and git_root:
        foreground_ok = _foreground_ok_path(git_root, session_id)
        try:
            if foreground_ok.exists():
                return no_advisory()
        except Exception:
            logger.warning(
                "escape-hatch sentinel check failed (%s) — falling through to reroute",
                foreground_ok, exc_info=True,
            )

    from coordinator_core.bash_guards._helpers import operator_override_note

    override_note = operator_override_note(
        "COORDINATOR_AGENT_FOREGROUND_OK", payload=params, git_root=git_root
    )

    tool_input = params.get("tool_input")
    if isinstance(tool_input, dict) and tool_input and tool_input.get("prompt"):
        # updatedInput REPLACES the argument object — carry every original key forward and
        updated = dict(tool_input)
        updated["run_in_background"] = True
        context = _REROUTE_NOTICE
        if override_note:
            context = context + " " + override_note
        return rewrite_input("PreToolUse", updated, context)

    # UNDOCUMENTED-DENY: see module docstring and _envelope.deny() docstring.
    deny_message = _DENY_MSG_TEMPLATE
    if override_note:
        deny_message = deny_message + " " + override_note
    return deny("PreToolUse", deny_message)

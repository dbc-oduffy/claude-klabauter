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
    updatedInput emitter on example-doctrine-repo's Agent PreToolUse matcher, racing enforce-agent-dispatch-
    mode.py's own updatedInput (mode elevation / sidecar / role framing, which fires on
    essentially every Agent dispatch) via the example-doctrine-repo-side relay shim nudge-foreground-agent-
    dispatch.py. Claude Code runs same-event PreToolUse hooks in parallel with undefined
    completion order, and updatedInput is last-writer-wins — so whichever hook finished
    second silently clobbered the other's rewrite. That race is now closed on the example-doctrine-repo side
    (2026-07-31): the relay shim is deregistered from hooks.json's Agent matcher entirely,
    and the foreground-reroute DECISION is a byte-faithful pure-Python port living in example-doctrine-repo's
    coordinator/hooks/scripts/_foreground_dispatch_strip.py, called directly by
    enforce-agent-dispatch-mode.py (the matcher's sole live updatedInput emitter) and folded
    into its single merged emission. Re-probed live on harness 2.1.220 with the rewrite
    computed as that single-emitter fold-in (no competing emitter): the EM regained control
    at 12:16:03.346 while the subagent ran until 12:16:35.633 — genuinely backgrounded, 32.3s
    of reclaimed control.

    This module — the reroute LOGIC — is therefore the reference implementation of the
    algorithm the example-doctrine-repo-side pure module ports, not itself wired into any live hooks.json
    matcher any more (the example-doctrine-repo relay shim that used to call it, nudge-foreground-agent-
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

Port of: nudge-foreground-agent-dispatch.sh (example-doctrine-repo d39ab164, 2026-07-16)

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
    PASS — the hole example-doctrine-repo's state/bug-backlog/2026-07-22-nudge-foreground-calibration-not-
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
    UX change: instead of ``export COORDINATOR_AGENT_FOREGROUND_OK=1`` in the shell,
    touch the file to enable intentional foreground dispatch for that session:
        touch .git/coordinator-sessions/<session_id>/.foreground-ok

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
    Do NOT re-wire this op's caller back onto a live Agent-matcher hooks.json entry in example-doctrine-repo
    without also verifying (AC-1 style) that it remains the sole updatedInput emitter for
    that matcher — see the module docstring of example-doctrine-repo's enforce-agent-dispatch-mode.py, Concern
    G, for why the emission site (not this op's logic) was the actual defect.

Spec backlink: docs/plans/2026-07-04-pcore-04-advisory-hook-ops-claude-klabauter-engine.md § C1 / D6 / D7
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from coordinator_core.hooks._envelope import deny, no_advisory, rewrite_input
from coordinator_core.hooks._payload import field
from coordinator_core.ipc import register_op

logger = logging.getLogger(__name__)

# session_id format guard — mirrors the guard in the bash source
_SESSION_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{4,}$")

# In-memory calibration set: session_ids for which the harness has proven it exposes
# run_in_background (the param was present, with any value, at least once in this process).
# Resident long-lived engine — persists across calls; no file I/O needed.
_BG_CAPABLE_SESSIONS: set[str] = set()

# Review: code-reviewer — A-F7: deny message uses real session_id at deny time
# (falls back to "<session_id>" placeholder only when session_id is empty/malformed).
_DENY_MSG_TEMPLATE = (
    "FOREGROUND AGENT DISPATCH BLOCKED — retry with `run_in_background: true`. "
    "Coordinator default is backgrounded dispatch: foreground blocks the EM until "
    "the subagent returns, wasting cycles that could process other waves, reconcile "
    "plans, or handle PM messages in parallel. "
    "Escape hatch for rare legitimate foreground (inline result needed for the very "
    "next statement): touch "
    ".git/coordinator-sessions/{session_id}/.foreground-ok "
    "(resident engine; the old env-var hatch is no longer reachable — "
    "see module docstring D6 and docs/reference/guard-override-keys.md). "
    "Doctrine: coordinator/snippets/em-operating-doctrine.md § How to Dispatch."
)


_REROUTE_NOTICE = (
    "FOREGROUND AGENT DISPATCH AUTO-REROUTED TO BACKGROUND — rewritten with "
    "`run_in_background: true`; result is a task notification, not inline. "
    "Locks the EM till backgrounded. "
    "Escape hatch: touch .git/coordinator-sessions/{session_id}/.foreground-ok — "
    "persists for session, firing till then. "
    "Doctrine: coordinator/snippets/em-operating-doctrine.md § How to Dispatch."
)


def _foreground_ok_path(git_root: str, session_id: str) -> Path:
    """Return the .foreground-ok escape-hatch sentinel path for a resolved session."""
    # Review: code-reviewer — W3 substituted ctx.repo_root (worktree path) with repo_root
    # (git_common_dir path), making the extra ".git" join double-nest the dir:
    # <repo>/.git/.git/coordinator-sessions/<sid> — a path that never exists.
    # Fix: git_root IS already the .git common dir, so drop the redundant ".git" join.
    session_dir = Path(git_root) / "coordinator-sessions" / session_id
    return session_dir / ".foreground-ok"


def _bg_capable_path(git_root: str, session_id: str) -> Path:
    """Return the durable harness-bg-capable calibration marker for a resolved session."""
    return Path(git_root) / "coordinator-sessions" / session_id / ".harness-bg-capable"


def _mark_bg_capable(git_root: str, session_id: str) -> None:
    """Record that this session's harness demonstrably exposes run_in_background (D7b).

    Called when the param arrives PRESENT (either value) — presence is the proof. The
    marker is what a later absent-key dispatch reads to tell "this build has no such
    param" (pass) apart from "this EM dropped it" (act).

    Best-effort and silent on failure: a missing marker degrades to the brick-proof PASS
    that predates calibration entirely, which is the safe direction for the absent case.
    """
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
    """Return True iff this session was previously seen sending run_in_background (D7b).

    Reads the durable marker, then falls back to the in-process set — the set still wins
    within a single interpreter (tests, and any future in-process batching), while the
    marker is what actually carries calibration between the fresh processes production
    uses. An unresolvable session or unreadable marker answers False: uncalibrated, PASS.
    """
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
    """Dead stub — kept for test-mock compatibility (tests patch this attribute).

    Production handler uses the repo_root param directly; this function is never called
    from the handler. The subprocess spawn it previously contained is removed (A-F1).
    Returns "" unconditionally.
    """
    return ""  # Review: code-reviewer — A-F1: subprocess removed; stub kept for test compat


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
    # Only fires on Agent tool dispatches
    tool_name = field(params, "tool_name")
    if tool_name != "Agent":
        return no_advisory()

    run_in_background = field(params, "run_in_background")  # "" = absent
    session_id = field(params, "session_id")

    # Validate session_id format; treat malformed as absent (mirrors bash guard)
    if session_id and not _SESSION_ID_RE.match(session_id):
        session_id = ""

    # Determine key presence: run_in_background == "" means key was absent
    has_bg = run_in_background != ""  # True = key was present (either value)
    bg_true = run_in_background == "true"

    # Calibrate in-memory: key present (either value) proves this build exposes the param.
    # Record session_id so a future absent-key dispatch on the same session is denied.
    # Review: code-reviewer — A-F1: calibration before escape-hatch check and bg_true
    # early-exit; the escape hatch is only relevant when about to deny.
    if has_bg and session_id:
        _BG_CAPABLE_SESSIONS.add(session_id)

    # git_root is needed by BOTH calibration legs now, not just the reroute path, so it is
    # resolved here rather than after the early-exits. Still no subprocess: repo_root is a
    # direct param (A-F1).
    try:
        git_root = str(repo_root) if repo_root else ""
    except Exception:
        git_root = ""

    # Persist calibration durably on ANY presence (either value), mirroring the in-memory
    # set write above: the in-memory set is dead in the spawn-per-call model and cannot
    # carry it to the next dispatch (D7b), and only presence proves the build exposes the
    # param — restricting this to bg_true left present-and-false dispatches uncalibrated,
    # silently defeating a later same-session absent-key call (review Finding 1, 2026-07-31).
    # Review: code-reviewer — Finding 1: move off the bg_true-only leg onto has_bg.
    if has_bg and session_id:
        _mark_bg_capable(git_root, session_id)

    # present-and-true → silent pass.
    if bg_true:
        return no_advisory()

    # absent → discriminate by calibration state.
    # Calibrated (this session was previously seen sending the param) → the omission is a
    # deliberate foreground choice on a build that supports the param → act on it.
    # Uncalibrated (never seen, or no resolvable session_id) → brick-proof PASS: on a build
    # that does not expose run_in_background at all, every dispatch omits it, and acting on
    # that would gate every Agent call on this machine.
    if not has_bg:
        if not _is_bg_capable(git_root, session_id):
            return no_advisory()
        # Fall through: calibrated absent = deliberate foreground

    # D6 escape hatch — an explicit opt-in to foreground for this session; pass untouched.
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

    effective_sid = session_id if session_id else "<session_id>"

    # present-and-false, or calibrated absent → rewrite the call into a backgrounded one.
    # tool_input is read raw: field() stringifies, and this value is a dict.
    # Review: code-reviewer — Finding 1: non-emptiness alone doesn't prove tool_input is a
    # complete, safe rewrite target. `prompt` is the load-bearing key the harness Agent tool
    # schema requires; a tool_input missing it would rewrite into a promptless dispatch —
    # silently, which is worse than the deny it replaces. subagent_type is NOT required here:
    # it is genuinely optional in the Agent tool schema, so requiring it would deny valid
    # dispatches.
    tool_input = params.get("tool_input")
    if isinstance(tool_input, dict) and tool_input and tool_input.get("prompt"):
        # updatedInput REPLACES the argument object — carry every original key forward and
        # override only run_in_background (D8).
        updated = dict(tool_input)
        updated["run_in_background"] = True
        context = _REROUTE_NOTICE.format(session_id=effective_sid)
        return rewrite_input("PreToolUse", updated, context)

    # No forwardable tool_input → no correct rewrite exists; fall back to the historical
    # bounce-back rather than letting a foreground dispatch through unremarked (D8).
    # UNDOCUMENTED-DENY: see module docstring and _envelope.deny() docstring.
    # Review: code-reviewer — A-F7: fill real session_id into escape-hatch path.
    return deny("PreToolUse", _DENY_MSG_TEMPLATE.format(session_id=effective_sid))

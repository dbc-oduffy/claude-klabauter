"""
coordinator_core.hooks.nudge_named_agent_report_delivery — PreToolUse Agent advisory:
a NAMED dispatch is a teammate, and a teammate's final text never reaches the EM.

Purpose: passing ``name`` to the Agent tool does not merely label the dispatch — it
converts it into an Agent-teams *teammate* (canonical id ``<name>@session-<short>``,
the shape ``track_dispatched_agents._TEAMMATE_AGENT_RE`` already recognises). A
teammate's plain-text output is NOT delivered to the parent session; per the harness
SendMessage contract, the only channel that reaches the dispatcher is an explicit
``SendMessage`` to ``"main"``. An unnamed background subagent's final report, by
contrast, IS delivered as the Agent tool result.

So a brief that ends "Report back: <structure>" — the natural way to brief any
subagent, and correct for an unnamed one — produces NOTHING from a named one. The
agent completes, does the work, writes its report as final text, and the EM receives
only an ``idle_notification``. The work is done and unrecoverable without a manual
``SendMessage`` round-trip; if the EM reads idle-as-failed it redispatches and races
two writers onto the same files.

Empirical basis (2026-07-30, this repo): three dispatches in one session. The one
WITHOUT ``name`` (subagent_type ``Explore``) delivered its full report inline. The two
WITH ``name`` (both ``general-purpose``) produced four idle notifications and zero
reports between them. (An earlier revision of this paragraph cited
``.git/coordinator-sessions/logs/agent-audit.jsonl`` as evidence that both had
completed; it is not — that log records dispatch-out, not arrival, and every
dispatch appears in it by construction. See
``agent_completion_log`` for why. The claim that stands on disk is the zero
delivered reports, which is what this hook exists for.) The EM stood both down and redid
the work inline, having first spent two round-trips asking them to deliver. Nothing in
the dispatch surface warned that naming changed the delivery contract — the sibling
PreToolUse gate (``nudge_foreground_agent_dispatch``) rewrites the same calls to
background and its notice says the result "will arrive as a task notification rather
than inline", which is true for an unnamed background agent and materially misleading
for a named one.

Why an advisory rather than doctrine prose: per this repo's north star (§ the discharge
test), a rule that survives only in prose is a surface that permitted the mistake and
then asked someone to remember not to make it. The artifact that discharges this one has
to fire at dispatch time, where the choice is being made.

Design-as-offers: the message leads with the two working shapes (drop the name, or brief
the SendMessage) rather than with the violation. Naming is often correct — mid-flight
addressability is exactly what it is for — so this hook never blocks and never argues
against naming; it only ensures the report has somewhere to go.

Suppression: a brief that already instructs a SendMessage-to-main gets no advisory. The
EM that has internalised this does not need reminding, and a nag that fires on the
correct shape trains the reader to ignore it.

Contract:
  - PreToolUse, ``tool_name == "Agent"`` only; every other tool is a silent no-op.
  - ``name`` absent → ``no_advisory()``. This is the common case and costs one dict
    lookup: no I/O, no spawn, no session state, nothing to fail.
  - Never blocks, never denies, never rewrites input. Advisory only — the dispatch
    proceeds exactly as written whatever this hook returns.
  - Never raises. Malformed/absent ``tool_input`` is treated as "no name", i.e. silence.

Negative-spec:
  - Do NOT fold this into ``nudge_foreground_agent_dispatch``. That hook's contract is
    DENY (foreground bounces back for the EM to reissue) and it documents a zero-spawn
    silent pass on the common ``run_in_background: true`` path; adding an advisory to
    that path would change its contract and its ordering guarantees. The triggers are
    also independent: this one fires on ``name`` regardless of background state,
    including on dispatches that hook passes silently.
  - Do NOT escalate to ``deny``. A named dispatch is legitimate; only its briefing is
    incomplete, and denying it would block a correct shape to fix a message problem.
  - Do NOT write session state to suppress repeats. This advisory is per-dispatch
    actionable — each named dispatch needs its own brief fixed — so once-per-session
    suppression would silence the 2nd..Nth dispatch, which is exactly where the loss
    recurred. (The sibling hook's own once-per-session notice was retired on 2026-07-30
    for the same reason: it hid a guard that had stopped working.)
  - Do NOT switch this to an ``updatedInput`` rewrite of the brief. Measured on harness
    2.1.220 (2026-07-30): updatedInput does not bind for the Agent tool — see
    ``nudge_foreground_agent_dispatch`` § REROUTE REVERTED TO DENY.

Spec backlink: pln-pcore-04-advisory-hook-ops-mak-b219a8
"""

from __future__ import annotations

import re

from coordinator_core.ipc import register_op
from coordinator_core.hooks._envelope import allow_advisory, no_advisory
from coordinator_core.hooks._payload import field

# A brief that already routes its report through SendMessage-to-main needs no advisory.
# Deliberately loose: it must match the shapes an EM actually writes ("SendMessage to
# main", 'SendMessage({to: "main"})', "send your report to main via SendMessage"), and a
# false NEGATIVE here costs only a redundant advisory on an already-correct brief, while a
# false positive silences the case this hook exists for. Ordering-independent (the two
# tokens may appear either way round) and case-insensitive.
# A SIDECAR-PROVISIONED DISPATCH IS NEVER SUPPRESSED, however well its brief
# is worded. The suppression below assumes a brief naming SendMessage-to-main is
# correctly briefed and needs no warning. For a sidecar dispatch that premise is
# wrong in the direction that costs work: doe-claude-em session `36630d4c`
# (2026-08-10) briefed all four named reviewers with both tokens present, and
# three of the four returned findings by SendMessage while leaving the
# provisioned sidecar at its 703-byte scaffold. `review-integrator` then refused
# on its empty-scaffold intake guard, correctly, and every finding needed a
# round trip to recover -- in exactly the fan-out shape partitioned review
# mandates.
#
# The gap is audience, not wording: the advisory addresses the EM WRITING the
# brief, while the agent READING it substitutes the message for the write --
# reasonably, since every sidecar-writing agent definition says to write
# findings and then return a pointer, and for a teammate the return IS
# SendMessage. A correctly-worded brief is not evidence the file will be
# written, so this is the one case the suppression must not cover.
_SIDECAR_RE = re.compile(r"sidecar|subagent-share", re.I)
_SENDMESSAGE_RE = re.compile(r"sendmessage", re.I)
_MAIN_TARGET_RE = re.compile(r"\bmain\b", re.I)

_ADVISORY = """\
NAMED DISPATCH — this agent's final text will NOT reach you (advisory; proceeding anyway).

`name` makes this a TEAMMATE (agentId `{name}@session-<short>`) — only `SendMessage` to
`"main"` reaches you. "Report back: ..." yields an `idle_notification`, report undelivered.

  (a) No mid-flight contact -> DROP `name`.
  (b) Mid-flight contact -> KEEP `name`, brief: SendMessage "main" with a POINTER ONLY
      (sidecar path + one-line verdict, not the report restated).

A sidecar left at its scaffold is not a delivered report: `review-integrator`
refuses an empty-scaffold intake, so the findings cannot be applied or
recorded. The pointer is the message; the file is the report.

Either is fine.\
"""


@register_op("hooks.nudge_named_agent_report_delivery")
def _handler(params: dict, repo_root=None) -> dict:
    """PreToolUse advisory: warn that a NAMED Agent dispatch cannot deliver its report.

    Pinned input fields (mcp_tool forwards only declared fields; ""=absent):
        tool_name  — must be "Agent" to fire; otherwise no-op.
        tool_input — the COMPLETE Agent tool-input dict, forwarded verbatim. Read raw
                     (not via field(), which stringifies) for `name` and `prompt`.

    Returns:
        no_advisory()      — not an Agent call, no `name`, or the brief already routes
                             its report through SendMessage-to-main AND provisions
                             no sidecar (`_SIDECAR_RE`: a sidecar dispatch is never
                             suppressed, however well its brief is worded).
        allow_advisory(...) — named dispatch whose brief has no delivery channel.

    Never blocks and never rewrites: the dispatch proceeds as written in every branch.
    """
    if field(params, "tool_name") != "Agent":
        return no_advisory()

    tool_input = params.get("tool_input")
    if not isinstance(tool_input, dict):
        return no_advisory()

    name = tool_input.get("name")
    if not isinstance(name, str) or not name.strip():
        return no_advisory()

    # Already-correct briefs stay silent — see _SENDMESSAGE_RE's note on why this
    # suppression is deliberately generous rather than precise.
    prompt = tool_input.get("prompt")
    if (
        isinstance(prompt, str)
        and _SENDMESSAGE_RE.search(prompt)
        and _MAIN_TARGET_RE.search(prompt)
        and not _SIDECAR_RE.search(prompt)
    ):
        return no_advisory()

    return allow_advisory("PreToolUse", _ADVISORY.format(name=name.strip()))

"""coordinator_core.hooks.group_em_autofire — UserPromptExpansion auto-fire
for Group EM entry.

Port of: DoE-claude `coordinator/hooks/scripts/group-em-autofire.py`
(docs/plans/2026-09-18-doe-holds-no-scripts.md chunk W4-C12). Shape per the
W4-C1 verdict: command/native-door — no coordinator/bin shim, no http
registration.

Shape change from the DoE source: DoE's script shells out to
`coordinator/bin/group-em-enter.py --json` as a subprocess. That op is
ALREADY a warm, in-process JSON-RPC op in this checkout
(`coordinator_core.ops.group_em_enter`, `register_op("groupem.enter", ...)`
— ported ahead of this chunk). This port therefore calls it in-process via
`coordinator_core.ipc.get_op_handler("groupem.enter")` instead of spawning
`sys.executable` — a strict improvement (one fewer process spawn per fire,
no `--repo`/`--session-id`/`--json` argv marshalling), not a re-architecture:
the CLI's own `--json` output IS this op's return dict, so
`render_additional_context` below consumes the same shape unchanged.
`resolve_enter_cli`/`_run_enter` (the subprocess machinery) have no
replacement — there is nothing left to resolve or spawn.

When a session types `/group-em`, this hook fires ahead of `UserPromptSubmit`,
runs the entry op, and injects the assembled result as `additionalContext` —
so the Group EM is already claimed and the roster and digest already built
by the time the session's own turn begins.

THE ENTRY OP MUTATES, AND THAT IS THE POINT (unchanged from DoE source). It
fires on nothing else: no `Stop` trigger, no timer, no other command.

SESSION ID IS PROPAGATED, NEVER AMBIENT. The payload's `session_id` is
passed explicitly as `caller_session_id`, never left to the op's own
environment-var default — a Group EM claimed under the wrong id is worse
than no claim (unchanged rationale from DoE source).

A REFUSAL IS REPORTED, NEVER SWALLOWED. A refused nomination (`claimed`
false), or the op's own `nomination_error` leg, each produce their own
`additionalContext` naming what happened.

NEGATIVE SPEC (unchanged from DoE source):
  - Never passes a supersede/incumbent-override signal — taking the role
    from a live peer is direction-class and belongs to a human.
  - Never sends to a peer. It assembles; `gate1`/`gate2` stay unresolved.
  - Never blocks. Any failure degrades to silence.

`render_watch_line`/`_watch_module` (DoE's separate watch-liveness render)
has NO replacement here: `coordinator_core.ops.group_em_enter`'s own
`watch_liveness` leg already answers the same question in-band, inside the
one composed payload this hook now reads — a second, independent watch
read would duplicate that leg rather than adding information. The watch
verdict is folded into `render_additional_context` below instead of being
prepended as a separate line.

Spec backlink: docs/plans/2026-09-18-doe-holds-no-scripts.md, chunk W4-C12.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from coordinator_core._hook_envelope import payload_of
from coordinator_core.hooks._envelope import context_only, no_advisory
from coordinator_core.hooks.support.skill_invocation import normalize_command_name
from coordinator_core.ipc import get_op_handler
from coordinator_core.ipc import register_op

_GROUP_EM_COMMAND_NAMES = {"group-em"}
_CONTEXT_BUDGET_CHARS = 10_000
_PM_CALL_DEFAULT = "resolving this is the PM's call"


def render_additional_context(payload: dict) -> str:
    """Render the injected turn context from `groupem.enter`'s composed
    return dict. A refusal renders as loudly as a success (see module
    docstring's refusal clause)."""
    nomination = (payload or {}).get("nomination")
    nomination_error = (payload or {}).get("nomination_error")

    if nomination_error:
        return (
            "## Group EM entry: NOT ENTERED (engine leg failed)\n\n"
            f"{nomination_error}\n\n"
            "The Group EM was NOT claimed and no roster or digest exists. Do not act as this "
            "repo's Group EM until entry succeeds."
        )

    if not isinstance(nomination, dict) or not nomination.get("claimed"):
        message = (nomination or {}).get("message") or "nomination refused"
        pm_call = (nomination or {}).get("needs_pm_decision") or _PM_CALL_DEFAULT
        return (
            "## Group EM entry: REFUSED\n\n"
            f"{message}\n\n"
            f"{pm_call}\n\n"
            "No roster or digest was built. Entry is last-writer-wins and vacates any incumbent "
            "before re-entering, so a refusal that reaches here is NOT a standing-ownership "
            "problem -- do not reach for an override. Something else refused."
        )

    roster = payload.get("roster") or []
    digest = payload.get("digest") or {}
    entries = digest.get("entries") or []
    suppressed = digest.get("suppressed") or []
    candidates = sum(1 for peer in roster if peer.get("candidate"))

    considered = payload.get("roster_considered")
    if isinstance(considered, int) and not isinstance(considered, bool):
        roster_line = f"Roster: {len(roster)}/{considered} considered, {candidates} candidate(s)"
    else:
        roster_line = f"Roster: {len(roster)} short, {candidates} cand (n/a total)"

    lines = [
        f"Group EM ACTIVE: {nomination.get('message') or 'claimed'}",
        roster_line,
    ]
    if nomination.get("displaced_holder"):
        lines.append(
            f"DISPLACED: {nomination['displaced_holder']} — "
            + (
                "still running and does not know yet. Tell it, this turn: it holds no standing and "
                "must not act as Group EM. This send is owed, not offered."
                if nomination.get("displaced_holder_live")
                else "not running; nobody to tell."
            )
        )
    for peer in roster[:10]:
        mark = "*" if peer.get("candidate") else " "
        lines.append(
            f"  {mark} {peer.get('session_id')}  {peer.get('state')} ({peer.get('reason')})"
        )
    if len(roster) > 10:
        lines.append(f"  ... and {len(roster) - 10} more")

    lines.append(f"Digest: {len(entries)}/{len(suppressed)} offerable/suppressed")
    for entry in entries[:10]:
        lines.append(f"  - {entry.get('session_id')}  {entry.get('trigger')}")

    unrecorded = digest.get("unrecorded") or []
    if unrecorded:
        lines.append(
            f"  ! cooldown UNARMED for {len(unrecorded)} peer(s) -- their throttle did not write"
        )

    intake = payload.get("intake") or {}
    if intake.get("rejected"):
        lines.append(
            f"  ! obligations-inbound: {intake['rejected']} malformed row(s) quarantined to "
            ".coordinator-local/subagent-share/<sid>/obligations-inbound.rejected.jsonl -- producer bug"
        )
    if intake.get("deferred"):
        lines.append(
            f"  ! obligations-inbound: {intake['deferred']} session(s) deferred; their rows fold "
            "on the next tick"
        )

    baseline = payload.get("baseline") or {}
    if baseline and not baseline.get("first_tick"):
        lines.append(
            f"Baseline: +{len(baseline.get('spawned') or [])} spawned, "
            f"-{len(baseline.get('exited') or [])} exited, "
            f"~{len(baseline.get('changed') or [])} changed"
        )

    watch_liveness = payload.get("watch_liveness") or {}
    if watch_liveness:
        lines.append(f"Watch liveness: {watch_liveness}")

    gate_line = "gate1/gate2 UNRESOLVED: declare per send; never loop-send entries."
    arm_line = "ARM NOW: CronCreate ~23min recur (off :00/:30) + Monitor poller."
    lines += [arm_line, gate_line]

    text = "\n".join(lines)
    if len(text) > _CONTEXT_BUDGET_CHARS:
        tail = f"{arm_line}\n\n{gate_line}"
        keep = max(0, _CONTEXT_BUDGET_CHARS - len(tail) - 24)
        text = text[:keep] + "\n... (truncated)\n\n" + tail
    return text


def _normalize_command_name(name: object) -> str:
    """`normalize_command_name` plus DoE source's leading-`/` strip — the
    shared `skill_invocation.normalize_command_name` (ported W4-C4) does not
    strip a literally-typed leading slash, and this hook's own DoE source
    did; preserved here rather than widening the shared helper out of this
    chunk's footprint."""
    if not isinstance(name, str):
        return ""
    return normalize_command_name(name.lstrip("/"))


def compute_context(payload: dict) -> Optional[str]:
    """Compute the `additionalContext` prose, or None on a non-matching
    command / no session id / unresolvable op handler."""
    if _normalize_command_name(payload.get("command_name")) not in _GROUP_EM_COMMAND_NAMES:
        return None  # not a group-em invocation -- silent pass

    session_id = payload.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        return None  # no id to claim under

    cwd = payload.get("cwd")
    repo_root = cwd if isinstance(cwd, str) and cwd else os.getcwd()

    handler = get_op_handler("groupem.enter")
    if handler is None:
        return None  # transport failure -- op unresolvable, fail open

    try:
        entered = handler(
            {"repo_root": repo_root, "caller_session_id": session_id},
            repo_root=Path(repo_root),
        )
    except Exception:
        return None  # fail open -- engine leg raised

    if not isinstance(entered, dict) or not entered:
        return None

    return render_additional_context(entered)


@register_op("hooks.group_em_autofire")
def _handler(params: dict, repo_root=None) -> dict:
    """IPC/dispatch_message adapter over `compute_context()`. `params` IS
    the raw UserPromptExpansion payload dict.

    Returns `context_only("UserPromptExpansion", ...)` when a group-em verb
    was matched and entry produced a renderable result; `no_advisory()`
    otherwise (silent pass — matches the DoE source's own fail-open
    contract).
    """
    params = payload_of(params)
    try:
        additional_context = compute_context(params)
    except Exception:
        additional_context = None

    if not additional_context:
        return no_advisory()
    return context_only("UserPromptExpansion", additional_context)

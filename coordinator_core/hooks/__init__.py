"""
coordinator_core.hooks — advisory and bookkeeping hook op handlers package.

Purpose: Namespace for coordinator_core hook op implementations. Each sub-module
self-registers its handler via register_op() at import time under the "hooks.<name>"
method namespace. Importing this bare package NEVER populates the op-registry —
registration is lazy unconditionally, with no flag or channel to arm it: a caller
must trigger a targeted per-op import itself (see coordinator_core.ipc's
hooks-scoped registry-miss fallback, C2), or call _eager_import_all() directly for
the rare full-registration need.

Every hook module that calls register_op() MUST be listed in _EAGER_HOOK_MODULES
below. A module left out of that list still registers its op whenever some
unrelated importer happens to pull it in directly, but _eager_import_all() (the
only remaining full-load routine) will not force it, so any caller relying on that
routine for completeness silently misses it. context_pressure_precompact was
exactly this shape until 2026-07-22, when it was still an import-order hazard
rather than a "not in the table" hazard — the failure mode this list closes off.

Lazy hook registration (docs/plans/2026-08-06-windows-hot-path-less-work-per-
interpreter.md § C1, made unconditional 2026-08-22 — the import-path-costs-nothing
sprint, mirroring coordinator_core.ops's own C6 retirement): a hook entrypoint
needing exactly one module (e.g. track-touched-files.py importing only
coordinator_core.hooks.track_touched_files) used to pay for all 20 registrations
and their full transitive import graph (measured ~0.609s min unarmed vs 0.079s
armed on `example-cockpit-repo-93`'s box) just to reach the one it wanted, because
importing this package always ran its __init__.py to completion first. A two-channel
flag (`COORDINATOR_CORE_LAZY_OPS` env var / `sys._coordinator_core_lazy_ops`
in-process attribute) — REUSED VERBATIM from coordinator_core.ops's own
now-retired channel, never a hooks-specific one — used to gate this package's
eager-import block; both channels, and the writers that armed the in-process one,
are retired. Every consumer of a bare `import coordinator_core.hooks` (including
DoE-claude's seven hook scripts that still call the sibling repo's own
`_arm_lazy_ops()`, defined in that repo's `coordinator/hooks/scripts/_engine_root.py`
(theirs, not ours — do not go looking for it here); that call becomes a harmless
no-op the moment nothing here reads the channel it arms) now goes through the
targeted-import or SAFE FALLBACK paths below instead of relying on package-init to
populate the registry as a side effect.

_eager_import_all() is exposed for two callers: (1) coordinator_core.ipc's
registry-miss SAFE FALLBACK — if a hooks.* op is absent from OP_MODULE_MAP (or a
mapped import didn't register it — map drift), the fallback calls this function
directly to force full registration regardless of whatever partial state this
package is already in; and (2) `ops/session/guard_roster_ops.py`'s
`list_ported_advisory_ops()`, which needs an exhaustive roster and cannot rely on
whatever has been imported so far in-process. Idempotent — re-importing an
already-imported submodule is a cheap no-op, so this is what makes an
incomplete/stale map (or a caller invoked before anything else has imported
hooks) degrade to today's correctness rather than to a broken dispatch or a
truncated roster. Pinned for C2 (coordinator_core.ipc's registry-miss fallback):
call `coordinator_core.hooks._eager_import_all()` — zero arguments, returns None.

Advisory hook ops ported from ~/.claude advisory/nudge command hooks (pcore-04, D4):
    nudge_foreground_agent_dispatch  — deny gate (foreground dispatches bounce back for
                                       the EM to reissue backgrounded); bg-capable
                                       calibration (D7). The 2026-07-29 updatedInput
                                       reroute was reverted 2026-07-30 — it did not bind
                                       run_in_background on harness 2.1.220.
                                       MUTATING despite sitting in the advisory group: its
                                       calibration marker is a .git/coordinator-sessions/
                                       sentinel write — see authz/classification.py
    suggest_sonnet_research          — advisory; subagent suppression via agent_id
    nudge_em_code_dispatch           — advisory; subagent + session suppression
    nudge_unauthorized_handoff       — advisory; PostToolUse translation; env-hatch re-plumb
    postuse_advisory_dispatch        — advisory; context-pressure + runtime-tripwire
    nudge_named_agent_report_delivery — advisory; PreToolUse Agent; warns that a NAMED
                                       dispatch (Agent-teams teammate) does not deliver
                                       its final report to the parent the way an unnamed
                                       background subagent does

Bookkeeping hook ops (pcore-08, D1) — write .git/coordinator-sessions/ session-runtime:
    track_touched_files      — dedup-append to touched.txt (session + agent); MUTATING
    session_heartbeat        — update last_activity in meta.json via liveness.py; MUTATING
    agent_completion_log     — jsonl append to logs/agent-audit.jsonl; MUTATING
    track_dispatched_agents  — tab-delimited dedup + collision rewrite; MUTATING

Bookkeeping hook op (W4b) — writes the PreCompact sentinel + state snapshot to tempdir:
    context_pressure_precompact — compaction sentinel + state file; MUTATING

Zero-tool-use detection ops (cross-repo DoE-claude contract, Stage 1 write / Stage 2 read).
Naming note: the store holds one record per verified tool-use count, not only zero —
`kind: "zero-tool-use"` names the detector, not a content filter; see
subagent_zero_tool_use's module docstring ("Naming note") before adding a consumer.
    subagent_zero_tool_use          — SubagentStop; counts tool_use blocks, durable-writes
                                       ONLY on a verified count (every count, not only
                                       zero — deliberately no zero-gate); MUTATING
    subagent_zero_tool_use_surface  — thin pure-read surface over that per-session store;
                                       returns ALL records unfiltered as structured
                                       JSON-RPC data, not an advisory — caller filters
                                       tool_use_count itself
    subagent_zero_tool_use_resolve  — Stage 3; pull/poll per-agent_id verdict over that
                                       same store, independent of SubagentStop having
                                       fired for THIS caller; DOES filter to
                                       tool_use_count == 0 for its "zero-tool-use"
                                       verdict; returns structured data

Subagent arrival-check op (cross-repo DoE-claude contract, sibling of the zero-tool-use
trio above — different question, same pull/poll posture):
    subagent_arrival_check          — pull/poll: classify one agent_id as
                                       arrived/running/unknown by tailing ONLY that
                                       subagent's own transcript (last record, never a
                                       full-file parse); fails toward "running"/"unknown",
                                       never a false "arrived"; returns structured data

Receiver-state sensor op (cross-repo DoE-claude contract, source memo
2026-08-14-doe-claude-em-receiver-state-sensor-seam.md) — thin op over
session.receiver_state, the detection half of the receiver-state split (DoE owns the
transport/hook-registration half):
    receiver_state_sensor           — Stop/SubagentStop; writes this session's own
                                       PAUSED/PRODUCING/UNKNOWN verdict to a NEW sibling
                                       file (.git/coordinator-sessions/<sid>/
                                       receiver-state.json); MUTATING

Sidecar-fill surfacing op (run-report sidecar contract, surfacing half of the
2026-08-15 break-class gap — detection is
coordinator_core.subagent_sandbox.detect_unfilled_sidecar):
    subagent_sidecar_fill_check     — PostToolUse-Agent; re-scans this session's
                                       own state/subagent-share/<session_id>/ and
                                       advises (post_advisory, never blocking)
                                       only when a sidecar is status: open with
                                       no agent-authored body; compute + advise,
                                       no durable write of its own

Spec backlinks:
    docs/plans/2026-07-04-pcore-04-advisory-hook-ops-claude-klabauter-engine.md
    docs/plans/2026-07-04-pcore-08-async-bookkeeping-hooks-engine-vs-mcp.md
    cross-repo/inbox/2026-07-25-doe-claude-em-zero-tool-use-detection-engine-op-contract.md
    cross-repo/inbox/2026-07-25-doe-claude-em-zero-tool-use-detection-verdict-viable.md
    docs/plans/2026-08-14-receiver-state-sensor.md
"""

from __future__ import annotations

import importlib
import sys as _sys
import traceback as _traceback
from typing import Dict

# ---------------------------------------------------------------------------
# Eager-import table: dotted module path per hook module that used to be a bare
# `from coordinator_core.hooks import X` statement. Kept as data (mirroring
# coordinator_core.ops._EAGER_OP_MODULES) so _eager_import_all() is a single
# loop rather than 20 duplicated import lines, and so it can be re-invoked
# on demand (C2's registry-miss fallback and guard_roster_ops.py's roster
# call) without re-running module-level code. Retained deliberately, not
# apparatus residue — mirroring C6's own note for _EAGER_OP_MODULES: it is
# the table _eager_import_all() iterates, so retaining the function retains
# the list.
# ---------------------------------------------------------------------------
_EAGER_HOOK_MODULES: list[str] = [
    "coordinator_core.hooks.nudge_foreground_agent_dispatch",  # registers "hooks.nudge_foreground_agent_dispatch"
    "coordinator_core.hooks.nudge_named_agent_report_delivery",  # registers "hooks.nudge_named_agent_report_delivery"
    "coordinator_core.hooks.suggest_sonnet_research",  # registers "hooks.suggest_sonnet_research"
    "coordinator_core.hooks.nudge_em_code_dispatch",  # registers "hooks.nudge_em_code_dispatch"
    "coordinator_core.hooks.nudge_unauthorized_handoff",  # registers "hooks.nudge_unauthorized_handoff"
    "coordinator_core.hooks.postuse_advisory_dispatch",  # registers "hooks.postuse_advisory_dispatch"
    "coordinator_core.hooks.track_touched_files",  # registers "hooks.track_touched_files"
    "coordinator_core.hooks.session_heartbeat",  # registers "hooks.session_heartbeat"
    "coordinator_core.hooks.agent_completion_log",  # registers "hooks.agent_completion_log"
    "coordinator_core.hooks.track_dispatched_agents",  # registers "hooks.track_dispatched_agents"
    "coordinator_core.hooks.agent_postuse_dispatch",  # registers "hooks.agent_postuse_dispatch"
    "coordinator_core.hooks.context_pressure_precompact",  # registers "hooks.context_pressure_precompact"
    "coordinator_core.hooks.subagent_zero_tool_use",  # registers "hooks.subagent_zero_tool_use"
    "coordinator_core.hooks.subagent_zero_tool_use_surface",  # registers "hooks.subagent_zero_tool_use_surface"
    "coordinator_core.hooks.subagent_zero_tool_use_resolve",  # registers "hooks.subagent_zero_tool_use_resolve"
    "coordinator_core.hooks.subagent_arrival_check",  # registers "hooks.subagent_arrival_check"
    "coordinator_core.hooks.subagent_fabrication_check",  # registers "hooks.subagent_fabrication_check"
    "coordinator_core.hooks.receiver_state_sensor",  # registers "hooks.receiver_state_sensor"
    "coordinator_core.hooks.subagent_sidecar_fill_check",  # registers "hooks.subagent_sidecar_fill_check"
    "coordinator_core.hooks.subagent_review_mark",  # registers "hooks.subagent_review_mark"
    "coordinator_core.hooks.cater_subagent_start",  # registers "hooks.cater_subagent_start"
    "coordinator_core.hooks.nudge_autonomous_askuserquestion",  # registers "hooks.nudge_autonomous_askuserquestion"
    "coordinator_core.hooks.sessionend_archive_session",  # registers "hooks.sessionend_archive_session"
    "coordinator_core.hooks.watchdog_undischarged_next_move",  # registers "hooks.watchdog_undischarged_next_move"
    "coordinator_core.hooks.plan_persistence_check",  # registers "hooks.plan_persistence_check"
    "coordinator_core.hooks.runtime_tripwire_em_check",  # registers "hooks.runtime_tripwire_em_check"
    "coordinator_core.hooks.stop_dispatch",  # registers "hooks.stop_dispatch", "hooks.guard_kira_verdict_routed", "hooks.stop_em_report_altitude", "hooks.nudge_harness_directive_dispatch", "hooks.nudge_unrouted_sizing"
]


# module dotted-path -> the exception raised the last time we tried to import
# it. Populated by _eager_import_all() on a per-module ImportError/Exception;
# cleared on a subsequent successful import of that same module (self-healing
# if the module is fixed mid-process). Mirrors
# coordinator_core.ops._POISONED_MODULES in name and role — read by
# coordinator_core.ipc's dispatch path to turn a registry MISS on a poisoned
# hooks.* module's op into the real cause instead of a generic "Method not
# found".
_POISONED_MODULES: Dict[str, BaseException] = {}


def get_poisoned_modules() -> Dict[str, BaseException]:
    """Return a shallow copy of {module dotted-path: last import exception}.

    Purpose: read-only seam mirroring coordinator_core.ops.get_poisoned_modules,
    for a future ipc.py disambiguation of a hooks.* registry MISS caused by an
    import failure rather than an unknown op.
    """
    return dict(_POISONED_MODULES)


def _eager_import_all() -> None:
    """Import every hook module, firing each one's register_op(...) side-effect.

    Full-load routine, mirroring coordinator_core.ops._eager_import_all() in
    name, role, AND resilience shape. This is the exact set of imports that
    used to run unconditionally at package-init time before this package's
    lazy-flag retirement (2026-08-22, mirroring coordinator_core.ops's own C6);
    it is now the ONLY way to force complete hooks.* registration — a bare
    `import coordinator_core.hooks` never does — called by C2's registry-miss
    fallback in coordinator_core.ipc and by
    `ops/session/guard_roster_ops.py::list_ported_advisory_ops()`, regardless
    of which submodules (if any) are already imported. Idempotent —
    re-importing an already-imported submodule is a cheap no-op.

    Resilient-and-loud (2026-08-06, mirroring coordinator_core.ops's
    2026-07-21 pattern): each module is imported independently. A single
    module's import failure is:
      1. NOT allowed to prevent any other hook module from registering
         (resilience) — this matters more now than before C2: a hooks.* miss
         on the live dispatch path calls this function synchronously to serve
         ONE op resolution, so a bare loop would let one bad hook module take
         down resolution of every other hooks.* op mid-request.
      2. Printed to stderr immediately, naming the module and the real
         exception (loudness) — never swallowed, never merely debug-logged.
      3. Recorded in _POISONED_MODULES so a later lookup of one of that
         module's ops can raise the real cause.

    Signature pinned for C2 (later wave): zero arguments, returns None.
    """
    for module_path in _EAGER_HOOK_MODULES:
        try:
            importlib.import_module(module_path)
        except Exception as exc:  # noqa: BLE001 — intentional broad catch, see docstring
            _POISONED_MODULES[module_path] = exc
            print(
                f"coordinator_core.hooks: FAILED to import {module_path!r} "
                f"({type(exc).__name__}: {exc}) — its op(s) will NOT be "
                f"registered; the other {len(_EAGER_HOOK_MODULES) - 1} hook "
                f"modules are unaffected. Dispatching any op owned by this "
                f"module will re-raise this exact cause instead of "
                f"'unknown op'.",
                file=_sys.stderr,
            )
            _traceback.print_exc(file=_sys.stderr)
        else:
            _POISONED_MODULES.pop(module_path, None)


# Lazy is the only mode: importing this bare package never eagerly registers
# any op. The former `_lazy_ops_requested()` gate (COORDINATOR_CORE_LAZY_OPS
# env var / sys._coordinator_core_lazy_ops in-process attribute, reused
# verbatim from coordinator_core.ops's own now-retired channel) is retired —
# there is no longer a flag to read or a channel to arm, so no conditional
# call to _eager_import_all() happens here. Callers reach registration
# through the targeted per-op import (ipc.py's registry-miss path) or, for
# the rare full-registration need, by calling _eager_import_all() directly.

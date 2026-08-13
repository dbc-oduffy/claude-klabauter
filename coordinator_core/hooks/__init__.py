"""
coordinator_core.hooks — advisory and bookkeeping hook op handlers package.

Purpose: Namespace for coordinator_core hook op implementations. Each sub-module
self-registers its handler via register_op() at import time under the "hooks.<name>"
method namespace. Importing this package triggers all 15 registrations, making the
hooks.* ops available to the IPC server — UNLESS lazy mode is active (see below), in
which case the eager import list is skipped and callers must trigger a targeted
per-op import themselves (see coordinator_core.ipc's hooks-scoped registry-miss
fallback, C2).

Every hook module that calls register_op() MUST be imported here. A module left out
still registers its op whenever some unrelated importer happens to pull it in, which
makes the op appear in the live _REGISTRY only under certain import orders — and the
op-registry drift guards (authz OP_CLASSIFICATION coverage, ipc _OP_KEY_SCOPE
coverage, OP_MODULE_MAP parity) then pass or fail depending on test ordering rather
than on the actual registration state. context_pressure_precompact was exactly that
shape until 2026-07-22.

Lazy hook registration (docs/plans/2026-08-06-windows-hot-path-less-work-per-
interpreter.md § C1): a hook entrypoint needing exactly one module (e.g. track-touched-files.py
importing only coordinator_core.hooks.track_touched_files) used to pay for all 15
registrations and their full transitive import graph (measured ~31ms / 107 modules)
just to reach the one it wanted, because importing this package always ran its
__init__.py to completion first.

This package REUSES coordinator_core.ops's existing two-channel lazy-registration
contract VERBATIM — the same operator-override env var and the same in-process sys
attribute, read by this package's own predicate below — rather than minting a
hooks-specific channel nobody would arm:

  - `COORDINATOR_CORE_LAZY_OPS` is the OPERATOR override, authoritative in BOTH
    directions ("1" forces lazy; any other value forces eager, whatever the
    in-process channel says) — the identical variable coordinator_core.ops already
    reads, not a new one.
  - `sys._coordinator_core_lazy_ops` is the IN-PROCESS channel — the identical `sys`
    attribute coordinator_core.ops already reads, NOT an env var and NOT a new `sys`
    attribute name. It is not an env var because an env var leaks to every child
    spawned without an explicit `env=`, and 59 test modules in this tree assert the
    op registry at import time — an env-based in-process channel broke pytest
    collection on a green tree (documented history, see
    coordinator_core.ops._lazy_ops_requested's own docstring). Reusing the exact
    attribute means every hook script that already arms coordinator_core.ops's lazy
    channel (via _arm_lazy_ops(), e.g. preuse-write-dispatch.py,
    preuse-bash-dispatch.py) gets lazy hooks for free, with no new channel for
    anyone to arm.

Default MUST remain eager (neither channel armed): importing this package eagerly
registers all 16 hooks.* ops exactly as before this chunk, so the op-registry drift
guards this docstring names above continue to see the FULL registered set at import
time in every consumer that does not opt into lazy mode — this is the same
default-preserving guarantee coordinator_core.ops makes for the analogous op-registry
drift checks on its own side.

Under LAZY mode, those three drift guards stay honest because nothing in this
package changes what "the full registered set" IS — only when eager import runs.
Any caller of the drift guards (or of coordinator_core.ops._eager_import_all(),
which itself imports coordinator_core.hooks per its own _EAGER_OP_MODULES entry) is
either (a) a plain `import coordinator_core.hooks` with neither lazy channel armed,
which still runs _eager_import_all() below unconditionally, or (b) explicitly opts
into lazy mode and is expected to force full registration first via
_eager_import_all() (C2's registry-miss fallback does exactly this on a hooks.* miss)
before relying on registry completeness. No guard observes a partially-registered
state as if it were final.

_eager_import_all() is also this package's full-load routine, mirroring
coordinator_core.ops._eager_import_all() in name and role: it forces complete
hooks.* registration on demand, regardless of the lazy flag or of whatever partial
import state this package is already in — idempotent, since re-importing an
already-imported submodule is a cheap no-op. Pinned for C2 (coordinator_core.ipc's
registry-miss fallback, later wave): call
`coordinator_core.hooks._eager_import_all()` — zero arguments, returns None.

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

Zero-tool-use detection ops (cross-repo example-doctrine-repo contract, Stage 1 write / Stage 2 read).
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

Subagent arrival-check op (cross-repo example-doctrine-repo contract, sibling of the zero-tool-use
trio above — different question, same pull/poll posture):
    subagent_arrival_check          — pull/poll: classify one agent_id as
                                       arrived/running/unknown by tailing ONLY that
                                       subagent's own transcript (last record, never a
                                       full-file parse); fails toward "running"/"unknown",
                                       never a false "arrived"; returns structured data

Spec backlinks:
    docs/plans/2026-07-04-pcore-04-advisory-hook-ops-claude-klabauter-engine.md
    docs/plans/2026-07-04-pcore-08-async-bookkeeping-hooks-engine-vs-mcp.md
    cross-repo/inbox/2026-07-25-example-doctrine-repo-em-zero-tool-use-detection-engine-op-contract.md
    cross-repo/inbox/2026-07-25-example-doctrine-repo-em-zero-tool-use-detection-verdict-viable.md
"""

from __future__ import annotations

import importlib
import os as _os
import sys as _sys
import traceback as _traceback
from typing import Dict

# ---------------------------------------------------------------------------
# Eager-import table: dotted module path per hook module that used to be a bare
# `from coordinator_core.hooks import X` statement. Kept as data (mirroring
# coordinator_core.ops._EAGER_OP_MODULES) so _eager_import_all() is a single
# loop rather than 15 duplicated import lines, and so it can be re-invoked
# on demand (C2's registry-miss fallback) without re-running module-level code.
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
    "coordinator_core.hooks.context_pressure_precompact",  # registers "hooks.context_pressure_precompact"
    "coordinator_core.hooks.subagent_zero_tool_use",  # registers "hooks.subagent_zero_tool_use"
    "coordinator_core.hooks.subagent_zero_tool_use_surface",  # registers "hooks.subagent_zero_tool_use_surface"
    "coordinator_core.hooks.subagent_zero_tool_use_resolve",  # registers "hooks.subagent_zero_tool_use_resolve"
    "coordinator_core.hooks.subagent_arrival_check",  # registers "hooks.subagent_arrival_check"
    "coordinator_core.hooks.subagent_fabrication_check",  # registers "hooks.subagent_fabrication_check"
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
    used to run unconditionally at package-init time; it is now also
    independently callable (by C2's registry-miss fallback in
    coordinator_core.ipc) to force complete hooks.* registration on demand,
    regardless of the COORDINATOR_CORE_LAZY_OPS flag or of which submodules
    (if any) are already imported. Idempotent — re-importing an
    already-imported submodule is a cheap no-op.

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


def _lazy_ops_requested() -> bool:
    """Whether eager hook registration is suppressed for this process.

    Reads the EXACT two channels coordinator_core.ops._lazy_ops_requested()
    reads — no new env var, no new sys attribute. See that function's
    docstring for the full precedence rationale and the 2026-07-28 history of
    why the in-process leg is a sys attribute rather than an environment
    variable (env leaks to every child spawned without explicit env=, and 59
    test modules in this tree assert the op registry at import time).

      - `COORDINATOR_CORE_LAZY_OPS` — operator override, authoritative in BOTH
        directions ("1" forces lazy; any other value forces eager).
      - `sys._coordinator_core_lazy_ops` — in-process channel, armed by the
        same writers coordinator_core.ops already documents
        (coordinator_core.invoke.__main__, coordinator/bin/lib/cc_invoke.py).
        Reusing this exact attribute means arming it once already gets both
        packages' lazy channels for free.

    Deliberately NOT `from coordinator_core.ops import _lazy_ops_requested`:
    coordinator_core.ops's own module-init imports coordinator_core.hooks (it
    is one of ops's _EAGER_OP_MODULES entries), so importing coordinator_core.ops
    from inside coordinator_core.hooks's own module body risks a circular,
    partially-initialized import — and would also defeat this channel's whole
    point by pulling ~55 ops modules' worth of package machinery into what is
    supposed to be a cheap, hooks-only import. This body is kept a verbatim
    hand-mirror instead; a parity test
    (test_lazy_hooks_channel.py::test_lazy_ops_requested_matches_ops_sibling_across_channel_matrix)
    asserts the two functions agree across the channel matrix so the
    "verbatim" claim stays true in practice, not just by inspection.
    """
    operator_override = _os.environ.get("COORDINATOR_CORE_LAZY_OPS")
    if operator_override is not None:
        return operator_override == "1"
    return getattr(_sys, "_coordinator_core_lazy_ops", False) is True


# Default behavior (neither channel armed) — UNCHANGED from before this chunk:
# importing this package eagerly registers all 16 hooks.* ops, keeping the
# op-registry drift guards (authz OP_CLASSIFICATION coverage, ipc
# _OP_KEY_SCOPE coverage, OP_MODULE_MAP parity) honest against the full set.
if not _lazy_ops_requested():
    _eager_import_all()

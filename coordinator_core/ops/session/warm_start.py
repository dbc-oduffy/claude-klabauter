"""
coordinator_core.ops.session.warm_start — session.warm_start op.

Purpose: C25 of docs/plans/2026-08-16-one-engine-for-the-whole-box.md. The op a
coordinator SessionStart hook calls to warm the engine IF AND ONLY IF this
machine opted in (C23's `warm.settings.is_warm_enabled` precedence: the
`COORDINATOR_WARM` env, falsy always winning, then `engine.warm.enabled` in
the machine-local registry, then off — this module does NOT re-derive that
precedence, it only reads its single exported answer).

No device-boot trigger, no scheduled task, no service registration — none of
those exist here; all are foreclosed anyway by
docs/reference/shell-out-carve-outs.md's closed list. The ONLY trigger this
op recognizes is a SessionStart hook invocation, matched exhaustively across
every SessionStart source (`startup|resume|clear|compact|fork` — see
`SESSIONSTART_MATCHERS` below). Omitting a source would silently mean no warm
start for exactly the long-lived sessions (resume/compact/fork) that benefit
most from an already-warm engine.

SHAPE — matches the SessionStart hooks already declared in DoE's hooks.json:
  - async: true. This op produces no context-bound stdout; its entire value
    is the side effect of a detached spawn, so it must not sit on
    first-token-to-context latency. `ASYNC = True` below is this module's
    declaration of that contract for whatever hook-manifest wiring (a
    DIFFERENT repo's concern — coordinator-claude owns hooks.json, not this
    module) consumes it.
  - FAIL OPEN unconditionally. A SessionStart hook that raises greets every
    session in the fleet with a stack trace — `_handler` below never lets an
    exception from `is_warm_enabled`, `should_spawn`, or `spawn_detached`
    escape; each of those three callees already documents its own
    never-raise contract, and this module's own top-level try/except is belt
    over that braces.

IDEMPOTENT BY CONSTRUCTION: reuses C18's `warm.breadcrumb.should_spawn` debounce
pre-check verbatim (never re-derived), so N sessions starting at once produce
at most ONE spawn, and a session starting when a healthy server already
exists (a breadcrumb younger than `SPAWN_DEBOUNCE_SECS` whose pid is alive)
does nothing at all — `_handler` returns without spawning. Spawn itself reuses
`ops.ceremony.detached_spawn.spawn_detached` verbatim, targeting
`warm.client.SERVER_ENTRY_SCRIPT`, the SAME entry script C15's lazy
client-side spawn already uses — one spawn target, two trigger occasions.

THE DIRECT-CHILD INVARIANT IS NOT AT RISK: docs/reference/interactive-launch-chain.md
forbids anything intervening between the operator's shell and claude.exe. This
op fires from a SessionStart hook, which runs AFTER the session (and
`claude.exe`) already exists, and it spawns a DETACHED SIBLING process (never
a child of the launch chain, never anything the hook itself execs into) — it
inserts nothing between the operator's shell and `claude.exe`. This is not
the shim and must never become one.

C15's lazy client-side spawn (`warm.client._spawn_once`, fired on a
FileNotFoundError trying to reach the pipe) REMAINS the fallback, unchanged
by this chunk: a session that missed SessionStart entirely, or whose server
has since demoted (idle timeout, skew eviction), still gets a warm path on
its next call without waiting for a new session. This op only moves the
common-case spawn OFF the first tool call's critical path — it does not
replace the fallback.

Self-registration: importing this module calls
register_op("session.warm_start", _handler) as a side-effect. Add this
module to coordinator_core/ops/__init__.py to trigger registration (a
separate chunk's wiring, outside this row's `writes:`).

Negative-spec:
  - Does NOT re-derive C23's on/off precedence — reads `is_warm_enabled()`
    only.
  - Does NOT re-derive C18's debounce math — reads `should_spawn()` only.
  - Does NOT register a device-boot trigger, scheduled task, or service —
    SessionStart is the only occasion this op recognizes.
  - Does NOT remove or alter C15's lazy client-side spawn fallback.
  - Does NOT wire hooks.json itself — that manifest lives in a different
    repo (coordinator-claude) and is a different chunk's concern; this
    module only supplies the op hooks.json is meant to call, plus the
    `ASYNC` / `SESSIONSTART_MATCHERS` constants documenting the contract
    that wiring must honor.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from coordinator_core.ipc import register_op
from coordinator_core.ops.ceremony.detached_spawn import spawn_detached
from coordinator_core.warm.breadcrumb import should_spawn
from coordinator_core.warm.client import SERVER_ENTRY_SCRIPT
from coordinator_core.warm.settings import is_warm_enabled

__all__ = ["ASYNC", "SESSIONSTART_MATCHERS", "warm_start"]

# hooks.json contract this op is written to satisfy — see module docstring's
# SHAPE section. Not consumed by this module itself; documented here as the
# single source of truth for whatever hook-manifest wiring binds to it.
ASYNC = True
SESSIONSTART_MATCHERS = ("startup", "resume", "clear", "compact", "fork")


def _engine_clone_root() -> Path:
    """This coordinator_core clone's own resolved root — the same
    computation `warm.client._engine_clone_root` / `warm.breadcrumb.
    _default_engine_clone` each keep as a local copy per this package's
    convention of not reaching into a peer module's private name for the
    same one-line computation."""
    return Path(__file__).resolve().parents[3]


def warm_start(engine_root: Optional[Path] = None) -> bool:
    """Warm the engine for THIS machine if opted in, debounced, fail-open.

    Returns True iff a spawn was actually attempted (`spawn_detached` was
    called — its own return value is not surfaced here, matching C15's
    `_spawn_once`, which also does not observe the spawn's outcome); False
    for every other case: warm is off (`is_warm_enabled()` False), or a
    live breadcrumb already vouches for an in-flight/recent spawn
    (`should_spawn()` False).

    NEVER raises — every exception from the three callees this function
    composes (`is_warm_enabled`, `should_spawn`, `spawn_detached`) is
    swallowed here as a second, belt-over-braces guarantee on top of each
    callee's own documented never-raise contract, because this function
    backs a SessionStart hook and a SessionStart hook that raises greets
    every session in the fleet with a stack trace (module docstring, FAIL
    OPEN unconditionally).

    `engine_root` is an injectable override for tests; defaults to this
    module's own resolved clone root, matching `should_spawn`'s own default
    resolution so the debounce check and the spawn target agree on which
    clone they're acting for.
    """
    try:
        if not is_warm_enabled():
            return False
        root = engine_root if engine_root is not None else _engine_clone_root()
        if not should_spawn(root):
            return False
        spawn_detached(str(root), SERVER_ENTRY_SCRIPT)
        return True
    except Exception:
        return False


@register_op("session.warm_start")
async def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC handler for `session.warm_start`. Ignores `params` and
    `repo_root` (this op is machine-scoped, not repo-scoped — it warms THIS
    engine clone regardless of which repo's session triggered it) and
    returns `{"spawned": bool}` — a SessionStart hook has no reason to
    inspect the result (this op's value is side-effect only, per the module
    docstring's `async: true` note), but a non-empty, well-formed result is
    still returned for callers (e.g. tests, manual invocation) that do
    inspect it.
    """
    return {"spawned": warm_start()}

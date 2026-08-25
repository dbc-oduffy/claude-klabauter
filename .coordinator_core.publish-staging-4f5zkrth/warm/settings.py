"""coordinator_core.warm.settings — resolves whether THIS machine runs a
warm engine.

Purpose: C23 of `docs/plans/2026-08-16-one-engine-for-the-whole-box.md`. The
choice is per-machine, never per-repo-clone or per-session, and is read by
every warm-aware entry path (C25's SessionStart trigger, C15's client
preamble) through the single function this module exports.

Resolution precedence, most specific first:

  1. `COORDINATOR_WARM` env — the per-invocation escape hatch. `0`/`false`/
     `no`/`off` ALWAYS wins, overriding anything the registry says, because
     it is scoped to the one process reading it rather than the machine.
  2. `engine.warm.enabled` in the machine-local TOML registry (the same
     DR-132 registry `engine.working_repos.*` and `repos.*` live in,
     resolved via `coordinator_core.machine_resolver.registry_get`) — the
     durable, install-time, per-machine choice.
  3. Off. A machine with neither signal set has never opted in.

STORAGE, and why it is not `~/.claude`: that tree is git-synced across a
user's machines (a Windows desktop and a Mac laptop, concretely), so a
warmth choice recorded there would travel with the sync rather than stay
bound to the machine it was made for — a 24-core engine-authoring desktop's
"yes" would silently arm a laptop that should never run a resident process.
The machine-local registry is machine-local by construction, which is the
property this setting actually needs.

This module resolves; it does not decide the install-time recommendation
(`scripts/setup.py` prompts and writes the registry key) and does not run
the warm server or gate its idle demotion (`coordinator_core.warm.idle`
owns that). A single read function is the entire consumer contract.

MEMOISATION (W12, 2026-08-20-a-refusal-cannot-exit-zero § C8):
`is_warm_enabled` is consulted on every warm-aware entry — a machine-local
TOML read each time, via `registry_get`. Cached process-wide after the
first resolution, deliberately: `COORDINATOR_WARM`/`engine.warm.enabled`
are both an install-time, per-machine choice (this module's own docstring
above), never expected to change mid-process, so re-reading the registry
on every call buys nothing on a short-lived CLI dispatch process (one call
per process either way — the cache changes only whether that one read hits
disk, not how many reads happen) and genuinely amortises on the warm
SERVER, a long-lived process that would otherwise pay this read on every
request. NOT the pattern to carry back to `cc_invoke` (a spawn-per-call
process with no repeat-call population to amortise over).
`_reset_for_test` exists solely so the memo does not leak the FIRST
monkeypatched `registry_get` result across every subsequent test in the
same pytest process; production code never calls it.
"""

from __future__ import annotations

import os
import threading
from typing import Optional

import sys

from coordinator_core.machine_resolver import registry_get

__all__ = ["ENV_VAR", "REGISTRY_KEY", "is_warm_enabled"]

ENV_VAR = "COORDINATOR_WARM"
REGISTRY_KEY = "engine.warm.enabled"

_TRUTHY = frozenset({"1", "true", "yes", "on"})
_FALSY = frozenset({"0", "false", "no", "off"})

_cache_lock = threading.Lock()
_cached_result: Optional[bool] = None

#: Once-per-process announcement flag -- mirrors `warm.client.
#: _log_live_tree_cold_once`'s shape exactly (a module-scope bool, set once,
#: never reset outside a test) for the identical reason that function
#: states: this is the permanent condition of a whole process population,
#: not a per-call fact, so a per-call diagnostic would be pure noise.
_warm_disabled_announced = False


def _reset_warm_disabled_announcement_for_test() -> None:
    """Test-only seam: clear the once-per-process announcement flag."""
    global _warm_disabled_announced
    _warm_disabled_announced = False


def _log_warm_disabled_once() -> None:
    """Emit a one-line stderr notice, once per process, that warmth is off
    and every dispatch from this process is going cold.

    state/handoffs/2026-08-21_103635_reaching-the-warm-engine.md; PM ruling
    verbatim: "keeping this loophole makes us blind to the true state of
    the engine." `COORDINATOR_WARM=0` (or the registry key) is a legitimate,
    deliberate off-switch -- this function does NOT refuse the dispatch,
    only announces the condition, matching the PM's own framing ("Do not
    make warm-disabled fail -- a deliberate off-switch that refuses is
    useless"). Without this, an operator flips the switch once, forgets,
    and the box runs cold forever with nothing ever saying so -- the same
    silent-degradation shape this whole row exists to close, just with a
    human's fingerprint on it instead of a boot-path defect's.

    Register (docs/wiki/guard-messaging.md § Register): one fact, no
    remediation offered -- there is nothing to "fix" about a deliberate
    configuration choice, so naming an alternative here would be the B6
    class this register bans (arguing against a setting the reader chose on
    purpose).
    """
    global _warm_disabled_announced
    if _warm_disabled_announced:
        return
    _warm_disabled_announced = True
    print(
        "[warm-settings] warmth is disabled by configuration "
        f"({ENV_VAR} or {REGISTRY_KEY}) -- every dispatch from this process "
        "runs cold.",
        file=sys.stderr,
    )


def is_warm_enabled() -> bool:
    """Resolve whether this machine should run a warm engine, per the
    precedence documented on this module: `COORDINATOR_WARM` env (falsy
    values always win) -> `engine.warm.enabled` registry key -> off.

    An unrecognized `COORDINATOR_WARM` value (neither truthy nor falsy
    token) is treated as unset and falls through to the registry rung,
    rather than raising — this is a read path consumed on every warm-aware
    entry, not a validated config load.

    Memoised process-wide after the first call (module docstring's
    MEMOISATION note) — the env rung is still re-read live every call (an
    `os.environ` read, not the registry TOML this memo exists to avoid),
    but once a registry read has happened this process, it is never
    repeated.
    """
    env_value = os.environ.get(ENV_VAR, "").strip().lower()
    if env_value in _FALSY:
        _log_warm_disabled_once()
        return False
    if env_value in _TRUTHY:
        return True

    global _cached_result
    with _cache_lock:
        if _cached_result is not None:
            if not _cached_result:
                _log_warm_disabled_once()
            return _cached_result

    registry_value = (registry_get(REGISTRY_KEY) or "").strip().lower()
    result = registry_value in _TRUTHY

    with _cache_lock:
        _cached_result = result
    if not result:
        _log_warm_disabled_once()
    return result


def _reset_for_test() -> None:
    """Test-only: clear the process-wide memo so a fresh test can exercise
    `is_warm_enabled`'s registry rung from a known state. Never called by
    production code — see the module docstring's MEMOISATION note."""
    global _cached_result
    with _cache_lock:
        _cached_result = None

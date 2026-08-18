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
"""

from __future__ import annotations

import os

from coordinator_core.machine_resolver import registry_get

__all__ = ["ENV_VAR", "REGISTRY_KEY", "is_warm_enabled"]

ENV_VAR = "COORDINATOR_WARM"
REGISTRY_KEY = "engine.warm.enabled"

_TRUTHY = frozenset({"1", "true", "yes", "on"})
_FALSY = frozenset({"0", "false", "no", "off"})


def is_warm_enabled() -> bool:
    """Resolve whether this machine should run a warm engine, per the
    precedence documented on this module: `COORDINATOR_WARM` env (falsy
    values always win) -> `engine.warm.enabled` registry key -> off.

    An unrecognized `COORDINATOR_WARM` value (neither truthy nor falsy
    token) is treated as unset and falls through to the registry rung,
    rather than raising — this is a read path consumed on every warm-aware
    entry, not a validated config load.
    """
    env_value = os.environ.get(ENV_VAR, "").strip().lower()
    if env_value in _FALSY:
        return False
    if env_value in _TRUTHY:
        return True

    registry_value = (registry_get(REGISTRY_KEY) or "").strip().lower()
    return registry_value in _TRUTHY

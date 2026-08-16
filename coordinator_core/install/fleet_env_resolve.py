"""fleet_env_resolve — the fallback ladder for when `fleet_env.root` is
absent, or present but its target is unreachable.

Purpose: `coordinator/bin/fleet-env.py::resolve_fleet_env_root()` (C1) is the
sole sanctioned read site for the `fleet_env.root` registry key and returns
`None` on a clean miss — it names no fallback directory of its own by
design (see that module's docstring). This module is the ladder C1's
docstring points at: given the registry's candidate (or `None`), resolve a
usable fleet-environment root, degrading through rungs that are each
writable on a stock machine with no `X:`, no Dev Drive, and no pre-existing
coordinator install. C4's provisioner (`coordinator_core/install/fleet_env.py`,
not yet written) imports and calls `resolve_fleet_env_fallback_root` rather
than re-deriving resolution.

Why "target does not exist" is not the same as "not yet provisioned": the
`.fleet-env` directory itself being absent is the NORMAL pre-provision state
that C4 creates on first run — that is not a failure this ladder reacts to.
What the ladder reacts to is the candidate's own ANCESTRY being unreachable
(no such drive, no such volume, a path nothing on this machine can ever
create) — walked via `_nearest_existing_ancestor` below, not a bare
`Path.exists()` on the leaf.

Rungs, in order:
  1. The registry candidate (`primary_candidate`, from C1), if its nearest
     existing ancestor directory is writable. Not a "fallback" — this is the
     documented, contract-clause location (`docs/reference/
     fleet-shared-environment-contract.md` § "The environment's location");
     this module accepts it unchanged when it is usable.
  2. `<settings-home>/.fleet-env` — `coordinator_core._settings_home
     .settings_home()` resolves via `COORDINATOR_SETTINGS_HOME`, else
     `CLAUDE_HOME`, else `Path.home()` (which honours `USERPROFILE` on
     Windows) with `.coordinator-claude-settings` appended. Pure env/home
     computation, no drive letter, no Dev Drive assumption, and always
     resolves to SOME path on any machine that can run Python at all — this
     is the rung that degrades to "somewhere writable on a stock machine."

No third rung exists. If rung 2's nearest existing ancestor is also
unwritable, resolution fails loud (`FleetEnvResolutionError`) rather than
inventing a third, surprising location — per this repo's cold-path
remediation rule, the raised message names a runnable script
(`machine-local set fleet_env.root <path>`), never a slash command.

Injectable by design (AC5's test needs this without a real stock machine):
`resolve_fleet_env_fallback_root` takes `settings_home_factory` as a keyword
parameter (default `coordinator_core._settings_home.settings_home`) so a
test can substitute a `tmp_path`-backed factory and a deliberately-
unreachable `primary_candidate` (e.g. a path whose nearest ancestor a test
never creates) to exercise every rung without touching real global state.
`_nearest_existing_ancestor` and `_is_writable_root` are independently
importable for a test that wants to probe the ladder's writability
predicate directly.

Negative-spec: does not read the `fleet_env.root` registry key itself (C1's
concern, `coordinator/bin/fleet-env.py::resolve_fleet_env_root`) — this
module never imports that file, to keep C1's read site and C5's fallback
ladder on disjoint write targets (plan review, finding 6). Does not create,
provision, or health-probe the environment (C4). Does not implement sibling
`.pth` binding (C6).

Spec backlink: docs/plans/2026-08-16-one-environment-for-the-fleet.md C5
Spec backlink: docs/reference/fleet-shared-environment-contract.md § DECISIONS (b),
    § "The environment's location — a contract clause, not an executor choice"
"""
from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Callable, Optional, Union

from coordinator_core._settings_home import settings_home as _default_settings_home

_FALLBACK_BASENAME = ".fleet-env"

_REMEDIATION = (
    "fleet_env_resolve: no writable location found for the fleet environment "
    "(registry candidate and the settings-home fallback both failed). "
    "Set a valid location: machine-local set fleet_env.root <writable-path>."
)


class FleetEnvResolutionError(RuntimeError):
    """No rung of the fallback ladder resolved to a writable root."""


def _nearest_existing_ancestor(path: Path) -> "Optional[Path]":
    """Return the nearest existing directory in `path`'s own ancestry.

    Walks `path` itself, then each of `path.parents`, returning the first
    one that exists on disk. Returns `None` if nothing in the chain exists
    (the no-such-drive / no-such-volume case this ladder exists to catch).
    """
    for candidate in (path, *path.parents):
        if candidate.exists():
            return candidate
    return None


def _is_writable_root(path: Path) -> bool:
    """True if `path` is usable as a fleet-environment root.

    Not a `Path.exists()` check on the leaf — the leaf is expected to be
    absent pre-provision (C4 creates it). Usable means: the nearest existing
    ancestor is a real directory this process can actually write into,
    proven by attempting to create and remove a uniquely-named temporary
    entry inside it — not `os.access(path, os.W_OK)`. On Windows,
    `os.access(..., os.W_OK)` reports only the read-only file attribute and
    ignores ACLs entirely, so for a directory it returns `True` in
    essentially all cases, including ACL-denied or elevation-required paths
    this process cannot actually write to. That would make rung 1 accept an
    unwritable registry candidate silently, so the ladder never falls
    through to rung 2 and C4 fails later at provisioning time with a bare
    permission error instead of this module's loud, actionable
    `FleetEnvResolutionError`. Same trap, different flag, as documented for
    `os.access(..., os.X_OK)` in `coordinator/bin/lib/machine_local_resolve
    .py`'s module docstring (the `WinError 193` precedent) — an actual probe
    is the only reliable cross-platform answer and it works identically on
    POSIX.
    """
    ancestor = _nearest_existing_ancestor(path)
    if ancestor is None:
        return False
    if not ancestor.is_dir():
        return False
    probe = ancestor / f".fleet-env-writable-probe-{os.getpid()}-{id(object())}-{time.monotonic_ns()}"
    try:
        probe.mkdir()
    except OSError:
        return False
    else:
        return True
    finally:
        try:
            probe.rmdir()
        except OSError:
            pass


def resolve_fleet_env_fallback_root(
    primary_candidate: "Union[str, Path, None]",
    *,
    settings_home_factory: "Callable[[], Path]" = _default_settings_home,
) -> Path:
    """Resolve a usable fleet-environment root, walking the fallback ladder.

    `primary_candidate` is C1's `resolve_fleet_env_root()` result (the
    registry value, or `None` on absence) — this function performs no
    registry read of its own. Accepts the candidate unchanged when its
    ancestry is writable; otherwise degrades to `<settings-home>/.fleet-env`;
    raises `FleetEnvResolutionError` if that also fails.

    `settings_home_factory` is injectable so a test can point rung 2 at a
    `tmp_path` fixture instead of the real settings home, and pair that with
    a deliberately-unreachable `primary_candidate` to prove both rungs
    without requiring a machine with no `X:` and no Dev Drive.
    """
    if primary_candidate is not None:
        candidate_path = Path(primary_candidate)
        if _is_writable_root(candidate_path):
            return candidate_path

    fallback_path = settings_home_factory() / _FALLBACK_BASENAME
    if _is_writable_root(fallback_path):
        return fallback_path

    raise FleetEnvResolutionError(_REMEDIATION)

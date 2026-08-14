"""
coordinator_core.plugin_health.oracle_surface — the single definition of
"claude-klabauter's fleet-invocable oracle surface": which on-disk directories are
scanned to answer "which oracles currently exist", and how the live
oracle-name set is derived from them.

Both `fleet_reachability.py` (supply side of its delete-safety diff:
DoE-cited demand minus live claude-klabauter oracles) and `bin_inventory_gate.py`
(live side of its disappearance diff: tracked inventory minus live claude-klabauter
oracles) ask the identical question -- "which oracle names currently exist
on this checkout" -- over the identical fixed, finite three-directory
surface. Before this module existed each gate answered it independently:
`fleet_reachability`'s supply side was widened 2026-07-27 (commit
`b1bc5789`) from `coordinator/bin/`-only to also scan `<repo-root>/bin/`
and `coordinator/lib/`, but `docs/install/bin-inventory.json` (seeded by
`5580413e`, the same day) was never widened to match -- it stayed seeded
from `coordinator/bin/` alone. That divergence is exactly how
`claude-klabauter-doctor-probe.py` (a real, `.cmd`-twinned oracle living in
repo-root `bin/`) ended up invisible to BOTH gates at once: unqualified in
DoE's own citation (blind to `fleet_reachability`) AND absent from an
inventory that had never scanned the directory it lives in (blind to
`bin_inventory_gate`) -- see commit `f622297b`'s own "FOLLOW-UP FOUND
WHILE VERIFYING THAT TRADE" note, which named the fix as "the surface
definition needs to exist once and be consumed by both". This module is
that fix.

The three directories, and why each qualifies (see `fleet_reachability`'s
own module docstring for the full inclusion-rule writeup this restates):
  - `coordinator/bin/` -- the original, ~700-CLI supply side.
  - `<repo-root>/bin/` -- root-level oracles with the same `.cmd`-Windows
    -launcher-twin shape (`claude-klabauter-doctor-probe.py`, `shell-init-guard.py`).
  - `coordinator/lib/` -- a MIXED library/CLI directory: it holds genuine
    fleet-invocable oracles (`resolve-coordinator-clone.py`, reserved for a
    different install family than the forwarder loop -- see
    `_AGENT_HELPER_RESERVED_NAMES`) alongside ordinary library-internal
    Python that is never invoked as a CLI at all (`async_hook_status.py`,
    `oss-repo-constants.py`, `release_currency.py`, ...). This module's own
    `live_oracle_names()` does NOT itself discriminate between those two
    classes for `coordinator/lib/` -- it returns the full raw scan, exactly
    as `fleet_reachability` always has, because over-inclusion on the
    LIVE side of a "is this name still reachable" diff cannot manufacture a
    false failure (it can only, in principle, mask one under an
    exceptionally unlucky name collision). The `.cmd`-twin discriminator
    that DOES matter -- "is this specific `coordinator/lib/` name worth
    tracking as an oracle at all" -- is a SEEDING-time judgment applied when
    curating `docs/install/bin-inventory.json`'s entry list, not a live-scan
    filter; baking every `coordinator/lib/` stem into the tracked inventory
    would manufacture exactly the false-alarm-the-day-someone-legitimately-
    refactors-a-library-file risk this repo's CLAUDE.md warns dispatch
    briefs to name explicitly.

Spec backlink: commit f622297b90d98f7ccd8f5796b53fe034ab4b190d (the
"Two gates, one shared blind spot" finding this module closes).
"""

from __future__ import annotations

from pathlib import Path
from typing import List, Optional, Set

from coordinator_core.claude_klabauter_root import coordinator_claude_klabauter_root

# Extensions (plus the bare/extensionless form) a reserved-family oracle
# might carry on disk -- mirrors fleet_reachability._KNOWN_ORACLE_EXTENSIONS,
# duplicated here (not imported) because that tuple is fleet_reachability's
# own private extension-normalization constant, a distinct concern from this
# module's disk-existence probe.
_ORACLE_FILE_EXTENSIONS = ("", ".py", ".js", ".sh", ".cmd")


def resolve_agent_bin(claude_klabauter_root: Optional[Path] = None) -> Optional[Path]:
    """`coordinator/bin/` -- the original, largest leg of the oracle
    surface. Returns `None` (never raises) when claude-klabauter's own root is
    unresolvable on this machine, or `coordinator/bin/` itself does not
    exist -- both callers treat that as their own top-level skip/fail
    precondition, not an error from this resolver."""
    if claude_klabauter_root is None:
        try:
            claude_klabauter_root = Path(coordinator_claude_klabauter_root())
        except RuntimeError:
            return None
    candidate = Path(claude_klabauter_root) / "coordinator" / "bin"
    return candidate if candidate.is_dir() else None


def resolve_extra_oracle_dirs(claude_klabauter_root: Optional[Path] = None) -> List[Path]:
    """The two fleet-exposed oracle directories beyond `coordinator/bin/`
    (see module docstring): `<repo-root>/bin/` and `coordinator/lib/`.
    Returns `[]` (not an error) when claude-klabauter's own root is unresolvable --
    a directory that does not exist is likewise not an error, since
    `_derive_agent_helper_target_map` treats a missing directory as an
    empty map. Callers pass the RESULT of this straight through as-is; it
    intentionally does not filter to only existing directories, matching
    `_derive_agent_helper_target_map`'s own missing-directory tolerance."""
    if claude_klabauter_root is None:
        try:
            claude_klabauter_root = Path(coordinator_claude_klabauter_root())
        except RuntimeError:
            return []
    claude_klabauter_root = Path(claude_klabauter_root)
    return [claude_klabauter_root / "bin", claude_klabauter_root / "coordinator" / "lib"]


def live_oracle_names(oracle_dirs: List[Path]) -> Set[str]:
    """Union of installed-name oracle stems (the `.py`-stripped,
    `.cmd`/`.ps1`-twin-excluded form `_derive_agent_helper_target_map`
    derives from one directory) across every directory in `oracle_dirs`.
    This is the ONE live-scan primitive both gates call -- `fleet_
    reachability` further `_normalize()`s the result for its own
    extension-agnostic demand comparison; `bin_inventory_gate` compares it
    directly against the tracked inventory's own raw installed-name
    entries.

    Reserved-name restoration: `_derive_agent_helper_target_map` pops every
    `_AGENT_HELPER_RESERVED_NAMES` entry from ITS OWN returned mapping,
    unconditionally, on every call -- correct for its own purpose (those
    names install via a different family than the forwarder-generation loop
    it serves), but that pop makes a reserved name that genuinely EXISTS on
    disk (`resolve-coordinator-clone` is the exemplar: reserved for
    `coordinator/bin/`'s forwarder loop, yet its real oracle lives one
    directory over in `coordinator/lib/`) invisible to any caller of this
    function, across EVERY given directory, not only the one where it is
    reserved. Restored here by checking real disk existence directly,
    independent of the forwarder-installability question `_derive_agent_
    helper_target_map` answers."""
    # Lazy import, mirroring fleet_reachability's own precedent: substrate.py
    # is a live install-surface module, imported at call time rather than
    # module load so an in-flight edit there can't break this module merely
    # by being imported alongside it.
    from coordinator_core.install.substrate import (
        _AGENT_HELPER_RESERVED_NAMES,
        _derive_agent_helper_target_map,
    )

    names: Set[str] = set()
    for oracle_dir in oracle_dirs:
        derived = _derive_agent_helper_target_map(oracle_dir)
        names.update(derived.keys())

    for reserved in _AGENT_HELPER_RESERVED_NAMES:
        if any(
            (oracle_dir / f"{reserved}{ext}").is_file()
            for oracle_dir in oracle_dirs
            for ext in _ORACLE_FILE_EXTENSIONS
        ):
            names.add(reserved)

    return names

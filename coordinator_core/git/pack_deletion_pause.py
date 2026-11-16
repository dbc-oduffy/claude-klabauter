"""coordinator_core.git.pack_deletion_pause -- the stop switch for engine pack deletion.

Purpose: while `PACK_DELETION_PAUSED` is true the engine deletes no pack file and launches no
repack or gc in any repo: `git_objects._flush_pack` folds nothing, and `store_hygiene` /
`git_maintenance` pack-deleting paths defer with nothing unlinked and no git spawned. One
exception: the light daily tier (MIDX heal and `git multi-pack-index write`; no pack deleted,
no repack, no gc) runs when `store_hygiene.midx_upkeep_owed`, and the idle maintenance tick
spawns only for that. Re-enabling is a deliberate edit of the constant, made only once every
pack-deleting path holds `fleet_maintenance_lock`.

Callers read `paused()` at call time, never the constant at import, so a test can flip it.
"""

from __future__ import annotations

PACK_DELETION_PAUSED = True


def paused() -> bool:
    """True when the engine must not delete a pack file or launch a repack or gc; the light daily tier is the one exception."""
    return PACK_DELETION_PAUSED

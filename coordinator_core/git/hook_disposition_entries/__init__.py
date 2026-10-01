"""Entry modules for `coordinator_core.git.hook_dispositions`. Each module
exposes `ENTRY`, a `HookDisposition`.

Loaded lazily by `load_entries` because entry modules import the disposition
types from `hook_dispositions`.
"""

from __future__ import annotations

from typing import Tuple


def load_entries() -> Tuple:
    from . import lfs_pre_push_gate, retired_auto_push_post_commit, retired_engine_pre_commit

    return (
        retired_auto_push_post_commit.ENTRY,
        retired_engine_pre_commit.ENTRY,
        lfs_pre_push_gate.ENTRY,
    )

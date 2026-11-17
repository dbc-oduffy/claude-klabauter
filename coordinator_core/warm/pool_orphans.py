"""Reap pool worker processes that belong to no live executor."""

from __future__ import annotations

import multiprocessing

_JOIN_SECS = 2.0


def reap_orphans(candidates: frozenset, live_pids: frozenset) -> int:
    """Terminate each pid in `candidates` (retired-worker ledger) that is absent from `live_pids`;
    return the count reaped.

    The caller passes the ledger in, so this module never imports `pool_retire`. `active_children()`
    only confirms a candidate is still a live child of this process; a child outside `candidates`
    is never touched. One pass, a bounded join per child. Raises nothing.
    """
    reaped = 0
    try:
        targets = set(candidates) - set(live_pids)
        if not targets:
            return 0
        for proc in multiprocessing.active_children():
            try:
                if proc.pid not in targets:
                    continue
                proc.terminate()
                proc.join(_JOIN_SECS)
                if not proc.is_alive():
                    reaped += 1
            except Exception:  # noqa: BLE001 -- one stubborn child never stops the pass
                continue
    except Exception:  # noqa: BLE001
        pass
    return reaped

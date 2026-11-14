"""Staged plan rows that never ran while a row at or before their wave did.

Both landing paths (the normal land and the halt commit) read this: when it returns any id, the
executed rows' product is withheld and the withhold reason names the unexecuted ids.
"""

from __future__ import annotations

from collections.abc import Iterable

from coordinator_core.ops.dispatch_emit.ask_contract import ManifestRow

#: The withhold reason code a landing path records against a withheld product path.
WITHHOLD_REASON = "unexecuted-plan-rows"


def unexecuted_plan_rows(staged_rows: Iterable[ManifestRow], executed_ids: Iterable[str]) -> tuple[str, ...]:
    """Return the sorted ids of staged rows that did not execute and sit at or after the earliest executed wave.

    "Executed" means the row ran at all: callers pass every id whose report file exists (any
    status) plus a halt's `stopped_rows`; a row that ran and stopped is never "unexecuted".
    Empty `executed_ids` returns `()`. Pure: no I/O.
    """
    rows = tuple(staged_rows)
    executed = frozenset(executed_ids)
    waves = [r.wave for r in rows if r.id in executed]
    if not waves:
        return ()
    earliest = min(waves)
    return tuple(sorted(r.id for r in rows if r.id not in executed and r.wave >= earliest))


def message(ids: Iterable[str]) -> str:
    """The withhold reason text naming the unexecuted ids, e.g. `unexecuted plan rows: C4, C5`."""
    return "unexecuted plan rows: " + ", ".join(ids)

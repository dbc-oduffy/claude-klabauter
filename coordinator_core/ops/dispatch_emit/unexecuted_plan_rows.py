"""Staged plan rows that never ran while a row at or before their wave did.

Both landing paths (the normal land and the halt commit) read this: when it returns any id, the
executed rows' product is withheld and the withhold reason names the unexecuted ids.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from typing import Optional

from coordinator_core.ops.dispatch_emit.ask_contract import ManifestRow
from coordinator_core.ops.dispatch_emit.in_run_widen import _reply_value

#: The withhold reason code a landing path records against a withheld product path.
WITHHOLD_REASON = "unexecuted-plan-rows"

_DONE_RE = re.compile(r"^[\s*_`#>-]*DONE\b", re.MULTILINE)


def satisfied_blocks(chunks: Iterable, read_report: Callable[[str], Optional[str]]) -> tuple[str, ...]:
    """Ids of chunks whose ROW-LOCAL-BLOCK path another chunk wrote and reported DONE.

    `chunks` carry `id`, `report` and `paths`; `read_report(rel)` returns the report text or None.
    A chunk never satisfies its own block. Pure over its inputs.
    """
    chunks = tuple(chunks)
    texts = {c.id: ((read_report(c.report) if c.report else None) or "") for c in chunks}
    done_paths = {c.id: set(c.paths) for c in chunks if _DONE_RE.search(texts[c.id])}
    out = []
    for c in chunks:
        value = _reply_value(texts[c.id])[0]
        if value is not None and any(value in paths for cid, paths in done_paths.items() if cid != c.id):
            out.append(c.id)
    return tuple(sorted(out))


def unexecuted_plan_rows(
    staged_rows: Iterable[ManifestRow], executed_ids: Iterable[str], satisfied: Iterable[str] = ()
) -> tuple[str, ...]:
    """Return the sorted ids of staged rows that did not execute and sit at or after the earliest executed wave.

    "Executed" means the row ran at all: callers pass every id whose report file exists (any
    status) plus a halt's `stopped_rows`; a row that ran and stopped is never "unexecuted".
    A row held, through `deps`, by a `satisfied` row (a row-local block another row's DONE edit
    answered) is not unexecuted. Empty `executed_ids` returns `()`. Pure: no I/O.
    """
    rows = tuple(staged_rows)
    executed = frozenset(executed_ids)
    deps = {r.id: tuple(r.deps) for r in rows}
    freed = set(satisfied)

    def _held_by_satisfied(row_id: str) -> bool:
        seen: set[str] = set()
        stack = list(deps.get(row_id, ()))
        while stack:
            d = stack.pop()
            if d in seen:
                continue
            seen.add(d)
            if d in freed:
                return True
            stack.extend(deps.get(d, ()))
        return False

    waves = [r.wave for r in rows if r.id in executed]
    if not waves:
        return ()
    earliest = min(waves)
    return tuple(
        sorted(r.id for r in rows if r.id not in executed and r.wave >= earliest and not _held_by_satisfied(r.id))
    )


def message(ids: Iterable[str]) -> str:
    """The withhold reason text naming the unexecuted ids, e.g. `unexecuted plan rows: C4, C5`."""
    return "unexecuted plan rows: " + ", ".join(ids)

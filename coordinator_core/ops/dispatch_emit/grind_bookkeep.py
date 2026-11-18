"""Grind run bookkeeping without agents: the typed contract for close, land and ledger sweep.

`no_product_edit` decides, from a run's settled rows, whether the run changed no product file;
`bookkeep` settles such a run in-process. The body of `bookkeep` is delivered by a later row.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from coordinator_core.ops.dispatch_emit import record_paths

#: A settled row counts as closed when its `outcome` is one of these or its `closed` flag is truthy.
CLOSED_OUTCOMES = frozenset({"closed", "committed"})


def row_closed(row: Mapping[str, Any]) -> bool:
    """True when the settled `row` is closed: `closed` truthy, or `outcome` in `CLOSED_OUTCOMES`."""
    return bool(row.get("closed")) or row.get("outcome") in CLOSED_OUTCOMES


def no_product_edit(rows: Sequence[Mapping[str, Any]]) -> bool:
    """True when `rows` is non-empty, every row is closed, and every `touchedFiles` path is a record
    path (`record_paths.is_record_path`). An empty row list is False: nothing settled, nothing to skip."""
    if not rows:
        return False
    for row in rows:
        if not row_closed(row):
            return False
        if not all(record_paths.is_record_path(p) for p in row.get("touchedFiles") or ()):
            return False
    return True


def bookkeep(repo_root: Path | str, run_id: str, rows: Sequence[Mapping[str, Any]]) -> dict:
    """Close, land and ledger-sweep a no-product-edit grind run in-process.

    `rows` are the run record's settled rows, each carrying `verdict` and `touchedFiles`. Returns
    `{closed, committed, sha, swept, error}`; a row with a product path is refused through `error`,
    never landed.
    """
    raise NotImplementedError("bookkeep body is delivered by C3")

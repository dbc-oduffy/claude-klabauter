"""
coordinator_core.tracker_id_grammar — the public ``item.id`` recognizer.

A true leaf module: imports ``re`` and nothing else from this package. Exists
so both `tracker_entities.mint_item_id` (the mint path's charset guard) and
`coordinator_core.ops.emit.closure_trailer` (the ``Closes:`` trailer pattern
table) can share one recognizer without either importing the other's home
module — `tracker_entities` is not itself a leaf (it imports
`coordinator_core.ops.ceremony.completion_entry` and
`coordinator_core.ops.emit._slug` at module scope), so importing it from
inside `ops.emit` closes a real import cycle (probed 2026-08-18, see
docs/plans/2026-08-18-sat-07-tier-a-wiring.md task C1). Putting the
recognizer here instead means the mint path and the recognizer cannot
diverge, without reintroducing that cycle in either direction.

Grammar mirrors `tracker_entities.mint_item_id`'s format string exactly:
``itm-<YYYYMMDD>-<slug 1..32 chars, [a-z0-9-]>-<nonce, 6 lowercase hex>-
<digest, 12 lowercase hex>``.
"""

from __future__ import annotations

import re

ITEM_ID_BODY = r"itm-\d{8}-[a-z0-9-]{1,32}-[0-9a-f]{6}-[0-9a-f]{12}"
"""Unanchored grammar body, for embedding into a larger pattern (e.g. a
trailer-value pattern table row that wraps it in a capturing group). Prefer
`ITEM_ID_PATTERN`/`is_item_id` for standalone recognition."""

ITEM_ID_PATTERN: re.Pattern[str] = re.compile(rf"^{ITEM_ID_BODY}$")
"""Recognizes a well-formed `item.id` as minted by `mint_item_id`. Anchored
both ends — never used with `.search`, only `.match`/`.fullmatch`."""


def is_item_id(value: str) -> bool:
    """True iff *value* is a well-formed `item.id` per `ITEM_ID_PATTERN`."""
    return ITEM_ID_PATTERN.match(value) is not None

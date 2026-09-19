"""
coordinator_core.ops.dispatch_emit.slot_order — chronological order of per-fire
review-trail slots WITHIN one run.

Purpose: `recycle-check.py` and `emit-wave-fire.py` both need to order the
per-fire slot directories a run writes execution/pointer records into
(`<run>/wave-<index>-<fireId>/`, or `<run>/repair-<fireId>/` for a repair) so
that LATEST RECORD WINS reads the actual latest one. Lexical order does not
answer this: `wave-10-…` sorts before `wave-2-…` as text, which would report a
ten-wave run's oldest record as its newest.

Extracted (review: coordinator:overengineering-reviewer, finding 5) from
`recycle-check.py::_slot_order`, which `emit-wave-fire.py::_slot_order_fn` used
to load by `importlib.util.spec_from_file_location` against the hyphenated
sibling file path. Both callers now import this module directly instead —
same function, same behaviour, one definition instead of a file-path load.
"""

from __future__ import annotations

import re
from pathlib import Path


def slot_order(run_dir: Path, rec: Path):
    """Chronological order of two records of one baton WITHIN one run. `sidecarFor` writes each
    fire's records into `<run>/wave-<index>-<fireId>/`, so a baton planned in wave 0 and re-planned
    in wave 2 has two records in one run, and LATEST RECORD WINS has to order them. Lexical order
    does not: `wave-10-…` sorts before `wave-2-…`, which would report a ten-wave run's oldest
    verdict as current. A record flat at the run root predates the slot and is earliest; a
    `repair-…` slot carries no wave index and is written after the waves it re-dispositions."""
    parts = rec.relative_to(run_dir).parts
    slot = parts[0] if len(parts) > 1 else ""
    if not slot:
        return (0, 0, "")
    m = re.match(r"wave-(\d+)-", slot)
    return (1, int(m.group(1)), slot) if m else (2, 0, slot)

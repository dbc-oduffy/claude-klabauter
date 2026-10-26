"""A halt gap line that is a pytest node id is closed when the run's reverify record shows it passing.

Pure; no I/O. The record is ``inline_review["reverify"]`` (see ``landing_integrity.reverify_record``).
"""

from __future__ import annotations

import re
from typing import Mapping

_NODE_ID_RE = re.compile(r"^(?P<id>[\w./\\-]+\.py(?:::[\w.]+)+(?:\[[^\]]*\])?)(?::\s.*)?$", re.DOTALL)


def node_id_of(line: object) -> str | None:
    """The pytest node id a halt gap line leads with (``path.py::test[param]``, optionally followed by
    ``: message``), else None."""
    match = _NODE_ID_RE.match(line.strip()) if isinstance(line, str) else None
    return match.group("id").replace("\\", "/") if match else None


def _ids(values: object) -> set[str]:
    return {str(v).replace("\\", "/") for v in values if v} if isinstance(values, (list, tuple)) else set()


def _run_set(record: Mapping) -> set[str]:
    run: set[str] = set()
    for key in ("kept", "reverted"):
        entries = record.get(key)
        for entry in entries if isinstance(entries, list) else ():
            if isinstance(entry, Mapping):
                run |= _ids(entry.get("tests"))
    return run | _ids(record.get("passing"))


def closed_by_reverify(line: object, record: object) -> bool:
    """True iff ``line`` parses as a node id that ``record`` lists in its run set and in none of
    ``failing`` / ``pre_existing`` / ``unattributed``. A missing, non-mapping or errored
    (``reverify_error``, ``indeterminate``) record closes nothing."""
    node = node_id_of(line)
    if node is None or not isinstance(record, Mapping):
        return False
    error = record.get("reverify_error")
    if isinstance(error, str) and error.strip():
        return False
    after = record.get("status_after")
    if (after if after is not None else record.get("status_before")) == "indeterminate":
        return False
    red = _ids(record.get("failing")) | _ids(record.get("pre_existing")) | _ids(record.get("unattributed"))
    return node in _run_set(record) and node not in red

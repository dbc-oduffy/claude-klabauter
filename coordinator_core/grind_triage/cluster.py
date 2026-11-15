"""Group survivor rows into one cluster per normalised failure signature, so one agent takes each cluster.

A test row's signature is its first failure line with paths, hex ids and numbers masked; a non-test row
clusters by the surface file its target cites. Pure and deterministic: no I/O, same rows in any order
give the same clusters.
"""

from __future__ import annotations

import re
from collections import defaultdict
from typing import Iterable

from coordinator_core.bug_blitz.mint import first_line
from coordinator_core.grind_triage.contract import Cluster, TriageRow

# Order matters: paths go before ids and numbers so `a/b_12.py:34` collapses whole.
_MASKS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"[A-Za-z]:[\\/][^\s'\"():,]+"), "<path>"),
    (re.compile(r"(?<![\w<])(?:\.{0,2}[\\/])?[\w.\-]+(?:[\\/][\w.\-]+)+(?::\d+)*"), "<path>"),
    (re.compile(r"\b[\w\-]+\.(?:py|ts|tsx|js|cpp|h|hpp|cs|rs|yaml|yml|json|md)(?::\d+)*\b"), "<path>"),
    (re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"), "<id>"),
    (re.compile(r"\b0[xX][0-9a-fA-F]+\b"), "<id>"),
    (re.compile(r"\b(?=[0-9a-f]*\d)(?=[0-9a-f]*[a-f])[0-9a-f]{7,}\b"), "<id>"),
    (re.compile(r"\d+(?:\.\d+)?"), "<n>"),
)
_SPACE = re.compile(r"\s+")
_NO_SIGNATURE = "(no failure text)"
_SURFACE_ANCHOR = re.compile(r"[:#].*$")


def normalise_signature(text: str) -> str:
    """The first failure line of `text` with paths, ids and numbers masked; "" when `text` has no line."""
    line = first_line(text)
    for pattern, mask in _MASKS:
        line = pattern.sub(mask, line)
    return _SPACE.sub(" ", line).strip()


def _surface(target: str) -> str:
    """The file a non-test row's target cites, without a `:line` or `#anchor` tail; a bare drive prefix survives."""
    target = target.strip().replace("\\", "/")
    drive = ""
    if len(target) > 2 and target[1] == ":" and target[2] == "/":
        drive, target = target[:2], target[2:]
    return drive + _SURFACE_ANCHOR.sub("", target)


def row_signature(row: TriageRow) -> str:
    """The cluster signature of `row`: `test:<normalised failure line>` or `surface:<file>`."""
    if row.is_test:
        sig = normalise_signature(row.signature_text)
        if sig:
            return f"test:{sig}"
        return f"test:{_NO_SIGNATURE}:{_surface(row.target.split('::', 1)[0])}"
    return f"surface:{_surface(row.target or row.title)}"


def cluster_rows(rows: Iterable[TriageRow]) -> tuple[Cluster, ...]:
    """One `Cluster` per distinct signature, sorted by signature; `row_keys` sorted and de-duplicated.

    A test row with no failure text clusters by its test file only: no text is no evidence that rows in
    different files share a cause.
    """
    groups: dict[str, set[str]] = defaultdict(set)
    for row in rows:
        groups[row_signature(row)].add(row.key)
    return tuple(Cluster(signature=sig, row_keys=tuple(sorted(groups[sig]))) for sig in sorted(groups))

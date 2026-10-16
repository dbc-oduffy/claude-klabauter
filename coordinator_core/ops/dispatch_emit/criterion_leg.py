"""Classify a plan's exit criterion as naming a typecheck.

Invariant: an executor row is never the one to discharge a criterion typecheck; the
terminal test phase runs it. A row that ran one anyway and was refused by a guard
releases its dependents through the guard-denied report line.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from coordinator_core.ops.dispatch_emit.gate_owed import KIND_GUARD_DENIED, report_line

_VOCAB_RE = re.compile(
    r"\b(?:vue-tsc|tsc|typecheck|type-check|type check|mypy|pyright)\b", re.IGNORECASE
)
_BACKTICK_RE = re.compile(r"`([^`\n]+)`")


@dataclass(frozen=True)
class CriterionTypecheck:
    """`commands`: backticked spans naming a typecheck, verbatim, in order, deduped.
    Empty when the criterion names a typecheck in prose only."""

    commands: tuple[str, ...]


def criterion_typecheck(statement: Optional[str]) -> Optional[CriterionTypecheck]:
    """None when `statement` is None/empty or names no typecheck."""
    if not statement or not _VOCAB_RE.search(statement):
        return None
    seen: dict[str, None] = {}
    for m in _BACKTICK_RE.finditer(statement):
        span = m.group(1).strip()
        if span and _VOCAB_RE.search(span):
            seen.setdefault(span, None)
    return CriterionTypecheck(commands=tuple(seen))


CRITERION_TYPECHECK_CLAUSE = (
    "The plan's exit criterion names a typecheck. This run's terminal test phase runs it: "
    "run no typecheck for it in this row. "
    "If a guard refuses a typecheck you ran anyway, do not retry it: return PARTIAL with this "
    "report line, copied as the second line of your reply, under the status line:\n"
    f"{report_line(KIND_GUARD_DENIED, '<command> (<guard>)')}"
)

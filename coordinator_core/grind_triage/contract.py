"""Frozen shapes shared by the grind triage stages: rows in, verdicts and clusters out, checkpoint events.

Pure data plus `default_budget`; no I/O. Triage, clustering and checkpointing import from here only.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional

CHECKPOINT_PERCENTS: tuple[int, ...] = (25, 50, 75)


class Verdict(str, Enum):
    """Per-row triage outcome: only SURVIVOR reaches an agent."""

    FLAKE = "flake"  # a test row passed in isolation
    NOT_REPRODUCED = "not_reproduced"  # a non-test row's cited anchor is gone at HEAD
    SURVIVOR = "survivor"


@dataclass(frozen=True)
class TriageRow:
    """One grinder row. `key` is the stable identity (target) that mint dedupes on."""

    key: str
    title: str
    is_test: bool
    target: str = ""  # test node id for a test row, cited path/anchor otherwise
    signature_text: str = ""  # raw failure text normalised later into a cluster signature


@dataclass(frozen=True)
class TriageResult:
    """A row's verdict with the evidence that produced it."""

    key: str
    verdict: Verdict
    evidence: str = ""


@dataclass(frozen=True)
class Cluster:
    """Survivor rows sharing one normalised failure signature; one agent takes the cluster."""

    signature: str
    row_keys: tuple[str, ...]


@dataclass(frozen=True)
class CheckpointEvent:
    """An EM progress report emitted when `percent` of rows have settled."""

    percent: int
    settled: int
    total: int
    run_id: Optional[str] = None


def default_budget(row_count: int) -> int:
    """Token budget for a run of `row_count` rows when the caller passes none.

    Scaled so admission's `budget_tokens // ROW_OUTPUT_TOKENS` covers every row, plus the fixed
    run reserve (RUN_FIXED_AGENT_CALLS rows' worth). Raises ValueError on a negative count.
    """
    if row_count < 0:
        raise ValueError(f"row_count must be non-negative, got {row_count}")
    # Function-level import: grind_admission imports this module to apply the default.
    from coordinator_core.ops.dispatch_emit.grind_admission import (
        ROW_OUTPUT_TOKENS,
        RUN_FIXED_AGENT_CALLS,
    )

    return (row_count + RUN_FIXED_AGENT_CALLS) * ROW_OUTPUT_TOKENS

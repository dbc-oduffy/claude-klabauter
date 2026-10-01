"""Gate-verdict contract: observed gate output vs the state a fixture planted. Pure stdlib."""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Mapping


class VerdictClass(enum.Enum):
    CLEAN = "clean"
    ADVISORY = "advisory"
    HALT = "halt"
    UNMEASURED = "unmeasured"


_EXIT_ZERO_CLASSES = frozenset({VerdictClass.CLEAN, VerdictClass.ADVISORY})


@dataclass(frozen=True)
class GateObservation:
    exit_code: int
    verdict: str | None
    magnitude: Mapping[str, int]


@dataclass(frozen=True)
class GateExpectation:
    verdict: str | None
    verdict_class: VerdictClass
    magnitude: Mapping[str, int]


def disagreement(
    gate: str, observed: GateObservation, expected: GateExpectation
) -> str | None:
    """None when exit code, verdict and every expected magnitude agree; otherwise ONE line
    naming the gate and every disagreeing axis as `axis: observed=X expected=Y`."""
    axes: list[str] = []
    want_zero = expected.verdict_class in _EXIT_ZERO_CLASSES
    if (observed.exit_code == 0) != want_zero:
        axes.append(
            f"exit_code: observed={observed.exit_code} "
            f"expected={'0' if want_zero else 'nonzero'}"
            f" ({expected.verdict_class.value})"
        )
    if observed.verdict != expected.verdict:
        axes.append(
            f"verdict: observed={observed.verdict} expected={expected.verdict}"
        )
    for key, want in expected.magnitude.items():
        got = observed.magnitude.get(key)
        if got != want:
            axes.append(f"{key}: observed={got} expected={want}")
    if not axes:
        return None
    return f"{gate}: " + "; ".join(axes)

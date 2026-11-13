"""Run budget (agents, tokens, minutes) for the distill and learn-lessons runs.

Vendored from coordinator-content-repo `coordinator/contract/ceremony-budget.json` @
19e0778361 as `ceremony-budget.json` beside this module; the DoE path is never
read at runtime. The file carries one shared `slice` plus
`whole_backlog_token_bar`, so `load` returns the same slice for every listed
ceremony. A missing or malformed file falls back to `PINNED_DEFAULT`
(`source == "pinned-default"`); loading never refuses.

`fit_units` is the one fit function both admissions call: it narrows, never
refuses and never shards. Per-agent token and minutes constants belong to each
caller, not here.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

CEREMONIES = frozenset({"distill", "learn-lessons"})
VENDORED_PATH = Path(__file__).with_name("ceremony-budget.json")
_AXES = ("agents", "tokens", "minutes")


@dataclass(frozen=True)
class RunBudget:
    """Cap per run. `source` is provenance only and excluded from equality."""

    agents: int
    tokens: int
    minutes: int
    source: str = field(default="vendored", compare=False)


@dataclass(frozen=True)
class UnitCost:
    """Cost of a fixed overhead or of one unit (a scan batch, a triage batch)."""

    agents: int = 0
    tokens: int = 0
    minutes: int = 0


PINNED_DEFAULT = RunBudget(agents=10, tokens=200_000, minutes=15, source="pinned-default")
PINNED_WHOLE_BACKLOG_BAR = 1_000_000


def _pos_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _read(path: Optional[Path]) -> Optional[dict]:
    try:
        data = json.loads((path or VENDORED_PATH).read_text(encoding="utf-8"))
        slice_ = data["slice"]
        if not all(_pos_int(slice_[a]) for a in _AXES):
            return None
        bar = data.get("whole_backlog_token_bar", PINNED_WHOLE_BACKLOG_BAR)
        if not _pos_int(bar):
            return None
        return {"slice": slice_, "bar": bar}
    except (OSError, ValueError, KeyError, TypeError):
        return None


def load(ceremony: str, path: Optional[Path] = None) -> RunBudget:
    """The run budget for `ceremony`; `KeyError` for an unlisted ceremony."""
    if ceremony not in CEREMONIES:
        raise KeyError(ceremony)
    data = _read(path)
    if data is None:
        return PINNED_DEFAULT
    s = data["slice"]
    return RunBudget(s["agents"], s["tokens"], s["minutes"])


def whole_backlog_bar(path: Optional[Path] = None) -> int:
    """Token bar for covering an entire backlog across runs."""
    data = _read(path)
    return PINNED_WHOLE_BACKLOG_BAR if data is None else data["bar"]


def _cap(budget: RunBudget) -> dict:
    return {a: getattr(budget, a) for a in _AXES}


def over_cap_fixed(budget: RunBudget, fixed: UnitCost) -> bool:
    """True when the fixed cost alone exceeds any axis of the cap."""
    return any(getattr(fixed, a) > getattr(budget, a) for a in _AXES)


def fit_units(budget: RunBudget, fixed: UnitCost, per_unit: UnitCost, wanted: int) -> int:
    """Largest n <= wanted with fixed + n*per_unit within every axis.

    Minimum 1 when wanted >= 1: a run always makes progress, and a fixed cost
    over the cap is reported through `over_cap_fixed`, not refused. A zero
    per-unit axis does not bind.
    """
    if wanted <= 0:
        return 0
    n = wanted
    for a in _AXES:
        unit = getattr(per_unit, a)
        if unit > 0:
            room = getattr(budget, a) - getattr(fixed, a)
            n = min(n, max(room, 0) // unit)
    return max(n, 1)


def budget_report(
    budget: RunBudget, projected: UnitCost, over_cap_fixed_flag: Optional[bool] = None
) -> dict:
    """`{cap, projected, within}` per axis; adds `over_cap_fixed` when passed."""
    report = {
        "cap": _cap(budget),
        "projected": {a: getattr(projected, a) for a in _AXES},
        "within": {a: getattr(projected, a) <= getattr(budget, a) for a in _AXES},
    }
    if over_cap_fixed_flag is not None:
        report["over_cap_fixed"] = over_cap_fixed_flag
    return report

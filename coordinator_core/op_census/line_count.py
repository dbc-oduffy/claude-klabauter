"""coordinator_core.op_census.line_count — line-count axis with a frozen
high-water ratchet.

Purpose: aggregate per-module line counts (from `ModuleSummary`, C1) into a
corpus-wide distribution, and ratchet the aggregate so future corpus growth
cannot silently pass unnoticed. Never a second file read or a second AST
walk — `line_count` is already computed inside `module_summary
._compute_module_summary` from the same in-memory text `sites_in_source`
uses; this module only consumes the resulting `ModuleSummary` objects.

Cheap and already measured (2026-08-21, C5 dispatch brief, 64ms over this
repo's `coordinator_core/` tree): 1,135 modules, 580,560 total lines, 72
modules over the 1,500-line per-module bar, largest
`pickup_assemble/__init__.py` at 10,072 lines and `bash_guards/
dispatch_checks.py` at 8,439 lines — the latter on every Bash call's hot
path. `FROZEN_HIGH_WATER_*` below freezes that measurement; `ratchet_check`
REFUSES (raises `RatchetError`), never degrades or silently passes, when a
later corpus measurement exceeds it on module count, total lines, or
over-bar count.

Negative-spec — do NOT "fix" while reading this module:
    - This module does NOT read the filesystem, parse a module, or call
      `module_summary.summarize_paths` itself — it is a pure aggregation
      over caller-supplied `ModuleSummary` objects. Assembling the corpus
      (which paths, warm vs. cold) is C1's / the census op's (C6)
      concern, not this module's.
    - The ratchet compares the AGGREGATE (module count / total lines /
      over-bar count) against the frozen high-water — never a per-module
      figure against a per-module frozen baseline. A corpus that shrinks
      one module and grows another still trips the ratchet via the
      aggregate, by design.
    - `PER_MODULE_LINE_BAR` (1,500 lines) is a per-module distribution
      bucket for the over-bar/under-bar three-state disposition report —
      it is NOT itself a ratchet target and bumping it is a distribution
      policy change, not a corpus-growth bump.
    - Bumping `FROZEN_HIGH_WATER_*` is a deliberate, reasoned edit to this
      module's own constants — this module has no ledger/watermark-file
      indirection (unlike `claude_md_budget.py`'s ratchet); the frozen
      figures live here, in code, reviewed like any other change.
    - This module does not check corpus root/identity — refusing when a
      ratchet is evaluated against a DIFFERENT corpus than the one the
      frozen figures were taken over is the census op's (C6) concern,
      which carries the corpus root/module count alongside this axis's
      output.

Spec backlink: state/dispatch-briefs/2026-08-21-the-census-that-cannot-miss-an-op/C5.md
"""

from __future__ import annotations

import dataclasses
from typing import Dict, Iterable

from coordinator_core.op_census.module_summary import ModuleSummary

__all__ = [
    "PER_MODULE_LINE_BAR",
    "FROZEN_HIGH_WATER_MODULES",
    "FROZEN_HIGH_WATER_LINES",
    "FROZEN_HIGH_WATER_OVER_BAR",
    "LineCountDistribution",
    "RatchetError",
    "RatchetOutcome",
    "compute_distribution",
    "evaluate_ratchet",
    "ratchet_check",
]

#: Per-module reporting threshold for the over-bar/under-bar three-state
#: disposition — 72 of 1,135 modules exceed this today (see module
#: docstring). NOT the ratchet target; see negative-spec above.
PER_MODULE_LINE_BAR: int = 1500

#: Frozen high-water, measured 2026-08-21 (C5 dispatch brief) against this
#: repo's own `coordinator_core/` tree. Bump only with a deliberate,
#: reasoned edit — never silently, and never to "make a failing ratchet
#: pass" without recording why the growth is intended.
FROZEN_HIGH_WATER_MODULES: int = 1135
FROZEN_HIGH_WATER_LINES: int = 580560
FROZEN_HIGH_WATER_OVER_BAR: int = 72


@dataclasses.dataclass(frozen=True)
class LineCountDistribution:
    """The real distribution over a corpus's `ModuleSummary` set."""

    module_count: int
    total_lines: int
    over_bar_count: int
    over_bar_modules: Dict[str, int]

    def to_dict(self) -> dict:
        return {
            "module_count": self.module_count,
            "total_lines": self.total_lines,
            "over_bar_count": self.over_bar_count,
            "over_bar_modules": dict(self.over_bar_modules),
        }


class RatchetError(Exception):
    """Raised when a corpus's line-count distribution has grown past its
    frozen high-water on module count, total lines, or over-bar count — a
    REFUSAL, never a silent pass or a degrade (see AC: "the ratchet
    assertions refuse (not degrade, not skip)")."""


@dataclasses.dataclass(frozen=True)
class RatchetOutcome:
    """The ratchet's verdict as DATA, not control flow — staff-eng Finding 5:
    a measurement instrument must still produce a measurement when its
    verdict is "over". `tripped` is the aggregate boolean (any of the three
    quantities over its frozen high-water); each per-quantity dict below
    carries both the `frozen` bound and the `measured` value so a consumer
    can see BY HOW MUCH, not only whether. `evaluate_ratchet` never raises —
    `ratchet_check` (below) is the raising sibling built on top of it for
    callers that still want the refusal as control flow (e.g. `strict=True`
    census callers)."""

    tripped: bool
    module_count: Dict[str, int]
    total_lines: Dict[str, int]
    over_bar_count: Dict[str, int]

    def to_dict(self) -> dict:
        return {
            "tripped": self.tripped,
            "module_count": dict(self.module_count),
            "total_lines": dict(self.total_lines),
            "over_bar_count": dict(self.over_bar_count),
        }


def compute_distribution(summaries: Iterable[ModuleSummary]) -> LineCountDistribution:
    """Aggregates `summaries` (as produced by `module_summary.summarize_paths`)
    into a `LineCountDistribution`. A `ModuleSummary` with `parse_error` set
    still contributes its best-effort `line_count` (per `module_summary`'s
    own negative-spec: line count is derived from raw text, independent of
    whether the AST parse succeeded) — this function does not special-case
    parse errors."""
    module_count = 0
    total_lines = 0
    over_bar_modules: Dict[str, int] = {}
    for summary in summaries:
        module_count += 1
        total_lines += summary.line_count
        if summary.line_count > PER_MODULE_LINE_BAR:
            over_bar_modules[summary.path] = summary.line_count
    return LineCountDistribution(
        module_count=module_count,
        total_lines=total_lines,
        over_bar_count=len(over_bar_modules),
        over_bar_modules=over_bar_modules,
    )


def evaluate_ratchet(distribution: LineCountDistribution) -> RatchetOutcome:
    """Measures `distribution` against the frozen high-water on all three
    ratcheted quantities and returns the verdict as a `RatchetOutcome` —
    NEVER raises (staff-eng Finding 5: separating measurement from verdict
    so a caller can always obtain a report, even when the ratchet has
    tripped). `ratchet_check` is the raising sibling built on top of this
    for callers that still want the refusal as control flow."""
    return RatchetOutcome(
        tripped=(
            distribution.module_count > FROZEN_HIGH_WATER_MODULES
            or distribution.total_lines > FROZEN_HIGH_WATER_LINES
            or distribution.over_bar_count > FROZEN_HIGH_WATER_OVER_BAR
        ),
        module_count={
            "frozen": FROZEN_HIGH_WATER_MODULES,
            "measured": distribution.module_count,
        },
        total_lines={
            "frozen": FROZEN_HIGH_WATER_LINES,
            "measured": distribution.total_lines,
        },
        over_bar_count={
            "frozen": FROZEN_HIGH_WATER_OVER_BAR,
            "measured": distribution.over_bar_count,
        },
    )


def ratchet_check(distribution: LineCountDistribution) -> None:
    """Refuses (raises `RatchetError`) when `distribution` has grown past
    the frozen high-water on module count, total lines, or over-bar count.
    A shrink or hold on all three measures is always allowed with no bump
    required — mirrors the "shrinks or holds without a reasoned bump"
    shape of `claude_md_budget.ratchet_check`, without that module's
    ledger-file indirection (see negative-spec above). Built on
    `evaluate_ratchet` — the measurement; this function is the raising
    control-flow wrapper around it, kept for callers (e.g. `census(...,
    strict=True)`) that still want the refusal to happen inline."""
    outcome = evaluate_ratchet(distribution)
    if not outcome.tripped:
        return

    violations = []
    if distribution.module_count > FROZEN_HIGH_WATER_MODULES:
        violations.append(
            f"module_count {distribution.module_count} > frozen high-water "
            f"{FROZEN_HIGH_WATER_MODULES}"
        )
    if distribution.total_lines > FROZEN_HIGH_WATER_LINES:
        violations.append(
            f"total_lines {distribution.total_lines} > frozen high-water "
            f"{FROZEN_HIGH_WATER_LINES}"
        )
    if distribution.over_bar_count > FROZEN_HIGH_WATER_OVER_BAR:
        violations.append(
            f"over_bar_count {distribution.over_bar_count} > frozen high-water "
            f"{FROZEN_HIGH_WATER_OVER_BAR}"
        )
    raise RatchetError(
        "Refused: the corpus has grown past its frozen line-count "
        "high-water on " + "; ".join(violations) + ". Bump the frozen "
        "FROZEN_HIGH_WATER_* constants in coordinator_core/op_census/"
        "line_count.py deliberately, with a stated reason, if this "
        "growth is intended -- this ratchet does not degrade or skip."
    )

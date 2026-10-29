"""Pure interface for the plan-path post-execution tail: review groups, quiescence, verify budget.

Stdlib only; no coordinator_core imports. Callers: execute_review (group review mode) and
dispatch_emit.emit (compose_script wiring).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Mapping, Sequence

# Wall-clock hang bound on one verify agent call the run cannot cancel; not an engine-op budget.
# Derivation: under half the 632s XL r34 verify, above a scoped dispatch_emit test run. The closing
# verify runs two bounded calls in series (test leg, then judge), so its worst case is
# 2 x VERIFY_WALL_BUDGET_S. Coupling: must exceed hung_test_budget.INVOCATION_BUDGET_S (180) of the
# sibling cause3 plan, else a legitimate invocation is cut short and scored as a failure. Enforced
# only by test_budget_exceeds_invocation_budget_when_importable, which skips until that module lands.
VERIFY_WALL_BUDGET_S: int = 300


@dataclass(frozen=True)
class ReviewGroup:
    """One emit wave's review unit.

    `paths` is the POSIX-sorted union of the wave rows' declared writes; `quiescence_rows` is every
    row in the whole plan (later waves included) that writes any of `paths`, and the group's review
    may start only once all of them have landed.
    """

    index: int
    rows: tuple[str, ...]
    paths: tuple[str, ...]
    quiescence_rows: tuple[str, ...]


def _posix(path: str) -> str:
    return path.replace("\\", "/")


def review_groups(
    waves: Sequence[Sequence[str]], writes: Mapping[str, Sequence[str]]
) -> list[ReviewGroup]:
    """One group per non-empty wave, in wave order; `index` is the position among emitted groups."""
    row_paths: dict[str, frozenset[str]] = {
        row: frozenset(_posix(p) for p in paths) for row, paths in writes.items()
    }
    order: list[str] = []
    for wave in waves:
        for row in wave:
            if row not in order:
                order.append(row)
    for row in row_paths:
        if row not in order:
            order.append(row)

    groups: list[ReviewGroup] = []
    for wave in waves:
        if not wave:
            continue
        rows = tuple(wave)
        paths = frozenset().union(*(row_paths.get(r, frozenset()) for r in rows))
        quiescence = tuple(r for r in order if row_paths.get(r, frozenset()) & paths)
        quiescence = tuple(dict.fromkeys((*rows, *quiescence))) if paths else rows
        groups.append(
            ReviewGroup(
                index=len(groups),
                rows=rows,
                paths=tuple(sorted(paths)),
                quiescence_rows=quiescence,
            )
        )
    return groups


def bounded_js(call_expr: str, budget_s: int, label: str) -> str:
    """JS expression racing `call_expr` against a timer; never rejects, never cancels the agent.
    The timer is cleared when the race settles; `call_expr` must be a side-effect-free identifier."""
    reason = json.dumps(f"verify budget exceeded ({budget_s}s): {label}")
    return (
        f"((_t) => Promise.race([{call_expr}, new Promise((_res) => {{ _t = setTimeout(() => "
        f"_res({{status: 'indeterminate', reason: {reason}}}), {budget_s * 1000}); }})])"
        f".finally(() => clearTimeout(_t)))()"
    )


def group_slice_prefix(sidecar_key: str, index: int, base: str) -> str:
    """The one spelling of a group's review.prep slice prefix and its reverify calls. `base` is
    the diff base the frozen diff holds (first 12 chars), so a re-emit over another base never
    reuses an earlier frozen diff; the same base spells the same prefix."""
    return f"{sidecar_key}.g{index}-{base[:12]}"

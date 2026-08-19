"""
coordinator_core.telemetry.composition_record -- durable per-composition span adapter.

Chunk C1 (docs/plans/2026-08-18-arm-the-composition-budget.md): wires
`coordinator_core.composition_budget.CompositionBudget`'s injectable `on_count`
callback to a durable sink. C1 shipped this as a PURE RECORDER with both fleet
ceilings (`composition_budget.FLEET_AGGREGATE_ELAPSED_BUDGET`,
`composition_budget.FLEET_MAX_INVOCATIONS`) at `None`; C5 armed them from this
recorder's own data (1200.0s / 110). The recording path is unchanged by that --
a budget still observes every composition; it now also bounds one.

WHY THIS MODULE, NOT `composition_budget.py` ITSELF (§ that module's own
DEPENDENCY-FREE LEAF section): `composition_budget.py` imports only
`__future__`/`dataclasses`/`os`/`time`/`typing` and deliberately does not import
`coordinator_core.telemetry.op_latency` or anything else intra-repo -- wiring
`on_count` to `op_latency` FROM INSIDE `composition_budget.py` would itself be
the intra-repo edge that section forbids. This module is the "small adapter"
that section names explicitly: it lives OUTSIDE the leaf and calls into
`op_latency` from ITS OWN module, keeping the leaf import-free.

ACCUMULATE, THEN FLUSH (mechanism note, because `on_count`'s actual shape does
not support a naive "one record per composition, written from on_count"
reading): `CompositionBudget.on_count` is `Callable[[str, int], None]`, fired
from `record_invocation()` once PER INVOCATION (i.e. per dispatched directive,
not once per composition), carrying only `(unit, running_total)` -- no
elapsed, no composition id, no composition name, and it is never called from
`check()`/`advisory_check()`, so there is no callback at the post-mutation
boundary either. `_Accumulator.bound_on_count` below therefore only keeps the
running invocation count fresh in memory (cheap, no I/O -- respecting
`composition_budget.py`'s own "NO SPAWN TO CHECK THE BUDGET" constraint);
`flush_composition_record()` is the single point that turns that in-memory
state into one durable row, called explicitly by the caller once per
composition, from a `finally` (see this module's own docstrings below for the
call shape each of C2/C3's 8 `apply.py` callers uses).

CHICKEN-AND-EGG, resolved by ASSIGN-AFTER-CONSTRUCTION: `on_count` must be
supplied to the `CompositionBudget` constructor, but the accumulator closure
needs the budget instance (to read `elapsed_secs()`/`invocation_count`/
`composition_id` at flush time) -- so `make_fleet_budget()` below constructs
the budget with `on_count=None` first, then assigns
`budget.on_count = accumulator.bound_on_count` once both the instance and the
accumulator exist, before returning. `CompositionBudget` is a plain
(non-frozen) dataclass, so this assignment is a normal attribute set, not a
workaround. Do not replace this with a mutable-cell indirection -- assign-
after-construction is simpler and is the ordering this module commits to.

PER-CALL FACTORY, NEVER A MODULE-LEVEL SINGLETON: `composition_budget.py`'s
own WARM-ENGINE INTERACTION note is explicit that a shared instance under a
warm engine conflates two compositions' invocation counts. `make_fleet_budget`
returns a fresh `CompositionBudget` (and a fresh accumulator closed over it)
on every call -- there is no module-level `CompositionBudget`/accumulator
state anywhere in this file.

Spec backlink: docs/plans/2026-08-18-arm-the-composition-budget.md § C1
               state/subagent-share/3d70362f-6845-47ec-901e-3b9f0b412836/PINNED-INTERFACE.md
               coordinator_core/composition_budget.py
               coordinator_core/telemetry/op_latency.py
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from typing import Optional

from coordinator_core import composition_budget as _composition_budget
from coordinator_core.composition_budget import SKIP_AND_SURFACE, CompositionBudget
from coordinator_core.contract.apply_base import resolve_explicit_session_id
from coordinator_core.telemetry.op_latency import record_composition_span

#: Every outcome `flush_composition_record` accepts -- see that function and
#: the pinned interface doc (§ "Call shape C2 and C3 both write") for the
#: three-way split C4 partitions on: healthy success, a run that mutated
#: something before failing, and a run that failed before any mutation.
VALID_OUTCOMES = frozenset({"success", "partial_mutation", "directive_failed"})


class _Accumulator:
    """Per-composition in-memory state `on_count` updates and `flush` reads.

    One instance per `make_fleet_budget()` call, closed over the
    `CompositionBudget` it is paired with -- never shared across calls (see
    module docstring, PER-CALL FACTORY).
    """

    def __init__(self, budget: CompositionBudget) -> None:
        self._budget = budget

    def bound_on_count(self, unit: str, running_total: int) -> None:
        """The `on_count` callback wired to `budget.on_count`. Pure in-memory
        bookkeeping -- `running_total` is already `budget.invocation_count`
        (the dataclass's own counter), so nothing further needs storing here;
        this method exists as the assign-after-construction wiring point and
        the documented shape of `CompositionBudget.on_count`, not because it
        tracks state `budget` does not already hold itself."""
        del unit, running_total  # budget.invocation_count is the source of truth at flush


def make_fleet_budget(composition_name: str) -> CompositionBudget:
    """Fresh `CompositionBudget` per call (never a module-level singleton --
    see module docstring, PER-CALL FACTORY).

    - `composition_id` is minted via `composition_budget.new_composition_id()`
      (composer-local; `identity_resolver` stays unwired here -- reserved for
      the PM per `composition_budget.py`'s own docstring).
    - `aggregate_elapsed_budget` / `max_invocations` read from the two fleet
      constants in `composition_budget.py`, armed at C5. This function is
      their sole reader, which is what arms all 8 compositions across both
      lineages at one posture.
    - `disposition=SKIP_AND_SURFACE` -- the module default, NOT
      parameterised here; a test wanting fail-loud constructs its own
      `CompositionBudget` directly.
    - `on_count` is wired by assign-after-construction (module docstring,
      CHICKEN-AND-EGG) to a fresh `_Accumulator` closed over this instance.

    `composition_name` is NOT stored on `CompositionBudget` itself (that
    dataclass has no such field) -- it is threaded through to
    `flush_composition_record` by the caller's own local variable, per the
    pinned call shape in
    `state/subagent-share/.../PINNED-INTERFACE.md`.
    """
    budget = CompositionBudget(
        composition_id=_composition_budget.new_composition_id(),
        aggregate_elapsed_budget=_composition_budget.FLEET_AGGREGATE_ELAPSED_BUDGET,
        max_invocations=_composition_budget.FLEET_MAX_INVOCATIONS,
        disposition=SKIP_AND_SURFACE,
        on_count=None,
    )
    accumulator = _Accumulator(budget)
    budget.on_count = accumulator.bound_on_count
    budget._composition_name = composition_name  # type: ignore[attr-defined]
    budget._composition_t_start = time.time()  # type: ignore[attr-defined]
    return budget


def flush_composition_record(
    budget: CompositionBudget,
    outcome: str,
    *,
    repo_root: Optional[Path] = None,
    sid: Optional[str] = None,
) -> None:
    """Never-raising public flush -- the one all 8 `apply.py` call sites use.

    NEVER RAISES, and that is load-bearing rather than defensive habit. The
    pinned call shape puts this call in a `finally`, so an exception escaping
    here during an unwinding directive loop would REPLACE the composition's
    own exception with a telemetry error -- a recorder deciding a ceremony's
    outcome. `contract/apply_base.py :: _budget_call` and
    `ceremony_common/apply_halt.py :: _budget_call` supply exactly this
    containment for the budget CHECK calls, but neither wraps the flush, so
    the containment has to live here. Same contract `op_latency`'s own
    writers hold ("Never breaks dispatch"); `record_composition_span` is
    already never-raising, and this wrapper extends that to the argument
    validation in `_flush_or_raise` below.

    A rejected flush costs one composition's row and one stderr line -- never
    the run. Tests target `_flush_or_raise` for the validation contract.
    """
    try:
        _flush_or_raise(budget, outcome, repo_root=repo_root, sid=sid)
    except Exception as exc:  # noqa: BLE001 -- see docstring; containment is the point
        print(f"composition-record: flush skipped ({exc})", file=sys.stderr)


def _flush_or_raise(
    budget: CompositionBudget,
    outcome: str,
    *,
    repo_root: Optional[Path] = None,
    sid: Optional[str] = None,
) -> None:
    """Write ONE `kind="composition"` row for this composition, via
    `op_latency.record_composition_span`. Call once, from a `finally`, per
    composition -- whatever the run's outcome (module docstring, ACCUMULATE
    THEN FLUSH) -- so a partial-mutation or directive-failure exit still
    produces a record instead of silently dropping from C4's population.

    `outcome` must be one of `VALID_OUTCOMES` ("success" | "partial_mutation"
    | "directive_failed"); a caller passing anything else gets a `ValueError`
    at flush time rather than a silently mis-tagged row.

    The composition NAME is read off `budget._composition_name`, set by
    `make_fleet_budget()` -- a caller that built its own `CompositionBudget`
    directly (bypassing the factory) must set that attribute itself before
    calling this function, or pass a budget from `make_fleet_budget()`.

    The row's wall-clock `t_start` is read off `budget._composition_t_start`,
    also set by `make_fleet_budget()`. Unlike the name it is DERIVED, not
    demanded, when absent: `time.time() - elapsed_secs` reconstructs it to
    within the flush's own duration, so a directly-constructed budget still
    produces a dateable row rather than none. `CompositionBudget.elapsed_secs`
    runs on `time.monotonic`, which is why the date cannot come from the
    budget's own clock (§ `op_latency.record_composition_span`).

    `repo_root` defaults to `Path(os.getcwd())` when not supplied -- the
    pinned two-argument call shape (`flush_composition_record(budget,
    outcome)`, § PINNED-INTERFACE.md) gives callers no repo_root to thread
    through, and every one of the 8 `apply.py` call sites runs with the
    target repo as its process cwd (same assumption
    `coordinator_core.lifecycle.git_common_dir`'s callers make elsewhere on
    this path). Tests inject `repo_root` explicitly to avoid depending on
    the test runner's own cwd.

    `sid` resolves from the environment on the same argument and for the same
    reason: the pinned two-argument call shape threads no session id either,
    so leaving it to the caller wrote `"sid": null` on every composition row
    the fleet produced while ordinary op rows carried 416 distinct real ids.
    That is not cosmetic -- `sid` plus `pid` is the only key joining a
    composition to the child ops it ran, so its absence makes C4's falsifiable
    `ceremony.scoped_git_commit` cross-check (docs/plans/
    2026-08-18-arm-the-composition-budget.md § C4) undecidable and leaves
    per-directive attribution with nothing but a recycled Windows pid. Same
    class of defect as the missing `t_start` fixed earlier in this function,
    and found the same way -- by reading the sink rather than the writer.

    Resolution reuses `contract/apply_base.py :: resolve_explicit_session_id`
    rather than re-deriving the chain here. That helper already owns
    `SESSION_ENV_READ_ORDER` (`COORDINATOR_SESSION_ID` > `CLAUDE_SESSION_ID` >
    `CLAUDE_CODE_SESSION_ID`) and is already imported by 5 of the 8 pinned
    `flush_composition_record` call sites, so this adds no import edge the
    lineage did not already carry -- both `apply_halt` ceremonies import
    `apply_base` too, and a cold import of either module measures the same.
    An earlier revision hand-copied the chain and cited
    `ops/handoff_correct_body.py` as precedent for duplicating it; that module
    in fact IMPORTS its copy, so the cited precedent argued the opposite of
    what it was cited for.

    NEGATIVE-SPEC: an explicitly passed `sid=None` is NOT preserved as null.
    A caller wanting an unattributed row must not call this function. Tests
    pass an explicit sid; nothing in the repo depends on a null sid column.
    """
    if outcome not in VALID_OUTCOMES:
        raise ValueError(
            f"flush_composition_record: outcome must be one of {sorted(VALID_OUTCOMES)}, "
            f"got {outcome!r}"
        )

    name = getattr(budget, "_composition_name", None)
    if not name:
        raise ValueError(
            "flush_composition_record: budget has no composition name -- "
            "construct it via make_fleet_budget(composition_name) rather than "
            "CompositionBudget(...) directly, or set budget._composition_name yourself"
        )

    elapsed = budget.elapsed_secs()
    t_start = getattr(budget, "_composition_t_start", None)
    if not isinstance(t_start, (int, float)):
        t_start = time.time() - elapsed

    record_composition_span(
        composition_id=budget.composition_id,
        name=name,
        invocation_count=budget.invocation_count,
        elapsed_secs=elapsed,
        outcome=outcome,
        t_start=float(t_start),
        repo_root=repo_root if repo_root is not None else Path(os.getcwd()),
        sid=resolve_explicit_session_id(sid),
    )

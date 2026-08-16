"""
coordinator_core.composition_budget — cross-process composition invocation + elapsed budget.

EXTRACTION VS NEW WORK (docs/plans/2026-08-15-composition-invocation-budgets.md chunk C9):
`coordinator_core/percolate/engine.py::run_entrypoint_gate` already carries a WORKING
aggregate-budget shape (`_budget_ok() = time.monotonic() - start_time <= aggregate_budget`,
skip-and-surface via its own `budget_exceeded[]` list) -- but it is elapsed-time-only,
single-process, single-run. `CompositionBudget.check()` below extracts exactly that
timing half (same monotonic-delta comparison, same "checked before dispatch, not merely
before report" contract). Everything else here -- the invocation counter, the injectable
cross-process identity channel, and the fail-loud disposition -- is NEW WORK, not a
percolate extraction. percolate remains this primitive's first caller and its
skip-and-surface disposition is unchanged by this module: `disposition="skip-and-surface"`
(the default) reproduces percolate's existing behaviour exactly; `disposition="fail-loud"`
is additive, for AC9's caller (chunk C10), and does not alter percolate's own call sites.

WHY ELAPSED TIME IS NOT OPTIONAL (docs/research/2026-08-15-wsc-close-cost-attribution.md,
chunk C1, gates this chunk): on a real PARTITION-MANDATORY `/workstream-complete` close,
ONE engine subprocess is 54-57% of wall clock (~150s) while its own INVOCATION COUNT on
that path is 1 -- the single most expensive term in the composition. A counter that only
counted invocations would have reported "1" for it. Git is the other real cost (43-45%
across ~2,315 spawns at 48-56ms each) and IS invocation-count-shaped. So this primitive
tracks both dimensions -- `aggregate_elapsed_budget` (wall-clock) and `max_invocations`
(count) -- as independent, either-or-both-settable ceilings, not one blended number.

DEPENDENCY-FREE LEAF (hard constraint, § chunk C9 brief): this module imports only
`__future__`/`dataclasses`/`time`/`typing` -- the same bar `_hook_envelope.py` sets at
the package root (17.9ms is the cost of pulling in a heavier package `__init__`; a
budget check that can sit on the dispatch path must cost nothing close to that). It does
NOT import `coordinator_core.telemetry.op_latency`, `coordinator_core.ipc`, or anything
else intra-repo. This is deliberate and is why "prefer extending op_latency's sink" (see
below) is satisfied by an INJECTION POINT here (`on_count`), not an import here: the
caller that wants a durable cross-process record wires `on_count` to a small adapter that
calls into `op_latency.record_op_latency`/a new op_latency helper from ITS OWN module
(e.g. percolate/engine.py or a future C10 call site), keeping that intra-repo edge out of
this leaf.

NO SPAWN TO CHECK THE BUDGET (hard constraint): `check()`/`record_invocation()` are pure
in-memory operations against a monotonic clock and an int counter -- no subprocess, no
file I/O, no import deferred to call time. Adding a spawn to check a budget would worsen
exactly the fork-less-platform problem (§ chunk C1: engine cold start dominates) this
primitive exists to bound.

PRIOR-ART CHECK (cite per chunk C9 brief, "so a later session does not re-litigate the
choice"): no ruling was found freezing `coordinator_core.telemetry.op_latency`'s record
shape -- unlike `guard_advisory_counter`, whose shape IS frozen by its own docstring. That
absence is why a caller wiring `on_count` to extend op_latency's sink (rather than minting
a third counter dialect) is the right default, not merely an option; this module does not
implement that wiring itself (see "dependency-free leaf" above), only makes it possible.

RESERVED FOR THE PM -- NOT DECIDED HERE (§ chunk C9 brief): how a composition identity
propagates across processes. The census named four shapes (env var, per-composition file,
composer-local/no-propagation, and -- unenumerated here but structurally identical to
"file" -- a durable sink keyed by a caller-minted id) with tradeoffs and made no
recommendation; C1's measurement did not settle it either (it measured ONE process's own
buckets, not cross-process identity). This module does not pick one: `identity_resolver`
is an injectable `Callable[[], str] | None` (default `None`, meaning "no propagation --
each `CompositionBudget` instance is composer-local, scoped to whatever process
constructs it"). A caller wanting cross-process identity supplies a resolver reading an
env var, a per-composition file, or anything else; `new_composition_id()` below is offered
only as a convenience default id-shape (pid + monotonic-ns, same construction as
`op_latency.new_correlation_id`'s pid + perf_counter_ns, minus the uuid4 cost for the same
hot-path reason), not a ruling on the channel.

THIRD SEAM BYPASS -- DECLARED KNOWN HOLE, NOT COVERED (census §9.1; chunk C9 brief names
it explicitly): in-process op-to-op re-entry via `coordinator_core.ipc.get_op_handler(name)`
followed by a direct `await` (site: `promote_shipped_in_flight_stubs.py`'s
`get_op_handler("handoff.stamp")` call; recurs in `post_commit_tail.py::
_run_deliverable_cascade` and `handoff_reconcile.py::_reconcile_ancestor_chain`) never
passes through `ipc.dispatch_message`, the seam any counter wired at dispatch time would
sit on. This primitive is a plain object with `check()`/`record_invocation()` methods a
caller invokes explicitly -- it has no seam of its own to hook, by the dependency-free-leaf
constraint above (hooking `dispatch_message` or `get_op_handler` would require importing
`coordinator_core.ipc`, which this module deliberately does not). A composition that
re-enters via `get_op_handler` and does not ALSO call `record_invocation()`/`check()` at
that call site is invisible to whatever budget the composition is using, exactly as it is
invisible to git spawn counts today. Covering it structurally is future work at the
`ipc.py`/`cli_entry.py` layer, not this primitive -- declared here per the brief's "cover
it or declare it a known hole; silence is not an option."

WARM-ENGINE INTERACTION (docs/research/spike-verdicts/2026-08-15-warm-engine-os-exit-and-
lock-reclaim.md): every counting/timing decision above assumes spawn-per-call, where one
`CompositionBudget` instance's lifetime is bounded by one process's lifetime and an
in-process counter cannot observe a SIBLING process's invocations at all -- cross-process
visibility depends entirely on the caller's `identity_resolver`/`on_count` wiring reaching
a durable, cross-process-readable sink (op_latency's JSONL, or equivalent). If the engine
moves toward warm reuse (spike verdict: viable-with-conditions, not yet routed), a single
warm process could dispatch MANY compositions' invocations through the SAME in-process
counter unless each dispatch constructs its own fresh `CompositionBudget` scoped by
`identity_resolver` -- an in-process-only counter's blindness changes from "blind across
processes" to "blind across compositions sharing one process" unless the caller re-scopes
per composition. This module's statelessness-per-instance (no module-level counter state)
is what keeps that re-scoping the caller's responsibility rather than a global that would
silently conflate two compositions' counts under warmth.

Disposition, both expressible (percolate's caller needs skip-and-surface; chunk C10's
caller needs fail-loud -- neither disposition changes the other's callers):
    "skip-and-surface" (default) -- `check()` returns `False` on breach; caller decides
        what "skip" means for its own unit of work (percolate: skip the entrypoint, add
        it to its own `budget_exceeded[]`). Never raises.
    "fail-loud" -- `check()` raises `BudgetBreach` on breach, carrying the composition id,
        the unit name, the count, and the elapsed seconds, for a caller (chunk C10) that
        wants a register-conforming breach message asserted by a test on its shape.

Mid-directive WARN-ONLY advisory (chunk C10 pairing, § that chunk's "boundary-vs-advisory
split"): `advisory_check()` NEVER raises regardless of `disposition` and never mutates
`disposition`'s own count/breach bookkeeping beyond recording the breach for
`breached_units` -- it exists so a caller mid-directive can observe "has this composition's
clock already blown its budget" without adopting fail-loud's abort semantics inline. C10
wires boundary checks (`check()`, disposition-respecting) and mid-directive advisories
(`advisory_check()`, always non-raising) as two distinct call shapes against the SAME
`CompositionBudget` instance's clock and counter -- this primitive supplies both without
picking which one a given call site uses.

Spec backlink: docs/plans/2026-08-15-composition-invocation-budgets.md § C9
               docs/research/2026-08-15-wsc-close-cost-attribution.md
               docs/research/spike-verdicts/2026-08-15-warm-engine-os-exit-and-lock-reclaim.md
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

#: Disposition values `CompositionBudget.disposition` accepts. Not an enum -- a plain
#: two-member closed string set is enough here and keeps this leaf import-free of `enum`
#: quirks across the pre-existing callers' Python versions; `__post_init__` validates
#: membership so a typo fails at construction, not at the first `check()` call.
SKIP_AND_SURFACE = "skip-and-surface"
FAIL_LOUD = "fail-loud"
_VALID_DISPOSITIONS = frozenset({SKIP_AND_SURFACE, FAIL_LOUD})

#: Convenience env var name a caller's `identity_resolver` MAY read (§ module docstring,
#: "RESERVED FOR THE PM" -- this module does not read it itself; `identity_from_env()`
#: below is an opt-in helper, never invoked implicitly).
DEFAULT_COMPOSITION_ID_ENV = "COORDINATOR_COMPOSITION_ID"


class BudgetBreach(Exception):
    """Raised by `CompositionBudget.check()` when `disposition="fail-loud"` and a breach fires.

    Register-conforming message (§ chunk C9 brief AC9 pairing): one fact, stated once --
    the composition id, which ceiling breached (`unit`: "elapsed" or "invocations", or a
    caller-supplied fan-out unit name via `record_invocation(unit=...)`), and the measured
    count/elapsed at breach time. No self-legitimacy, no apology, no override key (§
    docs/wiki/guard-messaging.md § Register, cited here because this exception's `str()`
    is the message a ladder caller (chunk C10) surfaces verbatim).
    """

    def __init__(
        self,
        *,
        composition_id: str,
        unit: str,
        invocation_count: int,
        elapsed_secs: float,
        aggregate_elapsed_budget: "float | None",
        max_invocations: "int | None",
    ) -> None:
        self.composition_id = composition_id
        self.unit = unit
        self.invocation_count = invocation_count
        self.elapsed_secs = elapsed_secs
        self.aggregate_elapsed_budget = aggregate_elapsed_budget
        self.max_invocations = max_invocations
        message = (
            f"composition budget breach: composition={composition_id!r} unit={unit!r} "
            f"invocations={invocation_count} elapsed={elapsed_secs:.3f}s "
            f"(elapsed_budget={aggregate_elapsed_budget!r} max_invocations={max_invocations!r})"
        )
        super().__init__(message)


def new_composition_id() -> str:
    """Mint a convenience composition id: `f"{pid}-{perf_counter_ns()}"`.

    Same construction as `op_latency.new_correlation_id` (pid + monotonic nanosecond
    counter, deliberately not `uuid` -- see that module's "Cheap" negative-spec, which
    applies identically to a primitive that may sit on the same dispatch path). This is
    ONE convenience shape for a caller with no cross-process propagation need (composer-
    local scoping, § module docstring "RESERVED FOR THE PM") -- not a default any
    `CompositionBudget` picks for itself; `composition_id` is a required constructor arg.
    """
    return f"{os.getpid()}-{time.perf_counter_ns()}"


def identity_from_env(var_name: str = DEFAULT_COMPOSITION_ID_ENV) -> Optional[str]:
    """One injectable `identity_resolver` shape: read a composition id from an env var.

    Returns `None` if the var is unset or empty -- a caller wiring this as
    `identity_resolver` should fall back to `new_composition_id()` on `None` (this
    function does not fall back itself, so a caller can distinguish "propagated" from
    "minted fresh" if it cares to). Offered, not mandated -- § module docstring's
    "RESERVED FOR THE PM" applies to whether this channel is the right one at all.
    """
    value = os.environ.get(var_name)
    return value if value else None


def identity_from_file(path: "str | os.PathLike") -> Optional[str]:
    """One injectable `identity_resolver` shape: read a composition id from a file's contents.

    Returns `None` (never raises) if the file is absent/unreadable/empty -- same
    fall-back-to-`new_composition_id()`-on-`None` contract as `identity_from_env`. Content
    is read as utf-8 text and stripped; a caller owning the per-composition-file channel
    (§ module docstring "RESERVED FOR THE PM") is responsible for writing that file, not
    this module.
    """
    try:
        with open(path, "r", encoding="utf-8") as fh:
            value = fh.read().strip()
    except OSError:
        return None
    return value if value else None


@dataclass
class CompositionBudget:
    """A composer-local (per-instance, § module docstring warm-engine note) invocation +
    elapsed-time budget tracker for one composition.

    `composition_id`: caller-supplied, or resolved via `identity_resolver`/
    `new_composition_id()` at construction if not supplied (see `for_composition()`
    below, the recommended constructor when identity propagation matters).

    `aggregate_elapsed_budget` / `max_invocations`: either, both, or neither may be set
    (`None` disables that ceiling, matching `run_entrypoint_gate`'s own
    `aggregate_budget=None` "no ceiling" default). Independent ceilings, not blended --
    see module docstring "WHY ELAPSED TIME IS NOT OPTIONAL".

    `disposition`: `SKIP_AND_SURFACE` (default) or `FAIL_LOUD` (§ module docstring).

    `on_count`: optional `Callable[[str, int], None]`, called from `record_invocation()`
    with `(unit, running_total)` after the internal counter is updated -- the injection
    point a caller uses to extend a durable sink (e.g. op_latency's) without this module
    importing it (§ module docstring "DEPENDENCY-FREE LEAF"). Never called from `check()`
    or `advisory_check()` -- those are read-only against already-recorded state.

    `clock`: defaults to `time.monotonic` (matches `run_entrypoint_gate`'s own
    `_budget_ok`); injectable for tests.
    """

    composition_id: str
    aggregate_elapsed_budget: "float | None" = None
    max_invocations: "int | None" = None
    disposition: str = SKIP_AND_SURFACE
    on_count: "Callable[[str, int], None] | None" = None
    clock: "Callable[[], float]" = time.monotonic

    _start: float = field(init=False, default=0.0, repr=False)
    _invocation_count: int = field(init=False, default=0, repr=False)
    _breached_units: list = field(init=False, default_factory=list, repr=False)

    def __post_init__(self) -> None:
        if self.disposition not in _VALID_DISPOSITIONS:
            raise ValueError(
                f"CompositionBudget.disposition must be one of {sorted(_VALID_DISPOSITIONS)}, "
                f"got {self.disposition!r}"
            )
        self._start = self.clock()

    @classmethod
    def for_composition(
        cls,
        *,
        identity_resolver: "Callable[[], str | None] | None" = None,
        **kwargs,
    ) -> "CompositionBudget":
        """Construct with an injectable identity channel (§ module docstring, "RESERVED FOR
        THE PM"). `identity_resolver`, when supplied, is called once; a `None` return (or
        no resolver at all) falls back to `new_composition_id()`. This is the recommended
        entry point for a caller that wants cross-process propagation without hand-rolling
        the fallback -- the plain `CompositionBudget(composition_id=..., ...)` constructor
        remains available for a caller that already has an id in hand.
        """
        resolved = identity_resolver() if identity_resolver is not None else None
        composition_id = resolved if resolved else new_composition_id()
        return cls(composition_id=composition_id, **kwargs)

    def elapsed_secs(self) -> float:
        """Seconds since this instance was constructed, per its own `clock`."""
        return self.clock() - self._start

    @property
    def invocation_count(self) -> int:
        return self._invocation_count

    @property
    def breached_units(self) -> tuple:
        """Every unit name `check()`/`advisory_check()` has ever reported a breach for,
        in the order first observed. Never cleared -- a breach, once true, stays true for
        this instance's remaining life (matching `run_entrypoint_gate`'s own
        `budget_exceeded` list, which is append-only across its call)."""
        return tuple(self._breached_units)

    def record_invocation(self, unit: str = "invocation", *, count: int = 1) -> int:
        """Record `count` (default 1) invocation(s) against this composition's counter.

        Pure in-memory increment (§ module docstring, "NO SPAWN TO CHECK THE BUDGET") --
        never itself checks the budget; call `check()`/`advisory_check()` separately.
        Calls `on_count(unit, running_total)` after incrementing, if `on_count` is set.
        Returns the running total.
        """
        self._invocation_count += count
        if self.on_count is not None:
            self.on_count(unit, self._invocation_count)
        return self._invocation_count

    def _within_budget(self) -> bool:
        if (
            self.aggregate_elapsed_budget is not None
            and self.elapsed_secs() > self.aggregate_elapsed_budget
        ):
            return False
        if (
            self.max_invocations is not None
            and self._invocation_count >= self.max_invocations
        ):
            return False
        return True

    def _breach_unit(self) -> str:
        """Which ceiling breached, for `BudgetBreach.unit`/`breached_units` bookkeeping.

        Elapsed is checked first in `_within_budget()`, so it is reported first here too
        when both ceilings happen to be breached simultaneously -- an arbitrary but stable
        tie-break, not a claim that elapsed is "more breached" than invocations.
        """
        if (
            self.aggregate_elapsed_budget is not None
            and self.elapsed_secs() > self.aggregate_elapsed_budget
        ):
            return "elapsed"
        return "invocations"

    def check(self, unit: "str | None" = None) -> bool:
        """Boundary check (§ module docstring disposition section): the primary call
        shape a caller uses BEFORE dispatching the next unit of work (mirrors
        `run_entrypoint_gate`'s `_budget_ok()`, "checked before each entrypoint is
        DISPATCHED, not merely before each is reported").

        `unit` names what is about to be dispatched (e.g. an entrypoint id, a directive
        name) for breach bookkeeping/messaging; defaults to whichever ceiling breached
        (`"elapsed"`/`"invocations"`) when not supplied.

        Returns `True` if still within budget. On breach: `SKIP_AND_SURFACE` (default)
        returns `False` (never raises) and records the unit in `breached_units`;
        `FAIL_LOUD` raises `BudgetBreach` instead (also recording the unit first, so a
        caller catching `BudgetBreach` still finds it in `breached_units`).
        """
        if self._within_budget():
            return True

        breach_unit = unit if unit is not None else self._breach_unit()
        self._breached_units.append(breach_unit)

        if self.disposition == FAIL_LOUD:
            raise BudgetBreach(
                composition_id=self.composition_id,
                unit=breach_unit,
                invocation_count=self._invocation_count,
                elapsed_secs=self.elapsed_secs(),
                aggregate_elapsed_budget=self.aggregate_elapsed_budget,
                max_invocations=self.max_invocations,
            )
        return False

    def advisory_check(self, unit: "str | None" = None) -> bool:
        """Mid-directive, WARN-ONLY observation (§ module docstring, chunk C10 pairing).

        Identical breach test to `check()`, but NEVER raises regardless of
        `disposition`, and has no control-flow effect -- a caller uses this to surface a
        breach at the point it happens (e.g. a periodic elapsed check inside a long
        directive) without adopting `check()`'s fail-loud abort semantics inline. Still
        records the breached unit in `breached_units`, so a boundary `check()` call made
        later in the same composition sees the same accumulated history either way would
        have produced.

        Returns `True` if still within budget, `False` on breach (same boolean contract
        as `check()`'s `SKIP_AND_SURFACE` path, regardless of this instance's actual
        `disposition`).
        """
        if self._within_budget():
            return True
        breach_unit = unit if unit is not None else self._breach_unit()
        self._breached_units.append(breach_unit)
        return False

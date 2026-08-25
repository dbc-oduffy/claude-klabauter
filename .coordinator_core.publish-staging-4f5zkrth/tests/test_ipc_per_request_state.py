"""
coordinator_core.tests.test_ipc_per_request_state — C11 regression net.

Covers the two defects C11 fixes, both invisible under one-op-per-process and
both only observable under OVERLAPPING dispatch:

  1. `_declared_writes_var` (the DR-276 declare-write collection) is now bound
     via `ContextVar.set()`'s returned Token and unwound via `reset()` in a
     `finally`, rather than a bare `set()` with no reset. Two interleaved
     dispatches with different declared writes must not cross-contaminate —
     each dispatch's `declare_write()` calls must land only on that
     dispatch's own declared-writes list, never a sibling's.
  2. `DISPATCH_TIMEOUT_SECS` / `_timeout_for()` are resolved PER REQUEST via
     `ipc._resolve_dispatch_timeout_secs()`, re-reading
     `COORDINATOR_DISPATCH_TIMEOUT_SECS` from `os.environ` on every call
     rather than trusting the value the module constant snapshotted at
     import — a warm, long-lived process must be able to retune this knob
     without a restart. Since 2026-08-21 that retune is NARROW-ONLY: the env
     var may lower the guard and can no longer raise it above the built-in
     default. The ratchet at the end of this module is the enforcement, and
     it is the half that was previously vacuous — the override-row sweeps
     preceding it iterate an empty table.
  3. `_OP_TIMEOUT_OVERRIDES` remains live after the C11 change: an override
     row, when one exists, must still resolve regardless of the global
     knob's per-request re-resolution. Both rows this docstring used to name
     are gone — `coverage.gate` (600s, removed K-001, state/kill-ledger.md)
     and `ceremony.scoped_git_commit` (150s, revoked 2026-08-21 by the
     ceremony budget, DR-348). Ceremony ops now have their own bound, owned
     by `coordinator_core/tests/test_ceremony_budget_ratchet.py`, not by any
     row in this table — see `ipc.CEREMONY_BUDGET_SECS`.

Handlers use `asyncio.run()` in sync test functions — no pytest-asyncio
dependency, matching `test_dispatch_message.py`'s own pattern.

Spec backlink: docs/plans/2026-08-15-warm-engine-retires-the-per-invocation-cold-start.md § C11
"""

from __future__ import annotations

import asyncio

import pytest

import coordinator_core.ipc as ipc
from coordinator_core.ipc import dispatch_message, _REGISTRY
from coordinator_core.session.declared_writes import declare_write, active_declarations


def _run(coro):
    return asyncio.run(coro)


class _RegistryScope:
    """Install test handlers into `_REGISTRY`, restore on exit.

    Mirrors `test_dispatch_message.py::_RegistryScope` exactly — kept as a
    local copy rather than an import so this test file has no coupling to
    that module's internals beyond the shared `_REGISTRY` object.
    """

    def __init__(self, handlers: dict) -> None:
        self._handlers = handlers
        self._saved: dict = {}

    def __enter__(self):
        for name in self._handlers:
            self._saved[name] = _REGISTRY.get(name)
        _REGISTRY.update(self._handlers)
        return self

    def __exit__(self, *_):
        for name, old in self._saved.items():
            if old is None:
                _REGISTRY.pop(name, None)
            else:
                _REGISTRY[name] = old


# ---------------------------------------------------------------------------
# Defect 1 — declared-writes isolation under overlapping dispatch
# ---------------------------------------------------------------------------

def test_overlapping_dispatch_declared_writes_do_not_cross_contaminate():
    """Two concurrently-running dispatches each see only their own declared
    writes — the misattributed-write-claim failure C11 names.

    `_record_self_reported_touches` unconditionally pops
    `_SCOPE_TOUCH_PATHS_KEY` off the wire result (by design — it never
    reaches the caller), so this test observes the mid-flight collection
    directly via `active_declarations()` — a live snapshot taken from
    inside each handler — rather than through the wire response.
    """
    seen: dict = {}

    async def _declaring_handler_a(params: dict, ctx=None, repo_root=None) -> dict:
        """Declares one write, yields (so the sibling dispatch can interleave),
        then declares a second write and snapshots the collection —
        exercising the window where a bare `set()` with no reset previously
        let a later dispatch's rebind stomp this dispatch's still-open
        collection."""
        declare_write("a-first.txt")
        await asyncio.sleep(0.05)
        declare_write("a-second.txt")
        seen["a"] = list(active_declarations() or [])
        return {"who": "a"}

    async def _declaring_handler_b(params: dict, ctx=None, repo_root=None) -> dict:
        """Starts after `a` has already declared once, declares its own
        write, and returns before `a` declares its second write — the
        interleave shape that reproduces a misattributed write claim if the
        two dispatches share one `ContextVar` slot."""
        await asyncio.sleep(0.01)
        declare_write("b-only.txt")
        seen["b"] = list(active_declarations() or [])
        return {"who": "b"}

    msg_a = {"jsonrpc": "2.0", "id": "a", "method": "test.declare_a", "params": {}}
    msg_b = {"jsonrpc": "2.0", "id": "b", "method": "test.declare_b", "params": {}}

    async def _both():
        return await asyncio.gather(
            dispatch_message(msg_a),
            dispatch_message(msg_b),
        )

    handlers = {"test.declare_a": _declaring_handler_a, "test.declare_b": _declaring_handler_b}
    with _RegistryScope(handlers):
        _run(_both())

    assert seen["a"] == ["a-first.txt", "a-second.txt"], (
        f"dispatch a's declared writes were contaminated by dispatch b: {seen['a']!r}"
    )
    assert seen["b"] == ["b-only.txt"], (
        f"dispatch b's declared writes were contaminated by dispatch a: {seen['b']!r}"
    )


def test_declared_writes_var_reset_after_dispatch_completes():
    """After a dispatch returns, the module-level `_declared_writes_var`
    context slot is back to its pre-dispatch state (None at module scope) —
    proof `reset()` actually unwinds the Token rather than leaving the
    dispatch's list bound forever."""
    from coordinator_core.session.declared_writes import _ACTIVE

    assert _ACTIVE.get() is None

    msg = {"jsonrpc": "2.0", "id": 1, "method": "test.declare_sync", "params": {}}

    def _handler(params, ctx=None, repo_root=None):
        declare_write("x.txt")
        return {}

    with _RegistryScope({"test.declare_sync": _handler}):
        _run(dispatch_message(msg))

    assert _ACTIVE.get() is None, (
        "_declared_writes_var must be reset after dispatch, not left bound"
    )


# ---------------------------------------------------------------------------
# Defect 2 — DISPATCH_TIMEOUT_SECS resolved per-request, not at import
# ---------------------------------------------------------------------------

def test_resolve_dispatch_timeout_secs_reads_env_live(monkeypatch):
    """`_resolve_dispatch_timeout_secs()` picks up a live env-var change on
    the very next call — no reliance on the import-time snapshot.

    Both probes narrow, because narrowing is now the only direction the knob
    has (see the ratchet section below). This test used to probe with 77 and
    assert 77 — a value ABOVE the built-in default, which is exactly what the
    narrow-only clamp exists to refuse. Liveness and widening were never the
    same property; only liveness is under test here."""
    monkeypatch.delenv("COORDINATOR_DISPATCH_TIMEOUT_SECS", raising=False)
    baseline = ipc._resolve_dispatch_timeout_secs()
    assert baseline == ipc.DISPATCH_TIMEOUT_SECS

    monkeypatch.setenv("COORDINATOR_DISPATCH_TIMEOUT_SECS", "7")
    assert ipc._resolve_dispatch_timeout_secs() == 7.0

    monkeypatch.setenv("COORDINATOR_DISPATCH_TIMEOUT_SECS", "12.5")
    assert ipc._resolve_dispatch_timeout_secs() == 12.5


def test_resolve_dispatch_timeout_secs_falls_back_to_module_attr(monkeypatch):
    """With no env var set, `_resolve_dispatch_timeout_secs()` falls back to
    the `DISPATCH_TIMEOUT_SECS` module attribute — so a caller (or test) that
    assigns `ipc.DISPATCH_TIMEOUT_SECS = X` directly still takes effect,
    matching the pre-C11 monkeypatch idiom used elsewhere in this suite."""
    monkeypatch.delenv("COORDINATOR_DISPATCH_TIMEOUT_SECS", raising=False)
    orig = ipc.DISPATCH_TIMEOUT_SECS
    try:
        ipc.DISPATCH_TIMEOUT_SECS = 42.0
        assert ipc._resolve_dispatch_timeout_secs() == 42.0
        assert ipc._timeout_for("test.nonesuch") == 42.0
    finally:
        ipc.DISPATCH_TIMEOUT_SECS = orig


def test_resolve_dispatch_timeout_secs_ignores_unparsable_env(monkeypatch):
    """An unparsable env value falls back to the module attribute rather than
    raising out of the dispatch hot path."""
    monkeypatch.setenv("COORDINATOR_DISPATCH_TIMEOUT_SECS", "not-a-number")
    assert ipc._resolve_dispatch_timeout_secs() == ipc.DISPATCH_TIMEOUT_SECS


# ---------------------------------------------------------------------------
# Defect 2, continued — _OP_TIMEOUT_OVERRIDES stays live after the change
# ---------------------------------------------------------------------------

def test_op_timeout_overrides_still_resolve_after_per_request_change(monkeypatch):
    """An `_OP_TIMEOUT_OVERRIDES` row keeps resolving to its table value
    regardless of the global knob's per-request re-resolution — the C11
    body's explicit DO NOT BREAK.

    Both real rows this test used to pin against are gone: `coverage.gate`
    (removed, K-001) now tracks the live global knob like any unlisted op,
    and `ceremony.scoped_git_commit` (revoked 2026-08-21, DR-348) can no
    longer carry a widening row at all — a live row would still be inert,
    clamped by `CEREMONY_BUDGET_SECS`
    (test_ceremony_budget_ratchet.py::test_an_injected_widening_row_is_still_clamped),
    which would make it useless as a test of "override wins over the env
    knob". A synthetic NON-ceremony row keeps that property under test
    without depending on a row that must never exist again."""
    monkeypatch.setitem(ipc._OP_TIMEOUT_OVERRIDES, "test.widened", 42.0)
    monkeypatch.setenv("COORDINATOR_DISPATCH_TIMEOUT_SECS", "5")
    assert ipc._timeout_for("test.widened") == 42.0
    # A method NOT in the override table still tracks the live global knob.
    assert ipc._timeout_for("ping") == 5.0


# ---------------------------------------------------------------------------
# Dispatch-timeout ratchet — every override is monotonically non-increasing.
# docs/wiki/cost-budgets-and-the-kill-disposition.md
# ---------------------------------------------------------------------------

# Each row's high-water mark. A timeout may be LOWERED freely (that is the
# direction this repo wants and no test should stand in its way); raising one
# requires editing this table, which is the point — the edit is the argument,
# made in a diff a reviewer sees, rather than a one-character change to a dict
# literal that reads as routine tuning.
#
# `ceremony.scoped_git_commit`'s 150.0s row lived here until 2026-08-21, when the
# ceremony budget (DR-348) revoked it outright rather than lowering it — a
# ceremony op's ceiling is no longer this table's business at all. That budget
# owns every `ceremony.*` method by prefix, present or future, and ratchets on
# its own schedule; see `coordinator_core/tests/test_ceremony_budget_ratchet.py`.
# This table now governs only non-ceremony override rows.
_TIMEOUT_HIGH_WATER_SECS: dict = {}


def test_op_timeout_overrides_never_ratchet_upward():
    """No `_OP_TIMEOUT_OVERRIDES` row may exceed its recorded high-water mark.

    This is the enforcement half of the budget rule: a bound is derived from
    what the box can afford at the load norm and then measured against, never
    fitted to what the op currently costs. Without a ratchet, every regression
    arrives as a locally-reasonable argument for a slightly larger constant —
    each one defensible in isolation, and the sequence unbounded. `coverage.gate`
    is the worked example: 30s until eb3c24348 raised it to 600s to unmask a
    real latency property, after which a single op held for ten minutes at a
    time and took 45 of the 49 fleet-wide >60s events on 2026-08-15. A later
    pass then derived 660s from post-fix telemetry — arithmetic that was correct
    and a direction that was not.

    An op that cannot fit under its bound is a kill candidate. Necessity is not
    a defense: work that genuinely requires the time is thereby shown not to be
    worth it, and the disposition is removal plus a rebuild record, never a
    larger number.
    """
    for method, ceiling in _TIMEOUT_HIGH_WATER_SECS.items():
        assert method in ipc._OP_TIMEOUT_OVERRIDES, (
            f"{method} left _OP_TIMEOUT_OVERRIDES while its high-water mark "
            f"remains here — drop the row from _TIMEOUT_HIGH_WATER_SECS too, "
            f"and record the kill in the rebuild ledger."
        )
        assert ipc._OP_TIMEOUT_OVERRIDES[method] <= ceiling, (
            f"{method} raised to {ipc._OP_TIMEOUT_OVERRIDES[method]}s over its "
            f"{ceiling}s high-water mark. Over budget is a kill candidate, not a "
            f"budget raise — see docs/wiki/cost-budgets-and-the-kill-disposition.md."
        )


def test_timeout_high_water_table_covers_every_override():
    """Every override row carries a high-water mark, so a new op cannot enter
    the table above the ratchet's reach and become the next unbounded row."""
    missing = set(ipc._OP_TIMEOUT_OVERRIDES) - set(_TIMEOUT_HIGH_WATER_SECS)
    assert not missing, (
        f"new _OP_TIMEOUT_OVERRIDES rows with no high-water mark: {sorted(missing)}. "
        f"Add each to _TIMEOUT_HIGH_WATER_SECS at the value it enters with."
    )


# ---------------------------------------------------------------------------
# The global knob is narrow-only — the half of the ratchet that was vacuous.
#
# The two ratchet tests above sweep `_OP_TIMEOUT_OVERRIDES`, which is empty and
# has been since DEC-2. They pass by iterating nothing. That is not a latent
# guard waiting for a row: it is a guard aimed at the surface nobody uses, while
# the surface everybody uses -- `COORDINATOR_DISPATCH_TIMEOUT_SECS`, re-read live
# on every request, effective with no restart, settable from any sibling repo --
# carried no ceiling at all. `COORDINATOR_DISPATCH_TIMEOUT_SECS=420` was obeyed
# immediately, and the ratchet above had nothing to say about it.
#
# The tests below put the knob itself under the ratchet. Same rule as every other
# budget here: it may be LOWERED freely, and raising it is an edit to a pinned
# literal that a reviewer reads as the argument it is.
# ---------------------------------------------------------------------------

#: The built-in default's high-water mark, as an independent second literal --
#: deliberately NOT `ipc.DISPATCH_TIMEOUT_SECS`, since importing the value under
#: test would make this file agree with any number whatsoever. Lowering the engine
#: default below this is permitted and needs no edit here; raising it above 30s
#: fails the suite.
_GLOBAL_TIMEOUT_HIGH_WATER_SECS = 30.0


def test_the_builtin_dispatch_default_is_at_or_below_its_high_water_mark():
    assert ipc.DISPATCH_TIMEOUT_SECS <= _GLOBAL_TIMEOUT_HIGH_WATER_SECS, (
        f"DISPATCH_TIMEOUT_SECS raised to {ipc.DISPATCH_TIMEOUT_SECS}s over its "
        f"{_GLOBAL_TIMEOUT_HIGH_WATER_SECS}s high-water mark. An op that does not "
        f"fit the guard is a kill candidate, not a budget raise -- see "
        f"docs/wiki/cost-budgets-and-the-kill-disposition.md."
    )


@pytest.mark.parametrize("requested", ["31", "60", "420", "9999", "1e9", "inf"])
def test_the_env_knob_cannot_raise_the_dispatch_timeout(monkeypatch, requested):
    """Every widening value the knob can carry resolves to at most the default.

    Parametrized over the shapes an operator actually reaches for -- a nudge over
    the line, a doubling, the 420 from the incident, an absurdity, and the two
    float spellings (`1e9`, `inf`) that a naive `float(raw)` accepts and a naive
    ceiling check written with `int` would not.
    """
    monkeypatch.setenv("COORDINATOR_DISPATCH_TIMEOUT_SECS", requested)
    assert ipc._resolve_dispatch_timeout_secs() <= _GLOBAL_TIMEOUT_HIGH_WATER_SECS
    assert ipc._timeout_for("ping") <= _GLOBAL_TIMEOUT_HIGH_WATER_SECS
    assert ipc._timeout_for("test.unregistered") <= _GLOBAL_TIMEOUT_HIGH_WATER_SECS


def test_the_env_knob_still_narrows(monkeypatch):
    """Narrowing is the permitted direction and must stay live and exact -- a
    fast-fail run is a supported use, and a clamp written as `max` or as an
    equality would break it."""
    monkeypatch.setenv("COORDINATOR_DISPATCH_TIMEOUT_SECS", "0.25")
    assert ipc._resolve_dispatch_timeout_secs() == pytest.approx(0.25)
    assert ipc._timeout_for("ping") == pytest.approx(0.25)


def test_the_builtin_default_is_not_itself_env_derived():
    """The ceiling may not be computed from the thing it bounds.

    `DISPATCH_TIMEOUT_SECS` was `float(os.environ.get("COORDINATOR_DISPATCH_
    TIMEOUT_SECS", "30"))` until 2026-08-21. With that read in place, clamping the
    env knob against this constant clamps it against itself: an engine started with
    `COORDINATOR_DISPATCH_TIMEOUT_SECS=420` takes 420 as its ceiling and the clamp
    is a no-op. Every runtime assertion in this module would still pass -- the
    monkeypatched env arrives after import, so no in-process test can see it.

    Asserted against the source, therefore, rather than against behaviour: the
    module-level binding must be a plain literal. A subprocess would also prove it
    and would cost a process start on every run, which this repo's brightline does
    not spend for a fact an AST read establishes for free.
    """
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(ipc))
    bindings = [
        node
        for node in tree.body
        if isinstance(node, ast.AnnAssign)
        and isinstance(node.target, ast.Name)
        and node.target.id == "DISPATCH_TIMEOUT_SECS"
    ]
    assert len(bindings) == 1, (
        f"expected exactly one module-level DISPATCH_TIMEOUT_SECS binding in ipc.py, "
        f"found {len(bindings)} -- a second one would decide the ceiling and this "
        f"test would be checking the wrong node."
    )
    assert isinstance(bindings[0].value, ast.Constant), (
        "DISPATCH_TIMEOUT_SECS must be bound to a bare literal. It is now the "
        "ceiling COORDINATOR_DISPATCH_TIMEOUT_SECS is clamped against, so deriving "
        "it from that same env var (or from anything else an operator controls) "
        "restores the unbounded knob this clamp removed, silently and with every "
        "other test in this file still green."
    )

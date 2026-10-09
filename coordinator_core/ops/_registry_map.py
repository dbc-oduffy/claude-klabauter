"""
coordinator_core.ops._registry_map — op-name -> owning-module lazy-import seam.

Purpose: seed a name->module map so dispatch_message (ipc.py) can import ONLY the
module that owns a given op on a registry MISS, instead of eagerly importing all
~55 op modules at process startup (F6 / cold-start tax — Windows has no
__pycache__ warm-up, so compiling 55 modules to run one wastes ~150-250ms per
invoke). This module contains ONLY dotted-path strings — it must NOT import any
op module itself, or it would defeat its own purpose (and risk a circular import
with coordinator_core.ipc, which every op module imports register_op from).

Maintenance: this map is a PERFORMANCE OPTIMIZATION, not a correctness gate. If an
entry is missing or stale (an op renamed/moved without updating this table), the
lazy-import miss path in ipc.dispatch_message falls back to importing the whole
coordinator_core.ops package (today's eager behavior) and retries — so a stale/
incomplete map degrades to today's correctness, never to a broken dispatch. Keep
this in sync with coordinator_core/ops/__init__.py's import list on a best-effort
basis; the fallback is the enforcement mechanism, not a hand-audit.

Spec backlink: pln-claude-klabauter-windows-portability-a48fac § C4
"""

from __future__ import annotations

from typing import Dict

# op-name -> dotted module path whose import triggers that op's register_op(...)
# side-effect. Some modules register multiple related ops (e.g. coordinator_core.hooks
# registers all 10 hooks.* ops in one import) — those ops share the same module value.
#
# Every key here must ALSO be reachable from the eager-import path
# (coordinator_core/ops/__init__.py::_EAGER_OP_MODULES), or the op registers only under
# whichever import order happens to pull its module in — see coordinator_core/hooks/
# __init__.py for the order-dependent drift-guard failure that shape produces.
OP_MODULE_MAP: Dict[str, str] = {
    # (see op_scopes.py::_OP_KEY_SCOPE's peer_notice.* entries, both "common_dir"),
    # each registered by its own
    # owning module. Registered in _REGISTRY and _OP_KEY_SCOPE/OP_CLASSIFICATION but
    # absent from this map until C3's three-way reconciliation (docs/plans/
    # 2026-08-15-warm-engine-retires-the-per-invocation-cold-start.md § C3) — a real
    # registry_map.py::OP_MODULE_MAP gap, not a deliberate omission; per this
    # module's docstring the absence degraded silently to the eager-import
    # fallback rather than breaking dispatch, which is why it went unnoticed.
    # coordinator_core.hooks registers all 16 hooks.* ops (6 advisory + 8 bookkeeping
    # + 1 pull/poll arrival-check + 1 subagent-fabrication check) in a single module
    # import. This package-level
    # granularity (one shared module value for all 15 keys, rather than a per-op
    # owning submodule) IS the correct mapping here, not a stand-in for a finer
    # split — confirmed C2 (docs/plans/2026-08-06-windows-hot-path-less-work-per-
    # interpreter.md): under the lazy hooks channel (C1), importing the shared
    # "coordinator_core.hooks" value alone is a lazy-gated no-op that registers
    # nothing, so ipc._lazy_import_and_lookup adds a hooks-scoped fallback stage
    # (coordinator_core.hooks._eager_import_all()) ahead of the ops-wide SAFE
    # FALLBACK, rather than repointing these entries to nonexistent per-op
    # submodules.
    # plan.prep_gate — the mise-prep authoring bar, REPORTED per class. Read twin
    # of plan.stamp_prepped; the DoE-side runnable half is
    # coordinator/bin/mise-prep-gate.py and the two must agree.
    "plan.prep_gate":                         "coordinator_core.ops.plan_prep_gate",
    "seam.mint_batons":                       "coordinator_core.ops.seam_baton_mint",
    "sizing.record_register":                    "coordinator_core.ops.sizing_record_register",
    "review_stamp.mint":                      "coordinator_core.ops.review_stamp",
    "review_stamp.rejudge":                   "coordinator_core.ops.review_stamp",
    "review_stamp.check":                     "coordinator_core.ops.review_stamp",
    "requirement_register.stall_report":      "coordinator_core.ops.requirement_register_stall",
    # C9 (docs/plans/2026-09-21-bug-blitz-emitter-engine-leg.md): the closed
    # queue-grind op list the vocabulary's SOURCE_OPS/VERIFY_OPS/REGENERATE_OPS
    # (C1) resolve to — one shared owning module, same many-keys-one-value
    # shape as the learn_lessons_pipeline.* pair above.
}


def resolves(op_key: str) -> bool:
    """True when `op_key` is DISPATCHABLE right now: it either has a lazy-import
    entry in OP_MODULE_MAP (dispatch_message can import its owning module on a
    registry miss) or it is already live in coordinator_core.ipc._REGISTRY
    (already registered, e.g. under eager-import mode or after a prior
    dispatch). This is a weaker claim than `"x" in _REGISTRY`: that proves "x is
    registered right now"; `resolves("x")` proves "x is dispatchable" -- true
    even with an empty _REGISTRY, since a mapped miss still resolves via lazy
    import. It is NOT a substitute for a registry read where the assertion's
    subject is registry state itself (an empty-registry proof, or a
    binding-identity check of which callable is bound) -- see
    docs/plans/2026-08-22-the-import-path-costs-nothing.md § C3 for the two
    excluded assertion classes.

    `ipc` is imported locally, not at module scope, to preserve this module's
    dotted-path-strings-only load-time contract (see module docstring) --
    resolves() is a test/inspection helper, not part of the hot dispatch path.

    Spec backlink: docs/plans/2026-08-22-the-import-path-costs-nothing.md § C3
    """
    if op_key in OP_MODULE_MAP:
        return True
    from coordinator_core import ipc

    return op_key in ipc._REGISTRY

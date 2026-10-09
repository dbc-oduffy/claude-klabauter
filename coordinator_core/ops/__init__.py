"""
coordinator_core.ops — IPC operation handlers package.

Purpose: Namespace for all coordinator_core op implementations. Each sub-module
self-registers its handler(s) via register_op() at import time. Importing this
bare package NEVER populates the op-registry — registration is lazy
unconditionally, with no flag or channel to arm it: a caller must trigger a
targeted per-op import itself (see coordinator_core.ipc._lazy_import_and_lookup),
or call _eager_import_all() directly for the rare full-registration need.

Op registration list is maintained in coordinator_core/op_scopes.py::_OP_KEY_SCOPE
(coordinator_core/ipc.py:441 only imports it from there).
Replaced stale hand-enumeration (8 of 19+ ops) with a canonical
reference to _OP_KEY_SCOPE, which is kept current as each op lands.

Lazy op registration (F6 / claude-klabauter-windows-portability § C4, made unconditional
2026-08-22 — the import-path-costs-nothing sprint): dispatching a single op via
coordinator_core.invoke used to unconditionally `import coordinator_core.ops`,
which (because Python always executes a package's __init__.py in full before any
of its submodules) forced ALL ~55 op modules to compile/import to run just ONE op
— a ~150-250ms Windows cold-compile tax per invoke (no __pycache__ warm-up there).
A two-channel flag (`COORDINATOR_CORE_LAZY_OPS` env var / `sys._coordinator_core_lazy_ops`
in-process attribute) used to gate this package's eager-import block; both
channels, and the writers that armed the in-process one, are retired — every
consumer of a bare `import coordinator_core.ops` (including the ~50 test modules
that assert the registry at import time) now goes through the targeted-import or
SAFE FALLBACK paths below instead of relying on package-init to populate the
registry as a side effect.

_eager_import_all() is also exposed for ipc.py's registry-miss SAFE FALLBACK: if
an op is absent from OP_MODULE_MAP (or a mapped import didn't register it — map
drift), the fallback calls this function directly to force full registration
regardless of the lazy flag or of whatever partial state this package is
already in — idempotent, since re-importing an already-imported submodule is a
cheap no-op. This is what makes an incomplete/stale map degrade to today's
correctness rather than to a broken dispatch.

Resilient-and-loud per-module import (2026-07-21 break-class fix — one op
module's ImportError used to abort ALL ~80 modules' registration in one shot;
demonstrated live when a concurrent session's single deleted symbol
(`sh_argv`, imported transitively) produced 146 pytest collection errors from
ONE missing name). _eager_import_all() now imports each module in
_EAGER_OP_MODULES independently: a single module's failure no longer prevents
the other ~79 from registering (§ resilience), but every failure is printed to
stderr immediately, by name, with the real exception (§ loudness) — see
"Negative-spec" below for the anti-pattern this deliberately avoids. Failed
modules are recorded in _POISONED_MODULES so that a later DISPATCH of one of
that module's ops (coordinator_core.ipc.dispatch_message, via OP_MODULE_MAP)
re-surfaces the ORIGINAL exception instead of a generic "Method not found" —
see coordinator_core/ipc.py's METHOD_NOT_FOUND branch for that half of the fix.

Failure-mode analysis (why this design, not a stricter one):
  - The _eager_import_all() path is the one reached by ipc.py's registry-miss
    SAFE FALLBACK and by the handful of callers that force full registration
    explicitly (the census enumerators, the warm server's preload) — this is
    exactly the path the reported defect broke, so it MUST be resilient: one
    broken module must never take ~8000 unrelated tests down with it. Before
    2026-08-22 this was the package-init default, reached whenever neither lazy
    channel was armed; package-init now registers nothing at all.
  - The actual PRODUCTION dispatch path (the one-shot CLI, DR-215) does a
    TARGETED import via OP_MODULE_MAP — it needs no arming, since lazy is the
    only mode; it reaches _eager_import_all() only via the SAFE FALLBACK, and
    only for the one op being dispatched. Making package-init "fail hard"
    would not make production stricter (production doesn't take that path);
    it would only reintroduce the test-collision collapse this fix removes.
  - Production strictness instead lives at the DISPATCH boundary: an op whose
    owning module is poisoned still errors out on every call to that op — now
    with the real cause attached instead of a misleading "unknown op" — while
    ops in HEALTHY modules that used to be collaterally killed by the same
    import failure now correctly continue to work. That is a strict
    improvement over today, not added laxness: no op that worked before now
    silently no-ops, and no failure that used to be visible becomes invisible.

Spec backlink: pln-pcore-03-beachhead-coordinator-core-fecdbb § C1b
                docs/plans/2026-07-14-claude-klabauter-windows-portability.md § C4
                (2026-07-21 resilient-eager-import fix — no dedicated plan doc;
                PM-authorized same-day break-class fix, see session record)

Negative-spec (hard-won — do NOT reintroduce):
    - `try: import X\n    except ImportError: pass` (swallow-and-continue) was
      explicitly REJECTED. It trades a LOUD failure (today's total collapse)
      for a SILENT one: the module's ops just don't register, and a later
      `invoke` of one of them fails with a confusing generic "unknown op"
      instead of the real ImportError — strictly worse than collapse, because
      collapse at least tells you immediately and unambiguously. Every catch
      in this module prints the real module name + exception to stderr AND
      records it in _POISONED_MODULES so dispatch-time lookups can re-surface
      the true cause — resilience without silence.
"""

from __future__ import annotations

import importlib
import logging as _logging
import sys as _sys
import traceback as _traceback
from typing import Dict, List, Tuple

_logger = _logging.getLogger(__name__)

_EAGER_OP_MODULES: List[Tuple[str, str]] = [
    ("coordinator_core.ops.seam_baton_mint", 'registers "seam.mint_batons"'),
    ("coordinator_core.ops.plan_prep_gate", 'registers "plan.prep_gate"'),
    (
        "coordinator_core.ops.sizing_record_register",
        'registers "sizing.record_register" (2026-10-09, writes a sizing\'s '
        '`requirement_register` block with a recomputed rollup)',
    ),
    (
        "coordinator_core.ops.review_stamp",
        'registers "review_stamp.mint", "review_stamp.check"',
    ),
    ("coordinator_core.ops.requirement_register_stall", 'registers "requirement_register.stall_report"'),
]

# module dotted-path -> the exception raised the last time we tried to import
# it. Populated by _eager_import_all() on a per-module ImportError/Exception;
# cleared on a subsequent successful import of that same module (self-healing
# if the module is fixed mid-process, e.g. under pytest --looponfail). Read by
# coordinator_core.ipc's dispatch_message to turn a registry MISS on a
# poisoned module's op into the real cause instead of a generic "Method not
# found" (see ipc.py's METHOD_NOT_FOUND branch).
_POISONED_MODULES: Dict[str, BaseException] = {}


def get_poisoned_modules() -> Dict[str, BaseException]:
    return dict(_POISONED_MODULES)


def _eager_import_all() -> None:
    """Import every production op module, firing each one's register_op(...)
    side-effect. This is the exact set of imports that used to run
    unconditionally at package-init time; it is now also independently
    callable (by ipc.py's registry-miss fallback) to force full registration
    on demand, regardless of which submodules (if any) are already imported.
    (Before 2026-08-22 this also read "regardless of the
    COORDINATOR_CORE_LAZY_OPS flag"; there is no such flag now.)

    Resilient-and-loud (2026-07-21): each module is imported independently.
    A single module's import failure is:
      1. NOT allowed to prevent any other module from registering (resilience).
      2. Printed to stderr immediately, naming the module and the real
         exception (loudness) — never swallowed, never merely debug-logged.
      3. Recorded in _POISONED_MODULES so a later dispatch of one of that
         module's ops can raise the real cause (see coordinator_core.ipc).
    See the module docstring's "Negative-spec" for the try/except-pass
    anti-pattern this deliberately avoids.
    """
    for module_path, _note in _EAGER_OP_MODULES:
        try:
            if module_path == "coordinator_core.hooks":
                hooks_module = importlib.import_module(module_path)
                hooks_module._eager_import_all()
                for poisoned_path, poisoned_exc in hooks_module.get_poisoned_modules().items():
                    _POISONED_MODULES[poisoned_path] = poisoned_exc
            else:
                importlib.import_module(module_path)
        except Exception as exc:  # noqa: BLE001 — intentional broad catch, see docstring
            _POISONED_MODULES[module_path] = exc
            # ERROR-severity logging call (§ FUNCTION gate C4C brief "make the
            # silent swallow observable") ALONGSIDE the pre-existing stderr
            # print below — control flow is UNCHANGED (still resilient: no
            # raise, every other module still gets its own import attempt).
            # This is purely about making a per-module import failure land
            # in anything that watches Python's logging machinery (e.g. a
            # log-aggregation handler attached to the root logger), which a
            # bare stderr print to an unread hermetic subprocess (§
            # `coordinator_core/percolate/engine.py` `run_function_gate`,
            # which only inspects stdout for "GATE_OK"/stderr for a
            # "GATE_FAIL:" marker it never emits here) does not reach.
            _logger.error(
                "coordinator_core.ops: FAILED to import %r (%s: %s) — its "
                "op(s) will NOT be registered",
                module_path,
                type(exc).__name__,
                exc,
            )
            print(
                f"coordinator_core.ops: FAILED to import {module_path!r} "
                f"({type(exc).__name__}: {exc}) — its op(s) will NOT be "
                f"registered; the other {len(_EAGER_OP_MODULES) - 1} op "
                f"modules are unaffected. Dispatching any op owned by this "
                f"module will re-raise this exact cause instead of "
                f"'unknown op'.",
                file=_sys.stderr,
            )
            _traceback.print_exc(file=_sys.stderr)
        else:
            _POISONED_MODULES.pop(module_path, None)


# Lazy is the only mode: importing this bare package never eagerly registers
# any op. The former `_lazy_ops_requested()` gate (COORDINATOR_CORE_LAZY_OPS
# env var / sys._coordinator_core_lazy_ops in-process attribute) is retired —
# there is no longer a flag to read or a channel to arm, so no conditional
# call to _eager_import_all() happens here. Callers reach registration
# through the targeted per-op import (ipc.py's registry-miss path) or, for the
# rare full-registration need, by calling _eager_import_all() directly.

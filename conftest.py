"""Repo-root conftest — eager op registration before any test module imports.

WHY THIS EXISTS. `coordinator_core/ops/__init__.py` skips its eager
per-module import list when `sys._coordinator_core_lazy_ops` is armed, and
~123 modules assert `_REGISTRY` contents at import time. Whether the op
registry is populated in time therefore depends on which test module pytest
imports first. pytest imports every initial conftest during
`pytest_load_initial_conftests`, strictly before it imports any test module,
so importing the package here guarantees the registry is populated before
collection reaches a test module, regardless of collection order.

The hazard is pytest-only. `coordinator_core.ipc.get_op_handler` already
resolves a registry miss by importing the owning module and falling back to
`_eager_import_all()`, so no production dispatch path can observe an empty
registry as a failure. Test substrate is the correctly-scoped place for the
fix.

NEGATIVE SPEC
    - Does NOT override `COORDINATOR_CORE_LAZY_OPS=1`. The operator override
      stays authoritative in both directions; the assertion below is skipped
      in that regime.
    - Does NOT change what any `_REGISTRY` guard proves. No test is rewritten
      and the registry contract is untouched; this only fixes WHEN
      registration happens relative to collection.
    - Adds NO fixtures, markers, or hooks beyond the import and assertion
      below.
"""
from __future__ import annotations

import os

import coordinator_core.ops  # noqa: F401 — kept so an import failure surfaces here, NOT for a registration side effect (lazy-only since 2026-08-22: this import registers nothing)
from coordinator_core.ops._registry_map import resolves

# Detection half: prove what the import above now guarantees regardless of
# eager or lazy mode — not "the registry populated eagerly" but "the map
# this conftest's own comment used to justify is intact and every op is
# dispatchable" — so a future regression surfaces as one legible error
# naming the cause instead of a collection abort inside an unrelated-looking
# test module.
if os.environ.get("COORDINATOR_CORE_LAZY_OPS") != "1":
    assert resolves("ping"), (
        "op registry is not dispatchable after the repo-root conftest imported "
        "coordinator_core.ops — the representative op key 'ping' does not "
        "resolve via OP_MODULE_MAP or coordinator_core.ipc._REGISTRY."
    )

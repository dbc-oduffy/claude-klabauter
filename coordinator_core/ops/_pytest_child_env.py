"""
coordinator_core.ops._pytest_child_env — environment for an op-spawned pytest child.

Purpose: engine-side counterpart to `coordinator/bin/lib/cc_invoke.py`'s
`child_env()`, for the op modules that spawn a pytest run as a subprocess.
`cc_invoke` lives in `coordinator/bin/lib`, which `coordinator_core` must not
import (DR-047 engine/contract split), so the strip is reimplemented here for
the engine layer rather than shared.

Why a pytest child needs its own env at all: `COORDINATOR_CORE_LAZY_OPS=1`
makes `import coordinator_core.ops` skip its eager per-module import list, and
59 test modules in this tree assert the op registry at import time — a pytest
process that inherits the flag fails collection outright on a green tree.

Stripped UNCONDITIONALLY, not only when this process set it: a pytest run is
never a `coordinator_core.invoke` dispatch, so lazy op registration is
incompatible with it under any provenance, an operator's own export included.
This mirrors the same unconditional-strip ruling already made for the nested
collect in `coordinator/bin/tests/test_zero_test_module_ratchet.py`
(`_NESTED_PYTEST_ENV_SCRUB`), and deliberately diverges from `child_env()`,
whose subject is an arbitrary child rather than a pytest run.

Since 2026-07-28 the in-process lazy-ops channel is `sys._coordinator_core_lazy_ops`
rather than `os.environ`, so the flag can no longer reach a child implicitly and
this helper is belt-and-braces against an explicit operator export — kept
because that export is a real, supported path.
"""

from __future__ import annotations

import os

LAZY_OPS_ENV_KEY = "COORDINATOR_CORE_LAZY_OPS"


def pytest_child_env(overrides: dict[str, str] | None = None) -> dict[str, str]:
    """Return an `os.environ` copy safe to hand a spawned pytest run as `env=`.

    `overrides`, if given, is applied on top (last-write-wins) after the strip.
    """
    env = dict(os.environ)
    env.pop(LAZY_OPS_ENV_KEY, None)
    if overrides:
        env.update(overrides)
    return env

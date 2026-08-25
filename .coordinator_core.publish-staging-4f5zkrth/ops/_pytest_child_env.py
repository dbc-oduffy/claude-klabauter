"""
coordinator_core.ops._pytest_child_env — environment for an op-spawned pytest child.

Purpose: engine-side counterpart to `coordinator/bin/lib/cc_invoke.py`'s
`child_env()`, for the op modules that spawn a pytest run as a subprocess.
`cc_invoke` lives in `coordinator/bin/lib`, which `coordinator_core` must not
import (DR-047 engine/contract split), so this stays a separate module rather
than importing across that boundary.

Retired the `COORDINATOR_CORE_LAZY_OPS` strip this module used to perform
(`import-path-costs-nothing` sprint, C8): lazy op registration is
unconditional now (C6) — nothing reads either the env var or the retired
`sys._coordinator_core_lazy_ops` in-process channel any more, so an inherited
value, from any provenance including an operator's own export, has zero
effect on collection. The leak this module existed to defend against is gone
because the thing it would have leaked no longer does anything.

Kept as a named seam — `pytest_child_env()` stays the one place an
op-spawned pytest child's env is built — so a future isolation need has
somewhere to land without re-deriving `dict(os.environ)` at each of the three
call sites by hand.
"""

from __future__ import annotations

import os


def pytest_child_env(overrides: dict[str, str] | None = None) -> dict[str, str]:
    """Return an `os.environ` copy safe to hand a spawned pytest run as `env=`.

    `overrides`, if given, is applied on top (last-write-wins).
    """
    env = dict(os.environ)
    if overrides:
        env.update(overrides)
    return env

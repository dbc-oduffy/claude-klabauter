"""test_priority_set_bootstrap_setdefault.py -- regression coverage for the
lazy-bootstrap clobber defect a 2026-08-29 staff-eng review found across
`coordinator/bin/*.py`'s `_bootstrap_imports()` shape.

Prior to the fix, `_bootstrap_imports()` guarded on `all(n in globals() for
n in _BOOTSTRAP_NAMES)` (correct) but then rebound EVERY name in
`_BOOTSTRAP_NAMES` via a bare `global ...` + `from ... import ...` (wrong):
a caller that had pre-bound exactly one bootstrapped name (e.g.
`mod.cc_invoke = stub`, the shape this file's own test suite and
`priority-set.py`'s `__getattr__` docstring both describe) had that stub
silently destroyed the moment ANY other bootstrapped name was still
missing, because the all-names guard correctly fell through to the rebind
branch and the rebind branch did not distinguish "already bound" from
"needs binding" per name.

This module asserts the fixed behaviour on `priority-set.py`, used here as
one representative door for the whole `_bootstrap_imports()` family
(`goal-close-day.py`, `set-goal-kr-status.py`, `query-handoff-columns.py`,
`reap-integrated-review-findings.py`, `reap-orphaned-in-flight-handoffs.py`,
`reap-sessions.py` share the identical shape and were fixed identically,
each via a `globals().setdefault(...)` publish loop instead of a bare
`global` rebind).

Negative-spec: does NOT assert anything about `priority-set.py`'s cwd
identity gate (covered by `test_priority_set_no_cwd_gate.py`) -- this
module is scoped to the bootstrap-clobber defect only.
"""

from __future__ import annotations

import importlib.util
import os
import sys
import unittest

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_BIN_DIR = os.path.dirname(_TESTS_DIR)
_LIB_DIR = os.path.join(_BIN_DIR, "lib")
if _BIN_DIR not in sys.path:
    sys.path.insert(0, _BIN_DIR)
if _LIB_DIR not in sys.path:
    sys.path.insert(0, _LIB_DIR)


def _load_module():
    path = os.path.join(_BIN_DIR, "priority-set.py")
    spec = importlib.util.spec_from_file_location(
        "bootstrap_setdefault_priority_set", path
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestBootstrapImportsDoesNotClobberAPatchedName(unittest.TestCase):
    def test_one_prebound_name_survives_the_others_being_bound(self):
        """A caller that pre-binds exactly one of `_BOOTSTRAP_NAMES` (the
        `mock.patch.object(mod, "cc_invoke", ...)` shape) must see that
        stub survive `_bootstrap_imports()` running to bind the STILL-
        missing names -- not get silently rebound to the real import."""
        module = _load_module()

        # Fresh module load: none of _BOOTSTRAP_NAMES are bound yet (the
        # module body itself stays inert -- C6k import-motion).
        for name in module._BOOTSTRAP_NAMES:
            self.assertNotIn(
                name,
                vars(module),
                f"{name!r} unexpectedly pre-bound by module exec alone",
            )

        sentinel = object()
        module.cc_invoke = sentinel

        module._bootstrap_imports()

        self.assertIs(
            module.cc_invoke,
            sentinel,
            "_bootstrap_imports() clobbered a name the caller had already "
            "bound, even though another _BOOTSTRAP_NAMES member was still "
            "missing at call time",
        )
        self.assertIn("mutation_refusal_message", vars(module))
        self.assertIn("resolve_checked_repo_root", vars(module))
        self.assertIsNot(module.mutation_refusal_message, sentinel)
        self.assertIsNot(module.resolve_checked_repo_root, sentinel)


if __name__ == "__main__":
    unittest.main()

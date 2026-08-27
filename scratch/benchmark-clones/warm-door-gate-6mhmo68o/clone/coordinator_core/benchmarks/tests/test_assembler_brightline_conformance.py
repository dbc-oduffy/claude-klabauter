"""
coordinator_core.benchmarks.tests.test_assembler_brightline_conformance --
the inert-on-landing property for C10's/C11's spine and sprint assembler
ops (chunk C13, docs/plans/2026-08-21-engine-half-of-the-roadmap-sprint-
spine-split.md).

Purpose: C10 (`roadmap_planning_assemble`) and C11 (`sprint_planning_assemble`)
each self-certify their own AC2/AC3/AC4 -- no module-scope
`coordinator_core.ops` import, <=200ms end-to-end, <=2.0 procs/call -- in
their own chunk's test file (test_roadmap_planning_assemble.py,
test_sprint_planning_assemble.py) against the real CLI. This module does
NOT re-assert those; duplicating that ownership would gate the brightline
property on publish (C12) rather than at build time, catching a
regression only after an outward-facing event. This module's only job is
the property neither C10 nor C11 can self-certify from inside its own
module: that landing either assembler in-tree wires up NO in-tree caller.
Per the source memo's own "Sequencing" section
(cross-repo/inbox/2026-08-20-doe-claude-em-roadmap-sprint-split-assembler-
ops.md): "your delivery is effectively inert on landing... no live caller
exists yet." This module is the structural proof of that claim, run at
build time so a regression that prematurely wires one of these two
assemblers up is caught here, not in DoE's tree after C12 publishes.

NARROWED 2026-08-25, after this pin and ceremony-sweep-05's package-registration
guard landed the same day pulling opposite ways. That guard requires every
`brief()`-defining package to have a phantom-sweep provider; this one read any
mention of an assembler's name, in any `.py` under `coordinator_core/`, as a
caller. A `brief()`-defining assembler could satisfy one or the other, never both.

Neither invariant lost, because they were never opposed on substance. "Inert on
landing" means no caller that INVOKES the assembler as part of the system's own
work before C12 publishes. A pytest-only harness reading the shape of `brief()`'s
output is not one, and could not make the op live if it tried -- which
`test_neither_assembler_is_a_registered_ipc_op` asserts directly and independently.
So the sweep now skips test files and the named harness, and `test_the_sweep_still
_catches_a_production_caller` proves it did not go blind in the process.

Negative-spec:
    - Do NOT widen the exemptions further without the same argument. A file is
      exempt only if it cannot invoke the assembler as production work; "it is
      inconvenient that this is red" is not that argument, and a NON-test module
      that calls `brief()` is exactly what this module exists to catch.
    - Do NOT re-assert AC2/AC3/AC4 (module-scope-import / process-time /
      spawn-count) here -- C10's and C11's own test files already own and
      gate those against the real CLI; this module owns the disjoint,
      only-provable-from-outside property (no in-tree caller exists yet).
    - Do NOT roll a new timer or gate on wall clock if timing is ever
      needed here -- reuse `benchmarks/process_time.py::batched_process_time_ms`,
      amortised over K invocations (Windows job-object accounting
      quantises to the ~15.6ms scheduler tick; a single sample near a
      bar measures tick noise, not cost), and verify every sample's `rc`
      is 0 before trusting a timing (an erroring batch reads as a valid
      fast measurement otherwise). This module currently needs no timing
      at all -- the inert-on-landing property is a structural (grep +
      registry) assertion, not a perf one -- but the constraint is
      documented here so a future addition to this file does not roll a
      parallel timer.
"""
from __future__ import annotations

import os
import sys
import unittest

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_ENGINE_ROOT = os.path.abspath(os.path.join(_THIS_DIR, "..", "..", ".."))
if _ENGINE_ROOT not in sys.path:
    sys.path.insert(0, _ENGINE_ROOT)

_CORE_ROOT = os.path.join(_ENGINE_ROOT, "coordinator_core")

# Both hyphen (CLI/op-name) and underscore (Python import path) spellings --
# a caller can reference either shape.
_ASSEMBLER_NAMES = (
    "roadmap-planning-assemble",
    "roadmap_planning_assemble",
    "sprint-planning-assemble",
    "sprint_planning_assemble",
)

# A module referencing its own name (docstrings, its own CLI arg-parsing
# prog string, its own test file) is a definition, not a caller -- exempt
# by directory, not by individual file, so a new file added to either
# assembler's own tree does not need this list maintained.
_EXEMPT_DIR_MARKERS = (
    os.path.join("coordinator_core", "roadmap_planning_assemble"),
    os.path.join("coordinator_core", "sprint_planning_assemble"),
)

# This test module itself names both assemblers (docstring, constants) to
# assert their absence elsewhere -- naming them here is the check, not a
# call to them, so this file is exempt from its own sweep.
_EXEMPT_FILES = (
    os.path.abspath(__file__),
    # ceremony_common's phantom-sweep harness. It calls each `brief()`-defining
    # package's `brief()` to introspect the directive/judgment-point shapes it
    # emits; ceremony-sweep-05's package-registration guards
    # (`test_every_discovered_package_is_registered_or_allowlisted`) require
    # EVERY such package to have a provider, so a `brief()`-defining package
    # landing in-tree cannot satisfy both that guard and a sweep that treats any
    # mention as a caller.
    #
    # The two guards read as opposed and are not: the property this module was
    # written to protect (module docstring) is that landing an assembler wires up
    # no caller that would INVOKE it as part of the system's own work before
    # C12 publishes. A test harness reading its output shape is not that -- it
    # runs only under pytest, reaches no production path, and cannot make the op
    # live. `test_neither_assembler_is_a_registered_ipc_op` below is the assertion
    # that actually carries "not live", and it is untouched and still green.
    #
    # Narrowed rather than deleted, per the recurring failure this repo keeps
    # paying for: the pin was on WHERE the name appears, not on WHAT would make
    # the assembler live.
    os.path.abspath(
        os.path.join(_CORE_ROOT, "ceremony_common", "_phantom_sweep_providers.py")
    ),
)


def _is_test_file(fname: str) -> bool:
    """A test file naming an assembler is asserting about it, not calling it as
    part of the system's own work -- and a test that DID prematurely wire one up
    still could not make it live, which is what
    `test_neither_assembler_is_a_registered_ipc_op` proves directly."""
    return (
        fname.startswith("test_")
        or fname.endswith("_test.py")
        or fname == "conftest.py"
    )


def _is_swept(path: str, rel: str, fname: str) -> bool:
    """Whether one file is in the sweep's scope. Split out from the walk so the
    planted-violation test can exercise the same predicate the sweep uses,
    rather than a re-spelling of it that could drift green."""
    if not fname.endswith(".py") or _is_test_file(fname):
        return False
    if os.path.abspath(path) in _EXEMPT_FILES:
        return False
    return not any(marker in rel for marker in _EXEMPT_DIR_MARKERS)


def _names_an_assembler(content: str) -> list[str]:
    return [name for name in _ASSEMBLER_NAMES if name in content]


def _iter_in_tree_python_files():
    """Every .py file under coordinator_core/, excluding each assembler's
    own directory (its own definition, never a caller of itself), test files
    and the named phantom-sweep harness (module docstring, NARROWED), this
    test module itself, and __pycache__ (compiled artifacts, not
    source)."""
    for root, _dirs, files in os.walk(_CORE_ROOT):
        if "__pycache__" in root:
            continue
        for fname in files:
            path = os.path.join(root, fname)
            rel = os.path.relpath(path, _ENGINE_ROOT)
            if _is_swept(path, rel, fname):
                yield path, rel


class TestInertOnLanding(unittest.TestCase):
    """This chunk's own AC: no in-tree caller references either C10's
    (`roadmap-planning-assemble`) or C11's (`sprint-planning-assemble`) op
    -- landing them in-tree must wire up nothing that would invoke them,
    so a regression that prematurely calls one is caught at build time,
    before C12's publish step, not after."""

    def test_the_sweep_still_catches_a_production_caller(self):
        """The narrowing is worthless if it went blind. A plain module under
        `coordinator_core/` that imports an assembler is still in scope and still
        reads as an offender; the phantom-sweep harness and a test file are not."""
        production = os.path.join(_CORE_ROOT, "ops", "some_new_op.py")
        self.assertTrue(
            _is_swept(production, os.path.relpath(production, _ENGINE_ROOT), "some_new_op.py")
        )
        self.assertEqual(
            _names_an_assembler(
                "from coordinator_core import roadmap_planning_assemble as rpa\n"
            ),
            ["roadmap_planning_assemble"],
        )

        harness = os.path.join(_CORE_ROOT, "ceremony_common", "_phantom_sweep_providers.py")
        self.assertFalse(
            _is_swept(harness, os.path.relpath(harness, _ENGINE_ROOT), "_phantom_sweep_providers.py")
        )
        a_test = os.path.join(_CORE_ROOT, "ops", "test_something.py")
        self.assertFalse(
            _is_swept(a_test, os.path.relpath(a_test, _ENGINE_ROOT), "test_something.py")
        )

    def test_no_in_tree_caller_references_either_assembler(self):
        offenders = []
        for path, rel in _iter_in_tree_python_files():
            try:
                content = open(path, encoding="utf-8").read()
            except (OSError, UnicodeDecodeError):
                continue
            for name in _names_an_assembler(content):
                offenders.append((rel, name))
        self.assertEqual(
            offenders,
            [],
            msg=(
                "in-tree caller(s) reference a C10/C11 assembler -- landing "
                "either assembler must wire up NO in-tree caller until "
                f"C12's publish step does so intentionally: {offenders!r}"
            ),
        )

    def test_neither_assembler_is_a_registered_ipc_op(self):
        # Force full eager registration first -- a lazy OP_MODULE_MAP miss
        # must not hide a registration that only a real dispatch would
        # otherwise trigger.
        import coordinator_core.ops as ops_pkg  # noqa: PLC0415

        if hasattr(ops_pkg, "_eager_import_all"):
            ops_pkg._eager_import_all()

        from coordinator_core.ipc import _REGISTRY  # noqa: PLC0415

        for name in ("roadmap-planning-assemble", "sprint-planning-assemble"):
            self.assertNotIn(
                name,
                _REGISTRY,
                msg=(
                    f"{name!r} must not be a registered IPC op -- it is a "
                    "hand-built CLI (C10/C11's own AC2 shape), never "
                    "register_op'd"
                ),
            )


if __name__ == "__main__":
    unittest.main()

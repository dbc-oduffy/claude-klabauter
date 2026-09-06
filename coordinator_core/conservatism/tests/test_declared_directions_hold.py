"""
Per-site assertions that each declared safe direction actually holds, plus the
meta-test that refuses a declaring site with no assertion.

## Specimen census (the five real sites this primitive was generalised from)

MIGRATED (declaration + assertion below):
  1. `concurrency_probe.evaluate_escape_hatch` -- FALL_BACK to a refusal.
  2. `concurrency_probe.default_physical_cores` -- FALL_BACK to os.cpu_count().
  3. `concurrency_probe.default_usable_ram_gb` -- RAISE.

DECLARED AS-IS, deliberately not migrated:
  4. `concurrency_probe.compute_parallelism_cap` and `validate_levels` raise on
     INVALID ARGUMENTS, which is a different thing from an undeterminable
     precondition: the caller supplied the wrong value and can see that it did.
     Decorating them would stretch the vocabulary to cover ordinary argument
     validation and make the declaration mean nothing.
  5. `DoE-claude coordinator/hooks/scripts/_posture.py` is coordinator-claude's
     file and may not be edited from here (this baton's anti-scope). Its
     discipline is pinned behaviourally by `test_posture_fail_direction.py`.

`cloud-em-03` and `cloud-em-04` were expected to add two more specimens. This
baton generalised from the five that existed then rather than waiting, because
it gates `cloud-em-02`.

  6. ADDED by `cloud-em-04`: `install.derive_worker_cap.cap_command_for_this_box`
     -- FALL_BACK to the command as committed, asserted in its own test file
     (see `_ASSERTED_IN_SIBLING_TESTS`).
     It is the first declaring site outside `concurrency_probe`, which is why
     the roster below scans a tuple of modules rather than one.
"""

from __future__ import annotations

import builtins
import contextlib
import importlib
import os

import pytest

from coordinator_core.benchmarks import concurrency_probe as cp
from coordinator_core.install import derive_worker_cap as dwc
from coordinator_core.conservatism import (
    SafeDirection,
    declares_safe_direction,
    iter_declarations,
)
from coordinator_core.conservatism.verify import assert_safe_direction_holds


@contextlib.contextmanager
def _no_psutil():
    """Make `import psutil` fail for the duration of the block -- the exact
    precondition both psutil-backed specimens cannot determine."""
    real_import = builtins.__import__

    def _blocked(name, *args, **kwargs):
        if name == "psutil" or name.startswith("psutil."):
            raise ImportError("psutil blocked by test")
        return real_import(name, *args, **kwargs)

    builtins.__import__ = _blocked
    try:
        yield
    finally:
        builtins.__import__ = real_import


def test_default_usable_ram_gb_raises_as_declared():
    pytest.importorskip("psutil", reason="the control run needs a real reading to be distinguishable")
    assert_safe_direction_holds(
        cp.default_usable_ram_gb,
        invoke=cp.default_usable_ram_gb,
        undeterminable=_no_psutil,
        # Without this, the RAISE branch accepts ANY exception raised inside
        # `_no_psutil()` as proof the site refused -- including one from a
        # broken fixture, which would read as compliance.
        expect_raises=ImportError,
    )


def test_default_physical_cores_falls_back_as_declared():
    assert_safe_direction_holds(
        cp.default_physical_cores,
        invoke=cp.default_physical_cores,
        undeterminable=_no_psutil,
        # control=False, with the reason the helper demands: on a box without SMT
        # psutil's physical count EQUALS os.cpu_count(), so the intact path is
        # indistinguishable from the anchor and the control run would fail for a
        # property of the hardware rather than a property of the code.
        control=False,
    )


def _readable_state() -> cp.MachineState:
    return cp.MachineState(readable=True, free_ram_gb=64.0, cpu_percent=10.0, process_count=10, error=None)


def _unreadable_state() -> cp.MachineState:
    return cp.MachineState(readable=False, free_ram_gb=None, cpu_percent=None, process_count=None, error="probe failed")


def test_evaluate_escape_hatch_refuses_on_unreadable_state_as_declared():
    state = {"value": _readable_state()}

    @contextlib.contextmanager
    def _unreadable():
        state["value"] = _unreadable_state()
        try:
            yield
        finally:
            state["value"] = _readable_state()

    assert_safe_direction_holds(
        cp.evaluate_escape_hatch,
        invoke=lambda: cp.evaluate_escape_hatch(
            state["value"], min_free_ram_gb=1.0, max_cpu_percent=95.0, max_process_count=10_000
        ),
        undeterminable=_unreadable,
    )


# Review: code-reviewer -- the per-site tests above assert each site's
# CURRENT `declaration.direction`, whatever it is; they do not pin what that
# direction should be. A decorator edit flipping e.g. `default_usable_ram_gb`
# from RAISE to FALL_BACK would leave its per-site test green under a now-
# lying name. `test_both_directions_are_expressible` below is the only place
# that independently derives "both directions are still represented" from
# the live declarations, decoupled from any one site's test.
_ASSERTED_SITES = {
    "coordinator_core.benchmarks.concurrency_probe.default_usable_ram_gb",
    "coordinator_core.benchmarks.concurrency_probe.default_physical_cores",
    "coordinator_core.benchmarks.concurrency_probe.evaluate_escape_hatch",
}

#: Sites whose assertion lives with the site rather than here, named so the
#: meta-test still refuses an unasserted declaration. A site belongs here only
#: when its `undeterminable` context needs fixtures this file has no business
#: owning -- keep the assertion beside the code, not the roster.
#:
#: Values are `(module, attrname)` pairs, not strings: resolved below at
#: import time, so a renamed-away sibling assertion fails the collection of
#: THIS file rather than satisfying the meta-test while asserting nothing
#: (`state/lessons/2026-09-01-killed-op-names-live-on-in-string-keyed-guards.md`).
_ASSERTED_IN_SIBLING_TESTS = {
    "coordinator_core.install.derive_worker_cap.cap_command_for_this_box": (
        "coordinator_core.install.test_derive_worker_cap",
        "test_an_unreadable_host_keeps_the_committed_ceiling",
    ),
}

for _site, (_sibling_module, _sibling_attr) in _ASSERTED_IN_SIBLING_TESTS.items():
    getattr(importlib.import_module(_sibling_module), _sibling_attr)
del _site, _sibling_module, _sibling_attr

#: Modules scanned for declarations. A declaring module absent from this tuple
#: is invisible to the meta-test below -- which is the failure this roster
#: exists to prevent, so add the module in the same commit as the declaration.
_DECLARING_MODULES = (cp, dwc)


def test_every_declaring_site_has_an_assertion():
    """A declaration nobody asserts is the convention this module replaced,
    with extra steps. New declaring site -> new test above, then this set."""
    declared = {
        name for module in _DECLARING_MODULES for name, _ in iter_declarations(module)
    }
    asserted = _ASSERTED_SITES | set(_ASSERTED_IN_SIBLING_TESTS)
    assert declared - asserted == set(), (
        f"declaring sites with no assertion: {sorted(declared - asserted)}"
    )
    assert asserted - declared == set(), (
        f"asserted sites that no longer declare a direction: {sorted(asserted - declared)}"
    )


def test_both_directions_are_expressible():
    """Survives a second review that deleted it as "redundant with the
    per-site tests" (Review: code-reviewer). It is not redundant: the
    per-site tests each assert whatever direction its site CURRENTLY
    declares, so a regression that flips the corpus's last RAISE to
    FALL_BACK (or vice versa) leaves every per-site test green under a name
    that now lies about which branch it exercises. This test is the only
    place that independently derives, from the live declarations, that both
    directions are still represented at all -- do not delete it a third
    time."""
    directions = {declaration.direction for _, declaration in iter_declarations(cp)}
    assert directions == {SafeDirection.RAISE, SafeDirection.FALL_BACK}


def test_an_imported_declaration_is_not_attributed_to_the_importer():
    """`derive_worker_cap` imports two declaring helpers from the probe. They
    are the probe's sites, asserted once, above -- attributing them here would
    demand a second assertion for the same function in every consumer."""
    names = {name for name, _ in iter_declarations(dwc)}
    assert names == {"coordinator_core.install.derive_worker_cap.cap_command_for_this_box"}


@pytest.mark.parametrize(
    "kwargs, message",
    [
        ({"direction": SafeDirection.FALL_BACK, "because": "x"}, "anchor"),
        ({"direction": SafeDirection.FALL_BACK, "because": "x", "anchor": 1}, "anchor"),
        ({"direction": SafeDirection.RAISE, "because": "x", "anchor": 1}, "anchor"),
        ({"direction": SafeDirection.RAISE, "because": "  "}, "because"),
    ],
)
def test_malformed_declarations_are_refused_at_import(kwargs, message):
    with pytest.raises(ValueError, match=message):
        declares_safe_direction(**kwargs)


def test_verify_catches_a_site_that_lies_about_its_direction():
    """The instrument must fail as well as pass -- a declaration that does not
    hold has to produce a red, or none of the tests above mean anything."""

    @declares_safe_direction(SafeDirection.RAISE, because="claims to refuse, does not")
    def liar():
        return 0

    broken = {"value": False}

    @contextlib.contextmanager
    def _break():
        broken["value"] = True
        try:
            yield
        finally:
            broken["value"] = False

    with pytest.raises(AssertionError, match="declares SafeDirection.RAISE"):
        assert_safe_direction_holds(liar, invoke=lambda: 0, undeterminable=_break)

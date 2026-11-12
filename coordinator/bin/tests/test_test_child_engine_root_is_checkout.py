"""Ratchet: a coordinator/bin wrapper a test spawns with `sys.executable` and no engine-root pin
of its own loads `coordinator_core` from the checkout under test, never the published engine
root (the `.claude-klabauter-root` pointer target), so a test child cannot silently run stale code.
"""

from __future__ import annotations

import pytest

from coordinator_core.testing.checkout_engine_probe import (
    assert_inherited_pin_child_resolves_checkout,
    assert_unpinned_child_loads_core_from_checkout,
)

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def test_unpinned_test_child_loads_core_from_checkout(tmp_path):
    assert_unpinned_child_loads_core_from_checkout(tmp_path)


def test_inherited_harness_pin_child_resolves_checkout_dispatch_root(tmp_path):
    """The shape `test_coordinator_safe_commit_k3_failure_path._wrapper` spawns: the env the
    root conftest left in `os.environ`, with no pin added by the test."""
    assert_inherited_pin_child_resolves_checkout(tmp_path)

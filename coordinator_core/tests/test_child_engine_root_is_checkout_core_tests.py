"""Ratchet for the coordinator_core/tests tree: spawned coordinator/bin wrappers resolve the checkout engine."""

from __future__ import annotations

import pytest

from coordinator_core.testing.checkout_engine_probe import (
    assert_inherited_pin_child_resolves_checkout,
    assert_unpinned_child_loads_core_from_checkout,
)

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def test_unpinned_test_child_loads_core_from_checkout(tmp_path):
    assert_unpinned_child_loads_core_from_checkout(tmp_path, may_refuse_without_published_engine=True)


def test_inherited_harness_pin_child_resolves_checkout_dispatch_root(tmp_path):
    assert_inherited_pin_child_resolves_checkout(tmp_path)

"""Pins provider_registry: exactly-one resolution, no heuristic pick."""

import pytest

from coordinator_core import provider_registry as pr


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.setattr(pr, "_PROVIDERS", {})


def test_single_candidate_returned():
    pr.register_provider("k", "a", 1)
    pr.register_provider("k", "b", 2)
    assert pr.resolve("k", lambda p: p == 2) == 2


def test_zero_candidates_raises():
    pr.register_provider("k", "a", 1)
    with pytest.raises(pr.NoProviderError):
        pr.resolve("k", lambda p: False)
    with pytest.raises(pr.NoProviderError):
        pr.resolve("unknown", lambda p: True)


def test_multiple_candidates_raise_with_sorted_names():
    for name in ("zeta", "alpha", "mid"):
        pr.register_provider("k", name, name)
    with pytest.raises(pr.AmbiguousProviderError) as ei:
        pr.resolve("k", lambda p: True)
    assert ei.value.names == ["alpha", "mid", "zeta"]


def test_duplicate_name_raises():
    pr.register_provider("k", "a", 1)
    with pytest.raises(pr.DuplicateProviderError):
        pr.register_provider("k", "a", 2)


def test_same_name_different_kind_ok():
    pr.register_provider("k1", "a", 1)
    pr.register_provider("k2", "a", 2)
    assert pr.resolve("k2", lambda p: True) == 2


def test_registration_order_independent():
    def run(order):
        pr._PROVIDERS.clear()
        for n in order:
            pr.register_provider("k", n, n)
        with pytest.raises(pr.AmbiguousProviderError) as ei:
            pr.resolve("k", lambda p: True)
        return ei.value.names

    assert run(["b", "a", "c"]) == run(["c", "b", "a"]) == ["a", "b", "c"]

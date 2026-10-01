
import pytest

from coordinator_core.testing.fleet_pin import assume_a_fleet_machine  # noqa: F401


@pytest.fixture(autouse=True)
def _author_machine_profile(monkeypatch):
    """Pin guard_level strict: this package's tests assert the strict (deny)
    behaviour; warn/off tests override the level explicitly."""
    from coordinator_core import machine_profile

    monkeypatch.setenv("MACHINE_LOCAL_COORDINATOR_GUARD_LEVEL", "strict")
    monkeypatch.setenv("MACHINE_LOCAL_COORDINATOR_FEATURE_DOCTRINE_EDIT_GATE", "on")
    machine_profile.reset_cache()
    yield
    machine_profile.reset_cache()

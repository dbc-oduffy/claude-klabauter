import pytest


@pytest.fixture(autouse=True)
def _pin_guard_level_strict(monkeypatch):
    """Hook tests assert the strict (deny) behaviour; warn/off tests override."""
    from coordinator_core import machine_profile

    monkeypatch.setenv("MACHINE_LOCAL_COORDINATOR_GUARD_LEVEL", "strict")
    machine_profile.reset_cache()
    yield
    machine_profile.reset_cache()

import pytest

from coordinator_core.group_em import nomination


@pytest.fixture(autouse=True)
def _low_disk_probe_is_silent(request, monkeypatch):
    """`poll_once` emits LOW-DISK on a real low-disk box; steady-state tests assert no emission.
    test_watch_low_disk drives the real probe against a faked disk."""
    if request.module.__name__.endswith("test_watch_low_disk"):
        return
    from coordinator_core.group_em import watch

    monkeypatch.setattr(watch, "_low_disk_tick", lambda now_epoch, emit: None)


@pytest.fixture(autouse=True)
def _claimant_session_is_real(monkeypatch):
    """`claim` refuses a sid the harness doesn't know; fixture sids are made up. The refusal
    itself is pinned in test_nomination, which restores the real check per test."""
    monkeypatch.setattr(nomination, "_session_is_real", lambda sid: True)

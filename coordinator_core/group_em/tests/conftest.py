import pytest

from coordinator_core.group_em import nomination


@pytest.fixture(autouse=True)
def _claimant_session_is_real(monkeypatch):
    """`claim` refuses a sid the harness doesn't know; fixture sids are made up. The refusal
    itself is pinned in test_nomination, which restores the real check per test."""
    monkeypatch.setattr(nomination, "_session_is_real", lambda sid: True)

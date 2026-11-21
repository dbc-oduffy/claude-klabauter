"""Hook tests mint runs whose driver session is recorded from the ambient session variable; clear it so a fixture's run names no driver."""

import pytest


@pytest.fixture(autouse=True)
def _no_ambient_driver_session(monkeypatch):
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)

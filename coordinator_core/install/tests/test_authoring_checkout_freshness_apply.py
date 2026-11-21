"""Apply-path tests for authoring-checkout freshness over tmp_path bare-origin + clone fixtures."""

import os

import pytest

from coordinator_core.install import authoring_checkout_freshness_apply as apply
from coordinator_core.warm import live_runs

pytestmark = pytest.mark.complete

_ENV = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.invalid",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.invalid",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_SYSTEM": os.devnull,
}


@pytest.fixture(autouse=True)
def _no_live_runs(monkeypatch):
    monkeypatch.setattr(live_runs, "find_live_runs", lambda repos=None, **kw: [])


def test_unreadable_checkout_is_a_row(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    result = apply.apply_all({"repos.plain": str(plain)})
    assert result["rows"][0]["action"] == "unreadable"
    assert "repos.plain" in result["advisories"][0]

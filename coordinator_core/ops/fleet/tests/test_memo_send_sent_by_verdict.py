"""
Purpose: pin the P03-C2 verdict — `memo_send._resolve_sent_by` is already
warm-scoped through `ops.session_context.resolve_current_session_id`, so a
warm-served send with no carried identity never stamps the server owner's
env id into `sent_by`.
"""
from __future__ import annotations

import pytest

from coordinator_core.ops.fleet import memo_send
from coordinator_core.session import core as session_core

_ENV_SID = "11111111-1111-4111-8111-111111111111"
_CARRIED_SID = "22222222-2222-4222-8222-222222222222"


@pytest.fixture(autouse=True)
def _env_names_a_stranger(monkeypatch):
    for name in session_core.SESSION_ENV_PRECEDENCE:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", _ENV_SID)


def test_warm_without_carried_identity_never_reads_env():
    with session_core.warm_served_request():
        assert memo_send._resolve_sent_by({}) == memo_send._SENT_BY_UNRESOLVED


def test_warm_with_carried_identity_uses_it():
    with session_core.warm_served_request(), session_core.session_identity_override(
        _CARRIED_SID
    ):
        assert memo_send._resolve_sent_by({}) == _CARRIED_SID


def test_cold_resolves_from_env():
    assert memo_send._resolve_sent_by({}) == _ENV_SID


def test_draft_sent_by_is_threaded_through_even_warm():
    with session_core.warm_served_request():
        assert memo_send._resolve_sent_by({"sent_by": "draft-sid"}) == "draft-sid"

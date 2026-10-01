"""Veneer tests for "session.incident_claim" and "session.incident_peers"."""

from __future__ import annotations

import pytest

from coordinator_core.ipc import CallerFacingValidationError
from coordinator_core.ops import session_incident_claim as op
from coordinator_core.session import incident_claims as ic


def _holder(key="k", sid="s1", note="n"):
    return ic.IncidentHolder(key=key, session_id=sid, note=note, claimed_at="t", address="a", is_self=False)


def test_claim_shape(monkeypatch):
    monkeypatch.setattr(
        ic, "set_claim",
        lambda r, k, n=None: ic.IncidentClaimResult(key="k", own_session_id="me", peers=[_holder()]),
    )
    out = op._session_incident_claim({"repo_root": "/r", "key": "K", "note": "x"})
    assert out == {
        "key": "k", "own_session_id": "me",
        "peers": [{"session_id": "s1", "note": "n", "claimed_at": "t", "address": "a"}],
    }


def test_claim_bad_key_is_caller_facing_error(monkeypatch):
    def boom(r, k, n=None):
        raise ValueError("bad key")
    monkeypatch.setattr(ic, "set_claim", boom)
    with pytest.raises(CallerFacingValidationError):
        op._session_incident_claim({"repo_root": "/r", "key": "!"})


def test_claim_missing_params_raise():
    with pytest.raises(CallerFacingValidationError):
        op._session_incident_claim({})


def test_release(monkeypatch):
    monkeypatch.setattr(ic, "normalize_key", lambda k: "k")
    monkeypatch.setattr(ic, "release_claim", lambda r, k: True)
    monkeypatch.setattr(ic.core, "resolve_session_id", lambda r: "me")
    out = op._session_incident_claim({"repo_root": "/r", "key": "K", "release": True})
    assert out["released"] is True and out["peers"] == []


def test_peers_shape(monkeypatch):
    monkeypatch.setattr(ic, "list_peers", lambda r, k=None: [_holder()])
    out = op._session_incident_claim.__globals__["_session_incident_peers"]({"repo_root": "/r"})
    assert out["error"] is None
    assert out["peers"][0]["key"] == "k"


def test_peers_degrades_on_bad_params(monkeypatch):
    assert op._session_incident_peers({})["peers"] == []
    assert op._session_incident_peers({})["error"]

    def boom(r, k=None):
        raise ValueError("bad key")
    monkeypatch.setattr(ic, "list_peers", boom)
    out = op._session_incident_peers({"repo_root": "/r", "key": "!"})
    assert out["peers"] == [] and out["error"] == "bad key"

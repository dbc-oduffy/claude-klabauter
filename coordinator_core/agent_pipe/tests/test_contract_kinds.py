"""Grok vendor kind, login-required cause, provenance envelope and session-driver value types."""
from __future__ import annotations

import json

import pytest

from coordinator_core.agent_pipe import contract, session_driver


def test_kinds_include_grok_but_agents_roster_does_not():
    assert contract.KIND_GROK == "grok"
    assert "grok" in contract.KINDS
    assert "grok" not in contract.AGENTS


def test_classify_exit_login_required_for_not_logged_in_event():
    ev = json.dumps({"type": "error", "message": "Not logged in. Run `grok login`."})
    assert contract.classify_exit(ev) == contract.CAUSE_LOGIN_REQUIRED
    assert contract.classify_exit("401 Unauthenticated") == "login_required"


def test_classify_exit_quota_for_429_free_usage_exhausted():
    ev = json.dumps({"type": "error", "error": {"message": "429 free-usage-exhausted"}})
    assert contract.classify_exit(ev) == contract.CAUSE_QUOTA


def test_quota_wins_over_login_text():
    assert contract.classify_exit("429 usage limit reached, please log in or login required") == "quota_exhausted"


def test_existing_causes_unchanged():
    assert contract.classify_exit("no upgrade reply") == contract.CAUSE_HANDSHAKE_TIMEOUT
    assert contract.classify_exit("turn timed out") == contract.CAUSE_TURN_TIMEOUT
    assert contract.classify_exit("all fine") is None


def test_provenance_envelope_shape_and_none_dropped():
    env = contract.provenance_envelope("grok", "~/.grok/sessions", origin="agent-initiated", ts="t", cli_version="1.2", session="s1")
    assert env == {
        "from_identity": "s1", "from_kind": "grok", "origin": "agent-initiated", "ts": "t",
        "vendor": "grok", "source": "~/.grok/sessions", "cli_version": "1.2",
    }
    assert contract.provenance_envelope("grok", "src") == {"from_kind": "grok", "vendor": "grok", "source": "src"}
    with pytest.raises(ValueError):
        contract.provenance_envelope("gemini", "src")


def test_session_driver_value_types_are_frozen():
    h = session_driver.SessionHandle(session_id="s", cwd="/c")
    with pytest.raises(Exception):
        h.session_id = "x"  # type: ignore[misc]
    req = session_driver.PermissionRequest(tool="bash", options=(session_driver.ALLOW_ONCE, session_driver.REJECT_ONCE))
    assert session_driver.Decision(option_id=session_driver.REJECT_ONCE).option_id in req.options
    assert session_driver.Update(kind="message", text="hi").raw == {}
    assert {session_driver.ALLOW_ONCE, session_driver.ALLOW_ALWAYS, session_driver.REJECT_ONCE} == {
        "allow_once", "allow_always", "reject_once",
    }

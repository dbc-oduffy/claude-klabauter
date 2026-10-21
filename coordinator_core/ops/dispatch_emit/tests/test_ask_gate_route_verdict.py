"""dispatch.ask_gate replies route_verdict on every non-refusal verdict of a named-route preference, and none otherwise."""

from __future__ import annotations

import pytest

from coordinator_core.ops.dispatch_emit import ask_gate, requirements_ledger, route_preference as rp, run_token
from coordinator_core.ops.dispatch_emit.ask_contract import record_dir
from coordinator_core.ops.dispatch_emit.tests.test_ask_gate import REL, _put, repo  # noqa: F401

RUN = "ask-20261021T000000-cccccc"
KEY = rp.ROUTE_VERDICT_KEY


@pytest.fixture(autouse=True)
def _no_other_binds(monkeypatch, repo):  # noqa: F811
    (repo / ".git").mkdir()
    monkeypatch.setattr(requirements_ledger, "bind", lambda *a, **k: None)
    monkeypatch.setattr(run_token, "bind_sizing", lambda *a, **k: None)


def _pref(repo, preference, rationale="because"):  # noqa: F811
    d = record_dir(repo, RUN)
    d.mkdir(parents=True, exist_ok=True)
    (d / rp.ROUTE_PREFERENCE_FILE).write_text(rp.to_json(rp.parse(preference, rationale)), encoding="utf-8")


def _call(repo, **extra):  # noqa: F811
    return ask_gate._handler({"sizing_path": REL, "run_id": RUN, **extra}, repo_root=repo / ".git")


@pytest.mark.parametrize("preference", ["goal", "roadmap", "plan"])
@pytest.mark.parametrize("route", ["goal-setting", "roadmap", "plan"])
def test_named_route_preference_replies_a_verdict_on_every_non_refusal(repo, preference, route):  # noqa: F811
    _put(repo, route=route)
    _pref(repo, preference)
    reply = _call(repo, route_override_reason="stated")
    assert "route_override_reason" not in ((reply.get("halt") or {}).get("reason") or "")
    v = reply[KEY]
    assert v is not None and v["sized_route"] == route
    matched = rp.PREFERENCE_ROUTES[preference] == route
    assert v["verdict"] == (rp.VERDICT_INDEPENDENT if matched else rp.VERDICT_PUSHBACK)


def test_match_replies_independent_without_a_reason(repo):  # noqa: F811
    _put(repo, route="plan")
    _pref(repo, "plan")
    assert _call(repo)[KEY]["verdict"] == rp.VERDICT_INDEPENDENT


def test_divergence_replies_pushback(repo):  # noqa: F811
    _put(repo, route="plan")
    _pref(repo, "roadmap")
    assert _call(repo, route_override_reason="why")[KEY]["verdict"] == rp.VERDICT_PUSHBACK


def test_rationale_only_preference_replies_none(repo):  # noqa: F811
    _put(repo, route="plan")
    _pref(repo, None, "context only")
    assert KEY not in _call(repo)


def test_absent_preference_file_replies_none(repo):  # noqa: F811
    _put(repo, route="plan")
    assert KEY not in _call(repo)


def test_empty_sized_route_replies_none(repo):  # noqa: F811
    _put(repo, route="")
    _pref(repo, "plan")
    assert KEY not in _call(repo, route_override_reason="x")


def test_unexplained_divergence_is_a_refusal_with_no_verdict(repo):  # noqa: F811
    _put(repo, route="plan")
    _pref(repo, "roadmap")
    reply = _call(repo)
    assert reply["halt"]["kind"] == "refusal" and KEY not in reply

"""ask_gate records the run's EM route preference on the sizing and refuses an unexplained divergence."""

from __future__ import annotations

import pytest
import yaml

from coordinator_core.ops.dispatch_emit import ask_gate, requirements_ledger, route_preference as rp, run_token
from coordinator_core.ops.dispatch_emit.ask_contract import record_dir
from coordinator_core.ops.dispatch_emit.tests.test_ask_gate import REL, _put, repo  # noqa: F401

RUN = "ask-20261017T000000-bbbbbb"


@pytest.fixture(autouse=True)
def _no_other_binds(monkeypatch, repo):  # noqa: F811
    (repo / ".git").mkdir()
    monkeypatch.setattr(requirements_ledger, "bind", lambda *a, **k: None)
    monkeypatch.setattr(run_token, "bind_sizing", lambda *a, **k: None)


def _pref(repo, preference="roadmap", rationale="spans two workstreams"):
    d = record_dir(repo, RUN)
    d.mkdir(parents=True, exist_ok=True)
    (d / rp.ROUTE_PREFERENCE_FILE).write_text(
        rp.to_json(rp.parse(preference, rationale)), encoding="utf-8"
    )


def _call(repo, **extra):
    return ask_gate._handler({"sizing_path": REL, "run_id": RUN, **extra}, repo_root=repo / ".git")


def _override_refused(reply):
    return "route_override_reason" in ((reply.get("halt") or {}).get("reason") or "")


def _em(repo):
    return (yaml.safe_load((repo / REL).read_text(encoding="utf-8")) or {}).get("em_analysis") or {}


def test_matching_route_records_preference_without_reason(repo):  # noqa: F811
    _put(repo, route="plan")
    _pref(repo, "plan")
    reply = _call(repo)
    assert not _override_refused(reply)
    em = _em(repo)
    assert em["route_preference"] == "plan" and em["em_rationale"] == "spans two workstreams"
    assert "route_override_reason" not in em


def test_divergence_without_reason_halts_naming_the_param(repo):  # noqa: F811
    _put(repo, route="plan")
    _pref(repo, "roadmap")
    reply = _call(repo)
    assert reply["halt"]["kind"] == "refusal" and _override_refused(reply)
    assert "route_override_reason" not in _em(repo)


def test_divergence_with_reason_writes_it(repo):  # noqa: F811
    _put(repo, route="plan")
    _pref(repo, "roadmap")
    reply = _call(repo, route_override_reason="  one workstream only  ")
    assert not _override_refused(reply)
    em = _em(repo)
    assert em["route_override_reason"] == "one workstream only" and em["route_preference"] == "roadmap"


def test_reason_already_on_the_sizing_satisfies_the_gate(repo):  # noqa: F811
    _put(repo, route="plan", em_analysis={"route_override_reason": "earlier call"})
    _pref(repo, "roadmap")
    reply = _call(repo)
    assert not _override_refused(reply)
    assert _em(repo)["route_override_reason"] == "earlier call"


def test_repeat_call_writes_nothing(repo):  # noqa: F811
    _put(repo, route="plan")
    _pref(repo, "roadmap")
    _call(repo, route_override_reason="why")
    before = (repo / REL).read_bytes()
    mtime = (repo / REL).stat().st_mtime_ns
    _call(repo, route_override_reason="why")
    assert (repo / REL).read_bytes() == before and (repo / REL).stat().st_mtime_ns == mtime


def test_no_record_file_leaves_sizing_untouched(repo):  # noqa: F811
    _put(repo, route="plan")
    before = (repo / REL).read_bytes()
    _call(repo, route_override_reason="ignored")
    assert (repo / REL).read_bytes() == before


def test_rationale_only_preference_needs_no_reason(repo):  # noqa: F811
    _put(repo, route="plan")
    _pref(repo, None, "just context")
    reply = _call(repo)
    assert not _override_refused(reply)
    assert _em(repo) == {"em_rationale": "just context"}

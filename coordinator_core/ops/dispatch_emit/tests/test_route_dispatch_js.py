"""_routedRun: classifier compatibility of the refusal line, literal dispatch, and node-run behaviour."""

from __future__ import annotations

import json
import re
import shutil
import subprocess

import pytest

from coordinator_core.ops.dispatch_emit import emit
from coordinator_core.ops.dispatch_emit.route_dispatch_js import (
    REFUSED_PREFIX,
    literal_dispatch_js,
    route_object_literal,
    routed_run_js,
)
from coordinator_core.win_portability import no_console_creationflags


pytestmark = [pytest.mark.cadence]


def test_refusal_line_is_blocked_and_not_a_stop_rule():
    line = REFUSED_PREFIX + " (x): y"
    blocked = re.compile(emit._BLOCKED_STATUS_JS_RE[1:].rsplit("/", 1)[0].replace("<\\/", "</"), re.M)
    stop = re.compile(emit._STOP_RULE_JS_RE[1:-1])
    assert blocked.search(line)
    assert not stop.search(line)


def test_literal_dispatch_emits_one_agent_type_literal_per_target():
    js = literal_dispatch_js({"a:x": "m1", "b:y": "m2"}, lambda t, m: f"agent(p + extra, {{ agentType: '{t}', model: '{m}' }})")
    assert js.count("agentType: 'a:x'") == 1 and js.count("agentType: 'b:y'") == 1
    assert js.startswith("(t, extra) => t === 'a:x' ?") and js.endswith(": null")


def test_route_object_literal_escapes_values():
    lit = route_object_literal({"workClass": "it's", "triage": None, "primary": "p:q", "targets": ["p:q"]})
    assert "workClass: 'it\\'s'" in lit and "triage: null" in lit


_NODE = shutil.which("node")


def _run(route: dict, replies: dict) -> dict:
    program = (
        routed_run_js()
        + "\nconst _calls = [], _logs = [];\nconst log = (s) => _logs.push(s);\n"
        + f"const _replies = {json.dumps(replies)};\n"
        + "const _seen = {};\n"
        + "const dispatch = async (t, extra) => { _calls.push([t, extra]); const q = _replies[t]; "
        + "const i = _seen[t] = (_seen[t] || 0) + 1; return q[Math.min(i, q.length) - 1]; };\n"
        + f"_routedRun({json.dumps(route)}, dispatch).then(r => console.log(JSON.stringify({{r, calls: _calls, logs: _logs}})));\n"
    )
    out = subprocess.run(
        [_NODE, "-"], input=program, capture_output=True, text=True, check=True, **no_console_creationflags()
    ).stdout
    return json.loads(out)


_ROUTE = {"workClass": "ue", "triage": None, "primary": "h:ue-a", "targets": ["h:ue-a", "h:ue-b"]}
_TRIAGE = {**_ROUTE, "triage": "h:triage", "targets": ["h:triage", "h:ue-a", "h:ue-b"]}

node = pytest.mark.skipif(_NODE is None, reason="node absent")


@node
@pytest.mark.spawns_process
def test_proceed_returns_reply_unchanged():
    out = _run(_ROUTE, {"h:ue-a": ["disposition: proceed\nDONE"]})
    assert out["r"] == "disposition: proceed\nDONE" and [c[0] for c in out["calls"]] == ["h:ue-a"]


@node
@pytest.mark.spawns_process
def test_missing_disposition_proceeds_and_logs():
    out = _run(_ROUTE, {"h:ue-a": ["DONE"]})
    assert out["r"] == "DONE" and len(out["logs"]) == 1 and "h:ue-a" in out["logs"][0]


@node
@pytest.mark.spawns_process
def test_refuse_becomes_blocked_with_reason():
    out = _run(_ROUTE, {"h:ue-a": ["disposition: refuse\nreason: not editor work"]})
    assert out["r"] == "BLOCKED: route-refused (ue): not editor work"


@node
@pytest.mark.spawns_process
def test_one_admissible_redirect_dispatches_the_target():
    out = _run(_ROUTE, {
        "h:ue-a": ["disposition: redirect\nredirect_agent_type: h:ue-b\nreason: r"],
        "h:ue-b": ["disposition: proceed\nok"],
    })
    assert [c[0] for c in out["calls"]] == ["h:ue-a", "h:ue-b"] and out["r"].endswith("ok")


@node
@pytest.mark.spawns_process
def test_second_redirect_is_refused():
    out = _run(_ROUTE, {
        "h:ue-a": ["disposition: redirect\nredirect_agent_type: h:ue-b"],
        "h:ue-b": ["disposition: redirect\nredirect_agent_type: h:ue-a"],
    })
    assert out["r"].startswith("BLOCKED: route-refused (ue):") and len(out["calls"]) == 2


@node
@pytest.mark.spawns_process
def test_redirect_outside_targets_is_refused():
    out = _run(_ROUTE, {"h:ue-a": ["disposition: redirect\nredirect_agent_type: evil:agent"]})
    assert out["r"] == "BLOCKED: route-refused (ue): redirect target evil:agent cannot take this class"
    assert len(out["calls"]) == 1


@node
@pytest.mark.spawns_process
def test_triage_pick_then_the_primary_own_redirect_is_allowed_once():
    out = _run(_TRIAGE, {
        "h:triage": ["disposition: redirect\nredirect_agent_type: h:ue-a"],
        "h:ue-a": ["disposition: redirect\nredirect_agent_type: h:ue-b"],
        "h:ue-b": ["disposition: proceed\ndone"],
    })
    assert [c[0] for c in out["calls"]] == ["h:triage", "h:ue-a", "h:ue-b"]
    assert "h:ue-a, h:ue-b" in out["calls"][0][1] and out["calls"][1][1] == "" and out["r"].endswith("done")


@node
@pytest.mark.spawns_process
def test_triage_pick_then_two_redirects_is_refused():
    out = _run(_TRIAGE, {
        "h:triage": ["disposition: redirect\nredirect_agent_type: h:ue-a"],
        "h:ue-a": ["disposition: redirect\nredirect_agent_type: h:ue-b"],
        "h:ue-b": ["disposition: redirect\nredirect_agent_type: h:ue-a"],
    })
    assert out["r"].startswith("BLOCKED: route-refused (ue):") and len(out["calls"]) == 3


@node
@pytest.mark.spawns_process
def test_triage_proceed_dispatches_the_primary_and_returns_its_reply():
    out = _run(_TRIAGE, {
        "h:triage": ["disposition: proceed\nassessed"],
        "h:ue-a": ["primary work done"],
    })
    assert [c[0] for c in out["calls"]] == ["h:triage", "h:ue-a"]
    assert out["calls"][1][1] == "" and out["r"] == "primary work done"


@node
@pytest.mark.spawns_process
def test_triage_without_disposition_proceeds_to_the_primary_and_logs():
    out = _run(_TRIAGE, {"h:triage": ["no disposition here"], "h:ue-a": ["primary done"]})
    assert [c[0] for c in out["calls"]] == ["h:triage", "h:ue-a"]
    assert out["r"] == "primary done" and "proceeding to the primary h:ue-a" in out["logs"][0]


@node
@pytest.mark.spawns_process
def test_triage_proceed_then_one_primary_redirect_is_allowed():
    out = _run(_TRIAGE, {
        "h:triage": ["disposition: proceed"],
        "h:ue-a": ["disposition: redirect\nredirect_agent_type: h:ue-b"],
        "h:ue-b": ["disposition: proceed\nfinal"],
    })
    assert [c[0] for c in out["calls"]] == ["h:triage", "h:ue-a", "h:ue-b"] and out["r"].endswith("final")


@node
@pytest.mark.spawns_process
def test_triage_proceed_then_two_primary_redirects_is_refused():
    out = _run(_TRIAGE, {
        "h:triage": ["disposition: proceed"],
        "h:ue-a": ["disposition: redirect\nredirect_agent_type: h:ue-b"],
        "h:ue-b": ["disposition: redirect\nredirect_agent_type: h:ue-a"],
    })
    assert out["r"].startswith("BLOCKED: route-refused (ue):") and len(out["calls"]) == 3

"""A launcher child's M+/roadmap chain hand-off always carries the emitted script `path` or an `emit_error`.

The gate agent's `chain_emit` relay can be absent; the hand-off then retries the `chain-emit` agent once rather
than returning a bare stub with no path (run ask-20261110T114506-e8fa0f).
"""

from __future__ import annotations

import functools
import json
import re
import shutil
import subprocess

import pytest

from coordinator_core.ops.dispatch_emit.ask_compose import compose_ask_script
from coordinator_core.ops.dispatch_emit.nested_run_contract import CHAIN_HANDOFF_KEY
from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW
from coordinator_core.ops.dispatch_emit.tests import test_launcher_child_m_plus_chains_incident as launcher

pytestmark = pytest.mark.complete

_NO_EMIT = "the gate agent returned no emit reply for the chained run"
_RETRY_LINE = re.compile(r"const _retry = await agent\(.*?\n(?=\s*if \(_good\(_retry\)\))", re.DOTALL)


@functools.lru_cache(maxsize=1)
def _composed() -> tuple[str, tuple[str, ...]]:
    script = compose_ask_script(
        repo_root="REPO", prompt="add a thing", sizing_rel=None, run_id="run-1", session_id=None,
        wrap_stage=lambda text: ("", []), plan_blitz_text="stub", launcher_child=True, **REVIEW_KW,
    )
    decl = re.search(r"const _launcherRelayTail = async \(\) => \{.*?return \{ emit_error: [^\n]*\n\s*\};\n", script, re.DOTALL)
    assert decl, "launcher child composed no relay tail"
    retry = _RETRY_LINE.search(decl.group(0))
    assert retry and "label: 'chain-emit'" in retry.group(0), "the relay tail has no chain-emit retry"
    legs = tuple(
        leg for leg in re.findall(r"return \{ chain: [^\n]*?, sizing: _sizingRel, run_id: _runId \};", script)
        if "_launcherRelayTail" in leg
    )
    assert len(legs) == 2, legs
    return _RETRY_LINE.sub("const _retry = await _stub();\n", decl.group(0)), legs


def handoff_legs() -> tuple[str, ...]:
    """The composed M+ and roadmap hand-off `return` statements of a launcher child."""
    return _composed()[1]


def run_leg(leg: str, relay, retry) -> tuple[dict, int]:
    """Evaluate one hand-off leg in node with the gate relay `relay` and a stub chain-emit agent answering `retry`.

    Returns the hand-off and how many times the chain-emit agent ran. `{"__throw": True}` makes the stub throw."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not available")
    code = (
        "const _sizingRel = 'state/sizings/s.yaml', _SESSION_ID = 's', REPO_ROOT = '/r', _runId = 'ask-r1';\n"
        f"const _crossRepoApproved = false; const _gate = {{ arm: 'm_plus', chain_emit: {json.dumps(relay)} }};\n"
        f"const RETRY = {json.dumps(retry)}; let calls = 0; let _viaRoadmap = false;\n"
        "const _stub = async () => { calls++; if (RETRY && RETRY.__throw) throw new Error('boom'); return RETRY; };\n"
        + _composed()[0]
        + f"\n(async () => {{ const r = await (async () => {{ {leg} }})();"
        " console.log(JSON.stringify({ r, calls })); })();\n"
    )
    out = subprocess.run([node, "-e", code], capture_output=True, text=True, timeout=30,
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    assert out.returncode == 0, out.stderr
    parsed = json.loads(out.stdout)
    return parsed["r"][CHAIN_HANDOFF_KEY], parsed["calls"]


_GOOD = {"path": "scratch/warp/ask-r2.workflow.mjs", "run_id": "ask-r2", "fire_args": {}}


@pytest.mark.spawns_process
@pytest.mark.parametrize("relay", [None, {"error": "not run: scout never emitted"}, {"path": "", "run_id": ""}, ["a"]],
                         ids=["null", "errored", "empty-path", "array"])
def test_a_relay_without_a_path_is_retried_and_the_hand_off_carries_the_retry_path(relay):
    for leg in handoff_legs():
        handoff, calls = run_leg(leg, relay, _GOOD)
        assert calls == 1
        assert handoff["path"] == _GOOD["path"] and handoff["run_id"] == "ask-r2"
        assert "error" not in handoff and "emit_error" not in handoff


@pytest.mark.spawns_process
@pytest.mark.parametrize("retry,expected", [
    ({"path": "", "run_id": "", "error": "denied"}, "denied"),
    ({"path": "", "run_id": ""}, _NO_EMIT),
    (None, _NO_EMIT),
    ({"__throw": True}, "boom"),
], ids=["error-reply", "empty-reply", "null-reply", "throws"])
def test_a_failed_retry_carries_emit_error_and_no_path_or_error(retry, expected):
    for leg in handoff_legs():
        handoff, calls = run_leg(leg, None, retry)
        assert calls == 1
        assert expected in handoff["emit_error"]
        assert "path" not in handoff and "error" not in handoff


@pytest.mark.spawns_process
def test_the_relay_error_is_the_emit_error_when_the_retry_names_none():
    for leg in handoff_legs():
        handoff, _ = run_leg(leg, {"error": "not run: x"}, None)
        assert handoff["emit_error"] == "not run: x"


@pytest.mark.spawns_process
def test_a_good_relay_is_kept_without_a_retry_and_loses_its_error_key():
    relay = {**_GOOD, "error": ""}
    for leg in handoff_legs():
        handoff, calls = run_leg(leg, relay, {"error": "must not be called"})
        assert calls == 0
        assert handoff["path"] == _GOOD["path"] and "error" not in handoff and "emit_error" not in handoff


@pytest.mark.spawns_process
def test_the_launcher_self_emits_for_a_hand_off_carrying_only_emit_error(tmp_path):
    handoff = {"ask": True, "sizing_path": launcher._SIZING, "session_id": "s", "target_root": "/r", "emit_error": "denied"}
    out = launcher._drive(
        launcher._script(tmp_path),
        [{CHAIN_HANDOFF_KEY: handoff, "sizing": launcher._SIZING, "run_id": "ask-r1"}, {"route": "dispatch", "done": True}],
        launcher._good_chain(tmp_path),
    )
    assert out["r"]["status"] == "ok", out["r"]
    assert [e["label"] for e in out["emits"]] == ["chain-emit-ask-r1"]
    assert "emit_error" not in out["emits"][0]["prompt"]

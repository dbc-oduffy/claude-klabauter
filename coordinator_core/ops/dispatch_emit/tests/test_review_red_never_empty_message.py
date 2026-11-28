"""A failing test in review, and land_run's unmeasured-gate advisory, never render with an empty cause."""
from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from coordinator_core.ops.dispatch_emit import ask_compose, land_run
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

FALLBACK = "no failure message recorded"
NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="node absent")

BLANK_DETAILS = {
    "empty": "[{ node_id: 'a::t1', message: '' }]",
    "missing": "[{ node_id: 'a::t1' }]",
    "whitespace": "[{ node_id: 'a::t1', message: '  \\n ' }]",
    "null": "[{ node_id: 'a::t1', message: null }]",
}


def _node(program: str) -> object:
    out = subprocess.run(
        [NODE, "-"], input=program, capture_output=True, text=True, check=True, **no_console_creationflags()
    ).stdout.strip()
    return json.loads(out)


def _cause(line: str) -> str:
    return line.split(": ", 1)[1] if ": " in line else ""


_BLOCKERS = """
const _src = { status_after: 'fail', failing: ['a::t1'], failing_detail: %s };
%s
console.log(JSON.stringify({ failing: _xFailing, gaps: _xGaps }));
"""


@needs_node
@pytest.mark.parametrize("detail", BLANK_DETAILS.values(), ids=BLANK_DETAILS.keys())
def test_a_blocker_gap_with_a_blank_message_renders_a_named_fallback(detail):
    out = _node(_BLOCKERS % (detail, ask_compose._test_blockers_js("_src", "_x")))

    assert len(out["gaps"]) == 1
    assert _cause(out["gaps"][0]).strip(), out
    assert FALLBACK in out["gaps"][0]


@needs_node
def test_a_blocker_gap_with_a_real_message_keeps_its_first_line_and_no_fallback():
    out = _node(
        _BLOCKERS % ("[{ node_id: 'a::t1', message: 'AssertionError: 1 != 2\\nsecond line' }]",
                     ask_compose._test_blockers_js("_src", "_x"))
    )

    assert out["gaps"] == ["a::t1: AssertionError: 1 != 2"]


_RECOVER = """
const _hfPosix = (p) => String(p).replace(/\\\\/g, '/');
const classifyReds = (failing) => ({ owned: failing.map(_hfPosix), foreign: [], unverified: [] });
const rv = { failing_detail: %s, pre_existing: [], passed_at_base: [] };
const failing = ['a::t1'];
const _manifest = { review_declared_paths: [] };
%s
console.log(JSON.stringify({ ownedTxt }));
"""


def _recover_render_js() -> str:
    js = ask_compose._REVIEW_RECOVER_JS
    start = js.index("      const detail = new Map();")
    return js[start:js.index("      const slices = ", start)]


@needs_node
@pytest.mark.parametrize("detail", BLANK_DETAILS.values(), ids=BLANK_DETAILS.keys())
def test_an_owned_red_line_in_review_recovery_carries_a_non_empty_cause(detail):
    out = _node(_RECOVER % (detail, _recover_render_js()))

    assert len(out["ownedTxt"]) == 1
    assert _cause(out["ownedTxt"][0]).strip(), out
    assert FALLBACK in out["ownedTxt"][0]


@needs_node
def test_an_owned_red_line_with_a_real_message_keeps_it():
    out = _node(_RECOVER % ("[{ node_id: 'a::t1', message: 'boom\\nmore' }]", _recover_render_js()))

    assert out["ownedTxt"] == ["a::t1: boom"]


def _gate(monkeypatch, tmp_path, result: dict) -> dict:
    from coordinator_core.ops import test_leg_run
    from coordinator_core.ops.dispatch_emit import clean_overlay_verify as cov

    monkeypatch.setattr(land_run, "_on_warm_request_path", lambda: False)
    monkeypatch.setattr(cov, "_commit_sha", lambda *_a, **_k: "a" * 40)
    monkeypatch.setattr(land_run, "_tests_could_run", lambda *_a, **_k: True)
    monkeypatch.setattr(land_run, "_post_land_commit", lambda *_a, **_k: "b" * 40)
    monkeypatch.setattr(test_leg_run, "run", lambda *_a, **_k: result)
    return land_run._new_red_gate(tmp_path, ["pkg/mod.py"], [], {})


def _unmeasured(result: dict) -> str:
    return result["advisory"].split("new-red gate unmeasured: ", 1)[1]


@pytest.mark.parametrize(
    "detail",
    [
        [{"node_id": "a::t1", "message": ""}],
        [{"node_id": "a::t1"}],
        [{"node_id": "a::t1", "message": None}],
        [{"node_id": "a::t1", "message": "  \n "}],
        [],
    ],
    ids=["empty", "missing", "null", "whitespace", "no-detail"],
)
def test_an_unmeasured_advisory_with_no_message_names_a_fallback(monkeypatch, tmp_path, detail):
    out = _gate(monkeypatch, tmp_path, {"status_after": "fail", "failing_detail": detail})

    assert _unmeasured(out).strip() and "None" not in out["advisory"]
    assert FALLBACK in out["advisory"]


def test_an_unmeasured_advisory_joins_only_the_real_messages(monkeypatch, tmp_path):
    detail = [
        {"node_id": "a::t1", "message": ""},
        {"node_id": "a::t2", "message": "real failure"},
        {"node_id": "a::t3", "message": None},
    ]

    out = _gate(monkeypatch, tmp_path, {"status_after": "fail", "failing_detail": detail})

    assert _unmeasured(out) == "real failure"


def test_an_unmeasured_advisory_prefers_the_summary(monkeypatch, tmp_path):
    out = _gate(monkeypatch, tmp_path, {"over_budget": True, "summary": "timeout: 540s", "failing_detail": []})

    assert _unmeasured(out) == "timeout: 540s"

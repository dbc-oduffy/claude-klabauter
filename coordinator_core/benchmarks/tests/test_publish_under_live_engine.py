"""The falsifier harness's parser and counters, driven on recorded reply streams (no pair started)."""
from __future__ import annotations

import json
import math

from coordinator_core.benchmarks import publish_under_live_engine as h
from coordinator_core.warm import client

_IND = {"jsonrpc": "2.0", "id": 1, "error": {"code": -32004, "message": "warm dispatch indeterminate"}}
_OK_HEALTH = {"jsonrpc": "2.0", "id": 1, "result": {"sid": "x"}}
_OK_GUARD = {"jsonrpc": "2.0", "id": 1, "result": {"stdout": "{\"permissionDecision\":\"allow\"}", "exit_code": 0}}
_OPEN_GUARD = {"jsonrpc": "2.0", "id": 1, "result": {
    "stdout": "coordinator: engine failed (x); guard not evaluated, command allowed."}}

_PUBLISH_OUT = """noise
  [timing] per-phase elapsed:
  [timing]   row: run_pre_sync_gates: 1.000s wall / 0.500s cpu (driver only, excludes subprocesses)
  [timing] round wall time: 12.500s; attributed: 1.000s; unattributed: 11.500s
  [timing] round driver cpu: 3.250s; attributed to phases: 0.500s (subprocess cpu is in neither)
"""


def _stream(*pairs):
    return [json.dumps({"probe": p, "reply": r}) for p, r in pairs]


def test_constant_matches_the_warm_client():
    assert h.MINUS_32004 == client.WARM_DISPATCH_INDETERMINATE


def test_counts_a_mixed_stream():
    recs = h.parse_reply_stream(_stream(
        ("health", _OK_HEALTH), ("guard", _OK_GUARD),
        ("health", _IND), ("guard", _IND),
        ("guard", None), ("guard", _OPEN_GUARD),
    ))
    assert h.count_probes(recs) == (2, 3)


def test_clean_stream_counts_zero_zero():
    recs = h.parse_reply_stream(_stream(("health", _OK_HEALTH), ("guard", _OK_GUARD)))
    assert h.count_probes(recs) == (0, 0)


def test_a_health_failure_is_not_a_fail_open():
    recs = h.parse_reply_stream(_stream(("health", None)))
    assert h.count_probes(recs) == (0, 0)


def test_parser_skips_blank_malformed_and_foreign_lines():
    lines = ["", "not json", json.dumps({"probe": "other", "reply": None}), json.dumps([1])]
    lines += _stream(("guard", _IND))
    assert len(h.parse_reply_stream(lines)) == 1


def test_report_pins_the_output_contract():
    recs = h.parse_reply_stream(_stream(("health", _IND), ("guard", _OPEN_GUARD)))
    out = h.render_report(recs, _PUBLISH_OUT).splitlines()
    assert out[0] == "minus_32004=1 fail_open=1 round_wall_s=12.500 round_cpu_s=3.250"
    assert out[1:] == [l.rstrip() for l in _PUBLISH_OUT.splitlines() if l.startswith("  [timing]")]


def test_harness_wall_overrides_the_driver_figure():
    out = h.render_report([], _PUBLISH_OUT, round_wall_s=99.0)
    assert out.splitlines()[0].startswith("minus_32004=0 fail_open=0 round_wall_s=99.000")


def test_missing_timing_lines_read_as_nan():
    assert math.isnan(h.parse_round_cpu_s("no timing here"))
    assert math.isnan(h.parse_round_wall_s("no timing here"))

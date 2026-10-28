"""component_sizes ranks parts by utf-8 bytes; cap_refusal names the largest; the probe prints JSON."""

from __future__ import annotations

import json

from coordinator_core.ops.dispatch_emit.compose_size import cap_refusal, component_sizes


def test_component_sizes_counts_utf8_bytes_descending():
    assert component_sizes({"a": "xx", "b": "é" * 3, "c": ""}) == [("b", 6), ("a", 2), ("c", 0)]


def test_cap_refusal_names_total_cap_and_largest_component():
    msg = cap_refusal(600, 524288, component_sizes({"base": "x" * 5, "blitz": "x" * 9}))
    assert msg == (
        "composed ask script is over the Workflow runner's 524288-byte cap: 600 bytes; "
        "largest inlined component blitz (9 bytes)"
    )


def test_cap_refusal_without_components_still_states_the_cap():
    assert cap_refusal(7, 5, []).endswith("5-byte cap: 7 bytes")


def test_probe_prints_json_and_exits_zero(capsys):
    import pytest

    from coordinator_core.ops.dispatch_emit import compose_size_probe
    from coordinator_core.warm.caller_context import resolve_caller_context

    if not resolve_caller_context().plugin_root:
        pytest.skip("plugin root does not resolve")
    assert compose_size_probe.main() == 0
    out = json.loads(capsys.readouterr().out)
    assert out["total"] > 0 and out["components"] and "pm_turns_mention_lines" in out

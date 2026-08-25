"""Tests for coordinator_core.ops.compute_layer_scaffold.op.

Discharges AC10's transport surface for chunk C4 of
docs/plans/2026-08-13-compute-layer-scaffolder.md: a thin JSON-RPC wrapper
over `emit.compose_producer_module` (mode=emit) and `check.score_fleet` /
`check.render_report` (mode=check), forwarding params and results verbatim.
"""

from __future__ import annotations

import pytest

from coordinator_core.ops.compute_layer_scaffold.op import _compute_layer_scaffold


def test_emit_mode_returns_module_text():
    result = _compute_layer_scaffold(
        {"mode": "emit", "skill_name": "example_assemble", "verbs": ["do_thing"]}
    )
    assert "module_text" in result
    assert "do_thing" in result["module_text"]
    assert "build_envelope" in result["module_text"]


def test_emit_mode_requires_skill_name():
    with pytest.raises(ValueError, match="skill_name"):
        _compute_layer_scaffold({"mode": "emit", "verbs": ["do_thing"]})


def test_emit_mode_requires_verbs():
    with pytest.raises(ValueError, match="verbs"):
        _compute_layer_scaffold({"mode": "emit", "skill_name": "example_assemble"})


def test_emit_mode_propagates_invalid_verb():
    from coordinator_core.ops.compute_layer_scaffold.emit import InvalidVerb

    with pytest.raises(InvalidVerb):
        _compute_layer_scaffold(
            {"mode": "emit", "skill_name": "example_assemble", "verbs": [""]}
        )


def test_check_mode_returns_rendered_report():
    result = _compute_layer_scaffold({"mode": "check"})
    assert "report" in result
    assert "Sub-shape B conformance:" in result["report"]
    assert "FLEET FINDING" in result["report"]


def test_check_mode_honors_explicit_modules_param():
    """Review: coordinator:code-reviewer (weak-test finding) — the prior
    assertion (`"1/1" in report or "0/1" in report`) is an OR that passes on
    either branch, so it only proves a fraction shape is present, never that
    the correct one is. Assert the specific per-clause fractions pickup_assemble
    actually scores when it is the sole explicit `modules` entry."""
    result = _compute_layer_scaffold({"mode": "check", "modules": ["pickup_assemble"]})
    assert "report" in result
    report = result["report"]
    assert "closed_cli_dispatch: 1/1" in report
    assert "execute_directives: 1/1" in report
    assert "build_envelope: 0/1" in report
    assert "no_local_emit: 0/1" in report
    assert "clean_on_all: 0/1" in report


def test_unknown_mode_raises_value_error():
    with pytest.raises(ValueError, match="mode"):
        _compute_layer_scaffold({"mode": "bogus"})


def test_missing_mode_raises_value_error():
    with pytest.raises(ValueError, match="mode"):
        _compute_layer_scaffold({})


def test_op_registered_under_compute_layer_scaffold():
    from coordinator_core.ipc import get_op_handler

    assert get_op_handler("compute_layer.scaffold") is not None

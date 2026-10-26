"""Per-arm x size composed ask scripts carry the criterion judge on every non-halted path and Kira on L and up."""

from __future__ import annotations

import json
import re

import pytest

from coordinator_core.ops.dispatch_emit.tests._reacher_leg_probe import (
    ARMS,
    SIZES,
    compose_cell,
    live_shaped_fragment,
    reacher_legs,
)

_SIZE_TERMS = ("tshirt", "'L'", "'XL'", "'XXL'")
_NON_SIZE_SKIPS = {
    "if (!_halted && (_gate.arm === 'xs' || _xsTail)) { _integrationNotRun = true; }",
    "if (!_halted && _noReviewPaths && !_xsTail && _gate.arm !== 'xs') { _integrationNotRun = true; }",
}


@pytest.mark.parametrize("arm", ARMS)
def test_judge_is_composed_and_gated_only_on_halt_and_manifest(monkeypatch, arm):
    legs = reacher_legs(compose_cell(monkeypatch, arm))
    assert legs["judge"], f"{arm}: verify agent lacks the judge brief"
    assert legs["verify_agents"] == 1
    gate = legs["judge_gate"]
    assert gate == "if (!_halted && _manifest && !_manifest.error) {"
    assert not any(t in gate for t in _SIZE_TERMS + ("_xsTail", "_noReviewPaths", "arm"))


_KIRA_GATE = re.compile(
    r"_integrationNotRun = !\(\(\(_gate\?\.tshirt\) == null \|\| (\[[^\]]*\])\.includes\(_gate\?\.tshirt\)\)\);"
)


def _kira_admits(script: str) -> list[str] | None:
    """Sizes the compiled integration gate admits; None when the stage carries no size gate."""
    m = _KIRA_GATE.search(script)
    return json.loads(m.group(1)) if m else None


@pytest.mark.parametrize("size", SIZES)
@pytest.mark.parametrize("arm", ARMS)
def test_kira_is_composed_and_its_size_gate_admits_l_and_up(monkeypatch, arm, size):
    script = compose_cell(monkeypatch, arm)
    legs = reacher_legs(script)
    assert legs["kira"]
    admits = _kira_admits(script)
    assert admits is not None, f"{arm}: integration stage lost its size gate"
    assert size in admits
    assert "M" not in admits and "S" not in admits
    # No skip statement may carry a size term: size-keyed exclusion lives only in the compiled admit test.
    for skip in legs["kira_skips"]:
        assert not any(t in skip for t in _SIZE_TERMS), skip


@pytest.mark.parametrize("arm", ARMS)
def test_kira_skips_are_the_known_non_size_set(monkeypatch, arm):
    skips = set(reacher_legs(compose_cell(monkeypatch, arm))["kira_skips"])
    assert skips == _NON_SIZE_SKIPS
    assert all(s.startswith("if (!_halted") for s in skips)


def test_kira_without_a_roster_floor_is_ungated_by_size(monkeypatch):
    script = compose_cell(monkeypatch, "m_plus", fragment=live_shaped_fragment(min_tshirt=None))
    assert reacher_legs(script)["kira"] and _kira_admits(script) is None


@pytest.mark.parametrize("arm", ARMS)
def test_roster_without_judge_or_kira_composes_neither(monkeypatch, arm):
    script = compose_cell(monkeypatch, arm, fragment=live_shaped_fragment(judge=False, kira=False))
    legs = reacher_legs(script)
    assert not legs["judge"]
    assert not legs["kira"]
    assert _kira_admits(script) is None

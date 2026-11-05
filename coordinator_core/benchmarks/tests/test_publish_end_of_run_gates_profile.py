"""`publish_end_of_run_gates_profile`: leg shape, verdict, drift probe, and the manifest budget entries.

Run: python -m pytest coordinator_core/benchmarks/tests/test_publish_end_of_run_gates_profile.py -q
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from coordinator_core.benchmarks import budget
from coordinator_core.benchmarks import publish_end_of_run_gates_profile as prof

_SOURCE = (
    "import os\n"
    'KEY = "repos.zz_probe_key"\n'
    'HOME = os.environ.get("ZZ_PROBE_HOME")\n'
)
_SECTION = {"hooks": ["substitute"], "substitute": [], "protected_literals": []}


class _Clock:
    """Scripted meter: each `now_ms` call returns the next reading."""

    def __init__(self, *readings: float) -> None:
        self._it = iter(readings)

    def now_ms(self) -> float:
        return next(self._it)


@pytest.fixture(scope="module")
def publish():
    return prof.load_publish_module()


def _tree(tmp_path):
    (tmp_path / "mod.py").write_text(_SOURCE, encoding="utf-8")
    return SimpleNamespace(name="row", source_dir=tmp_path)


def test_inject_rules_prepends_and_activates_hook_without_mutating():
    section = {"hooks": [], "substitute": [{"key": "a", "value": "b", "match_mode": "plain"}]}
    out = prof.inject_rules(section, [("x", "y")])
    assert out["substitute"][0] == {"key": "x", "value": "y", "match_mode": "plain"}
    assert len(out["substitute"]) == 2 and "substitute" in out["hooks"]
    assert section["hooks"] == [] and len(section["substitute"]) == 1


def test_manifest_carries_a_500ms_entry_per_gate():
    overrides = budget.load_manifest()["overrides"]
    for gate in prof.GATES:
        assert overrides[prof.BUDGET_KEYS[gate]]["target_ms"] == 500
        assert prof.bar_for(gate) == 500.0


def test_profile_gate_cold_then_warm_average():
    calls = []
    meter = _Clock(0.0, 300.0, 300.0, 310.0, 310.0, 350.0)
    p = prof.profile_gate(meter, "g", lambda: calls.append(1) or True, warm_repeat=3)
    assert len(calls) == 1 + 1 + 2
    assert p.cold.process_ms == 300.0
    assert p.warm.n == 3 and p.warm.process_ms == pytest.approx((10.0 + 40.0) / 3)
    assert p.warm_within_bar


def test_profile_gate_does_not_repeat_a_warm_call_over_the_ceiling():
    calls = []
    meter = _Clock(0.0, 5000.0, 5000.0, 12000.0)
    p = prof.profile_gate(meter, "g", lambda: calls.append(1) or True, warm_repeat=3)
    assert len(calls) == 2 and p.warm.n == 1
    assert p.warm.process_ms == 7000.0 and not p.warm_within_bar


def _profile(cold_ok=True, warm_ok=True, warm_ms=100.0, gate="argv_parity"):
    return prof.GateProfile(gate, prof.Leg(10.0, cold_ok), prof.Leg(warm_ms, warm_ok, 3))


def test_verdict_passes_only_when_every_gate_is_in_bar_and_ok():
    caught = prof.DriftProbe(caught={g: True for g in prof.LITERAL_GATES})
    assert prof.verdict([_profile()], caught) is True
    assert prof.verdict([_profile(warm_ms=501.0)], caught) is False
    assert prof.verdict([_profile(warm_ok=False)], caught) is False
    assert prof.verdict([_profile(cold_ok=False)], caught) is False


def test_verdict_fails_when_a_literal_gate_misses_the_drift():
    missed = prof.DriftProbe(caught={"repo_key_literal": True, "live_identifier": False})
    assert prof.verdict([_profile()], missed) is False
    assert prof.verdict([_profile()], prof.DriftProbe()) is False
    assert "DRIFT MISSED" in prof.render([_profile()], missed, "x")


def test_find_drift_rules_picks_a_repo_key_and_an_env_var(tmp_path):
    from coordinator_core.percolate import engine as pct_engine
    from coordinator_core.percolate import live_identifiers as live
    from coordinator_core.percolate import repo_key_literals as rkl

    found = prof.find_drift_rules([(_tree(tmp_path), _SECTION)], pct_engine, rkl, live)
    assert found is not None
    _target, section, picked = found
    assert picked == {"repo_key": "repos.zz_probe_key", "env_var": "ZZ_PROBE_HOME"}
    assert [r["key"] for r in section["substitute"]] == ["repos.zz_probe_key", "ZZ_PROBE_HOME"]


def test_find_drift_rules_skips_a_literal_the_row_protects(tmp_path):
    from coordinator_core.percolate import engine as pct_engine
    from coordinator_core.percolate import live_identifiers as live
    from coordinator_core.percolate import repo_key_literals as rkl

    protected = dict(_SECTION, protected_literals=["repos.zz_probe_key"])
    assert prof.find_drift_rules([(_tree(tmp_path), protected)], pct_engine, rkl, live) is None


def test_drift_probe_makes_both_literal_gates_false_and_the_clean_tree_passes(publish, tmp_path):
    target = _tree(tmp_path)
    ctx = SimpleNamespace(engine_claude_klabauter=object())
    clean = prof.gate_calls(publish, ctx, [(target, _SECTION)], tmp_path, tmp_path)
    assert prof._quiet(clean["repo_key_literal"])[0] is True
    assert prof._quiet(clean["live_identifier"])[0] is True

    probe = prof.drift_probe(publish, ctx, [(target, _SECTION)], prof._Meter())
    assert probe.caught == {"repo_key_literal": True, "live_identifier": True}
    assert probe.passed

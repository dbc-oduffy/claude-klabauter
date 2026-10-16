"""Both roadmap scripts end in one haiku commit leg carrying the fan-out; no waves, execute or workstream-complete leg."""

from __future__ import annotations

import pytest

from coordinator_core.ops._workflow_contract import Severity, run_checks
from coordinator_core.ops.dispatch_emit import leg_register as lr
from coordinator_core.ops.dispatch_emit.ask_contract import FANOUT_RECORD_PARAM
from coordinator_core.ops.dispatch_emit.tests.test_ask_compose import _compose as _compose_raw
from coordinator_core.ops.dispatch_emit.tests.test_ask_roadmap import _compose as _compose_standalone, _put, repo  # noqa: F401

_RB_TEXT = "export const meta = { name: 'rb', description: 'd', phases: [{ title: 'RB' }] };\nreturn {};\n"
_CUT = ("waves", "execute", "workstream-complete")


def _scripts(repo):
    _put(repo)
    return {
        "standalone": _compose_standalone(repo),
        "in-run": _compose_raw(roadmap_blitz_text=_RB_TEXT, in_run_commit=True),
    }


@pytest.mark.parametrize("which", ["standalone", "in-run"])
def test_no_waves_execute_or_workstream_complete_leg(repo, which):
    script = _scripts(repo)[which]
    labels = [leg.label for leg in lr.emitted_legs(script)]
    assert not set(labels) & set(_CUT), set(labels) & set(_CUT)
    assert "phase('execute')" not in script and "phase('workstream-complete')" not in script


@pytest.mark.parametrize("which", ["standalone", "in-run"])
def test_exactly_one_haiku_commit_leg_fires_the_fanout(repo, which):
    script = _scripts(repo)[which]
    commits = [leg for leg in lr.emitted_legs(script) if leg.label == "commit"]
    assert len(commits) == 1 and commits[0].model == "haiku"
    emitted = (
        f"{FANOUT_RECORD_PARAM}: {{ plans: _ready.map((r) => r.planPath), "
        "waves: Object.fromEntries(_ready.map((r) => [r.planPath, r.wave])) }"
    )
    assert script.count(emitted) == 1
    assert "dispatch.terminal_commit --params-file" in script
    assert "fanout: _commit?.fanout ?? null" in script


@pytest.mark.parametrize("which", ["standalone", "in-run"])
def test_the_commit_gate_carries_no_roadmap_exclusion(repo, which):
    script = _scripts(repo)[which]
    gates = [ln for ln in script.splitlines() if "_nextAction.kind === 'terminal_commit' && _RUN_TOKEN" in ln]
    assert len(gates) == 1 and "_viaRoadmap" not in gates[0]


@pytest.mark.parametrize("which", ["standalone", "in-run"])
def test_waves_come_from_the_stage_reply_and_a_missing_field_halts_by_name(repo, which):
    script = _scripts(repo)[which]
    assert "const _waves = [...(_rb?.stage?.waves ?? [])].sort((a, c) => a.index - c.index);" in script
    assert "roadmap-blitz stage reply carries no waves" in script
    assert script.index("_rb?.stage?.waves") < script.index("waveIndex: w.index")


def test_the_in_run_fan_out_survives_the_closeable_overwrite(repo):
    script = _scripts(repo)["in-run"]
    assert "const _roadmapFan = !!(_viaRoadmap && !_halted" in script
    assert "_nextAction = _roadmapFan ? _nextAction : _closeable ?" in script
    assert "const _closeable = !!(_roadmapFan || (" in script


@pytest.mark.parametrize("which", ["standalone", "in-run"])
def test_both_scripts_lint_clean(repo, which):
    assert not [f for f in run_checks(_scripts(repo)[which]) if f.severity is Severity.ERROR]

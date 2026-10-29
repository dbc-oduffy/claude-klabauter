"""Both roadmap scripts end in one haiku commit leg carrying the fan-out; no waves, execute or workstream-complete leg."""

from __future__ import annotations

from unittest import mock

import pytest

from coordinator_core.ops._workflow_contract import Severity, run_checks
from coordinator_core.ops.dispatch_emit import ask_compose
from coordinator_core.ops.dispatch_emit import leg_register as lr
from coordinator_core.ops.dispatch_emit.ask_contract import FANOUT_RECORD_PARAM
from coordinator_core.ops.dispatch_emit.tests.test_ask_compose import _compose as _compose_raw
from coordinator_core.ops.dispatch_emit.tests.test_ask_roadmap import _compose as _compose_standalone, _put, repo  # noqa: F401

_RB_TEXT = "export const meta = { name: 'rb', description: 'd', phases: [{ title: 'RB' }] };\nreturn {};\n"
_CUT = ("waves", "execute", "workstream-complete")


def _compose_in_run(**over):
    """The in-run roadmap leg rides only a script that embeds plan-blitz, i.e. an M+ re-fire."""
    with mock.patch.object(ask_compose, "_known_arm", lambda *_: ask_compose.ARM_M_PLUS):
        return _compose_raw(**over)


def _scripts(repo):
    _put(repo)
    return {
        "standalone": _compose_standalone(repo),
        "in-run": _compose_in_run(roadmap_blitz_text=_RB_TEXT, in_run_commit=True),
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
    head = (
        f"{FANOUT_RECORD_PARAM}: {{ plans: _ready.map((r) => r.planPath), "
        "waves: Object.fromEntries(_ready.map((r) => [r.planPath, r.wave])), "
    )
    tail = "parent_sizing: _sizingRel, not_fired: _unfired }"
    spec = script[script.index(head):]
    spec = spec[: spec.index(tail) + len(tail)]
    assert script.count(head) == 1 and spec.count("interaction_mode") <= 1
    assert "dispatch.land_run --params-file" in script
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


_SCRIPT_PATH = "scratch/warp/r1.mjs"


def _scripts_with_path(repo):
    _put(repo)
    return {
        "standalone": _compose_standalone(repo, script_path=_SCRIPT_PATH),
        "in-run": _compose_in_run(roadmap_blitz_text=_RB_TEXT, in_run_commit=True, script_path=_SCRIPT_PATH),
    }


@pytest.mark.parametrize("which", ["standalone", "in-run"])
def test_the_commit_params_block_carries_every_key_the_run_token_hook_requires(repo, which):
    from coordinator_core.ops.dispatch_emit.terminal_commit_params_channel import REQUIRED_STR_KEYS

    script = _scripts(repo)[which]
    (call,) = [ln for ln in script.splitlines() if "_terminalCommitParamsBlock({" in ln and "..._nextAction.params" in ln]
    block = call[call.index("_terminalCommitParamsBlock({"):]
    assert "script_path: _nextAction.params.script_path ?? ''" in block, "an unknown path is '', never an absent key"
    for key in REQUIRED_STR_KEYS:
        assert f"{key}:" in block, key
    assert block.index("..._nextAction.params") < block.index("script_path:") < block.index("run_id: _runId")


@pytest.mark.parametrize("which", ["standalone", "in-run"])
def test_a_known_script_path_rides_the_next_action_and_the_commit_block(repo, which):
    script = _scripts_with_path(repo)[which]
    assert f"script_path: '{_SCRIPT_PATH}', {FANOUT_RECORD_PARAM}: {{ plans:" in script
    assert f"script_path: _nextAction.params.script_path ?? '{_SCRIPT_PATH}', run_id: _runId" in script


@pytest.mark.parametrize("which", ["standalone", "in-run"])
def test_the_fan_out_next_action_params_replay_with_a_run_id(repo, which):
    script = _scripts(repo)[which]
    assert f"params: {{ run_id: _runId, {FANOUT_RECORD_PARAM}: {{ plans:" in script
    assert "script_path: null" not in script


@pytest.mark.parametrize("which", ["standalone", "in-run"])
def test_both_scripts_lint_clean(repo, which):
    assert not [f for f in run_checks(_scripts(repo)[which]) if f.severity is Severity.ERROR]


# --- the fan-out record reaches a sibling run that reviews and commits ----------------------------

import yaml  # noqa: E402

from coordinator_core.ops.dispatch_emit import ask_compose, ask_fanout  # noqa: E402
from coordinator_core.ops.dispatch_emit import land_run_contract, land_run_fanout  # noqa: E402
from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW  # noqa: E402
from coordinator_core.ops.dispatch_emit.tests.test_ask_fanout_sizings_arm import _sizing  # noqa: E402

_PLAN = "docs/plans/2026-10-16-a.md"


@pytest.fixture
def fan_repo(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()
    sizing = _sizing(tmp_path, "a", _PLAN)
    (tmp_path / _PLAN).parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / _PLAN).write_text(
        f"---\nrun_id: fixture\nsizing_object: {sizing}\n---\n\n## Tasks\n\n```yaml plan-tasks\n"
        "- id: A1\n  title: T\n  disposition: open\n  writes:\n    - some/a.py\n```\n",
        encoding="utf-8", newline="\n",
    )
    return tmp_path, sizing


def _sibling_script(repo, sizing_rel, run_id):
    doc = yaml.safe_load((repo / sizing_rel).read_text(encoding="utf-8"))
    doc.update({"route": "plan", "status": "sized"})
    (repo / sizing_rel).write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8", newline="\n")
    return ask_compose.compose_ask_script(
        repo_root=repo.as_posix(), prompt=None, sizing_rel=sizing_rel, run_id=run_id, session_id=None,
        wrap_stage=lambda t: ("  async function planBlitz(args) { return { ready: [] }; }", ["Plan"]),
        plan_blitz_text="stub", in_run_commit=True, run_base_sha="a" * 40, **REVIEW_KW,
    )


def _stages(script):
    labels = [leg.label for leg in lr.emitted_legs(script)]
    return {
        "review": "phase('Review wave')" in script or "phase('Review prep')" in script,
        "commit": "commit" in labels and "dispatch.land_run --params-file" in script,
    }


def test_fanout_record_drives_a_sibling_whose_script_reviews_and_commits(fan_repo):
    repo, sizing = fan_repo
    params = {FANOUT_RECORD_PARAM: {"plans": [_PLAN], "waves": {_PLAN: 0}}}
    assert land_run_contract.mode_of(params) == "fanout"
    resolved, refusal = land_run_fanout._resolve_plans(repo, params[FANOUT_RECORD_PARAM]["plans"])
    assert refusal is None and resolved[0][1] == sizing
    plan, sz, _baton, writes = resolved[0]
    out = ask_fanout.fanout({"sizings": [{"sizing": sz, "label": "a", "wave": 0, "writes": writes}]}, repo)
    assert out["exit_code"] == 0, out
    run = out["ready"][0]
    script = _sibling_script(repo, run["sizing"], run["run_id"])
    assert _stages(script) == {"review": True, "commit": True}
    assert _stages(script.replace("dispatch.land_run --params-file", "x")) == {"review": True, "commit": False}
    stripped = script.replace("phase('Review wave')", "phase('x')").replace("phase('Review prep')", "phase('y')")
    assert _stages(stripped)["review"] is False

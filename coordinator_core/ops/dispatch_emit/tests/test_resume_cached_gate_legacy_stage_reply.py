"""A resumed run whose cached gate return lacks `stage_reply` re-runs one stage (or reads a legacy top-level handle).

The real composed gate-fold line and `_adoptStageReply` run under node; `agent()` is a stub that records
its label and prompt. A gate cached under an engine that did not ask the gate agent to stage replays with no
`stage_reply`; before the fix the fold recorded 'stage_reply absent' and the run halted with no folded manifest.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess

import pytest

from coordinator_core.ops.dispatch_emit import ask_compose
from coordinator_core.ops.dispatch_emit.tests.test_ask_compose_no_fold_cause import _compose
from coordinator_core.ops.dispatch_emit.tests.test_ask_compose_stage_handle import (  # noqa: F401
    WORKFLOW_FAKE_JS,
    readers_cfg,
    stage_reply,
    stage_root,
)
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_ROW = {
    "id": "C1", "brief_path": "b/C1.md", "agent_type": "coordinator:executor", "model": "opus",
    "writes": ["a.py"], "wave": 0, "deps": [],
}
_WHOLE = {"run_dir": "d", "plan_id": "p", "rows": [_ROW], "review_declared_paths": ["a.py"],
          "test_targets": ["t.py"], "marker_path": "m", "head_sha": "a" * 40}
#: A pre-ship-5 gate return: the gate reply fields and nothing about a stage.
_CACHED_GATE = {"arm": "xs", "halt": None, "baton": None, "tshirt": "XS", "route": None}


@pytest.fixture(autouse=True)
def _pre_sized_xs(monkeypatch):
    monkeypatch.setattr(ask_compose, "_known_arm", lambda *_: "xs")


def _fold(gate: dict, *, recovered: dict | None) -> dict:
    """Run the composed gate fold over the replayed `gate`; `recovered` is the stage-recover agent's return."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not on PATH")
    script = _compose(writes=["a.py"])
    fold_start = script.index("  if (_gate && typeof _gate.arm === 'string')")
    fold_end = script.index("\n", fold_start)
    rec_start = script.index("  const _stageRecoverParams")
    rec_end = script.index("\n", script.index("  _stageRecover = async"))
    program = (
        WORKFLOW_FAKE_JS
        + "const cfg = JSON.parse(process.env.CFG);\n"
        "const workflow = makeWorkflow(cfg);\n"
        "const REPO_ROOT = 'REPO';\n"
        "const _seen = [];\n"
        "const _agentDefsClause = () => '';\n"
        "const agent = async (p, opts) => { _seen.push({ label: opts.label, prompt: p }); "
        "if (opts.label !== 'stage-recover') throw new Error('unstubbed ' + opts.label); return cfg.recovered; };\n"
        "(async () => {\n"
        "let _halted = null, _gate = cfg.gate, _sizingRel = null, _planRel = null, _runId = 'run-1';\n"
        "let _writes = ['a.py'], _gated = [], _SESSION_ID = 's', _crossRepoApproved = false;\n"
        + ask_compose._ADMIT_MANIFEST_JS
        + "\nlet _stagedManifest = null;\n"
        + ask_compose._stage_reply_js()
        + "\n"
        + script[rec_start:rec_end]
        + "\n"
        + script[fold_start:fold_end]
        + "\nconsole.log(JSON.stringify({ seen: _seen, manifest: _stagedManifest, refusal: _stageRefusal, "
        "cause: _noFoldCause, relayGap: _relayGap }));\n"
        "})().catch((e) => { console.error(String(e && e.stack || e)); process.exit(1); });\n"
    )
    cfg = {"gate": gate, "recovered": recovered, **readers_cfg()}
    proc = subprocess.run(
        [node, "-"], input=program, capture_output=True, text=True, encoding="utf-8", timeout=60,
        env={**os.environ, "CFG": json.dumps(cfg)},
        **no_console_creationflags(),
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_a_cached_gate_without_stage_reply_re_runs_one_stage_and_folds_its_manifest():
    out = _fold(dict(_CACHED_GATE), recovered={"stage_reply": stage_reply(_WHOLE)})
    assert [s["label"] for s in out["seen"]] == ["stage-recover"]
    assert out["manifest"] == _WHOLE
    assert out["cause"] is None and out["refusal"] is None


def test_the_recovery_stage_runs_over_the_gate_params_of_an_xs_run():
    out = _fold(dict(_CACHED_GATE), recovered={"stage_reply": stage_reply(_WHOLE)})
    prompt = out["seen"][0]["prompt"]
    assert "dispatch.ask_stage" in prompt
    assert '"writes":["a.py"]' in prompt and '"run_id":"run-1"' in prompt


def test_a_cached_gate_carrying_a_legacy_top_level_handle_folds_with_no_agent():
    gate = {**_CACHED_GATE, "manifest_handle": stage_reply(_WHOLE)["manifest_handle"]}
    out = _fold(gate, recovered=None)
    assert out["seen"] == []
    assert out["manifest"] == _WHOLE


def test_a_gate_with_a_stage_reply_still_folds_with_no_agent():
    out = _fold({**_CACHED_GATE, "stage_reply": stage_reply(_WHOLE)}, recovered=None)
    assert out["seen"] == []
    assert out["manifest"] == _WHOLE


def test_a_genuine_stage_refusal_still_refuses_and_re_runs_nothing():
    out = _fold({**_CACHED_GATE, "stage_reply": {"error": "boom"}}, recovered=None)
    assert out["seen"] == []
    assert out["refusal"] == "boom"
    assert out["manifest"] is None


def test_a_recovery_stage_that_refuses_surfaces_its_refusal():
    out = _fold(dict(_CACHED_GATE), recovered={"stage_reply": {"error": "plan not ready"}})
    assert [s["label"] for s in out["seen"]] == ["stage-recover"]
    assert out["refusal"] == "plan not ready"


def test_a_recovery_that_returns_no_stage_reply_keeps_the_no_fold_cause_and_re_runs_only_once():
    out = _fold(dict(_CACHED_GATE), recovered={})
    assert [s["label"] for s in out["seen"]] == ["stage-recover"]
    assert out["manifest"] is None
    assert out["cause"] == "stage_reply absent from the agent return"


#: A gate return captured byte-for-byte from workflow journal wf_db8a35c3-7c0 (arm s), cached
#: 2026-11-06T03:01Z under the pre-ship-5 engine, which did not ask the gate agent to stage.
_REAL_CACHED_GATE = json.loads(
    (__import__("pathlib").Path(__file__).parent / "fixtures" / "gate_return_pre_stage_reply_20261105.json").read_text(encoding="utf-8")
)


def test_a_real_pre_ship5_cached_s_gate_replays_without_refusing_the_run():
    # An s-arm gate stages later, at the plan fold, so the gate fold neither refuses nor re-runs a stage.
    assert "stage_reply" not in _REAL_CACHED_GATE and _REAL_CACHED_GATE["arm"] == "s"
    out = _fold(dict(_REAL_CACHED_GATE), recovered=None)
    assert out["refusal"] is None, out
    assert out["seen"] == []


#: The live gate return example-game-repo-80's resume of run 44e645 got on klabauter d5a8fcc9e (wf_aeb40339-a4d journal
#: line 6, verbatim): a resume gate, arm s, that omitted stage_reply. Not a cache replay.
_LIVE_RESUME_GATE = json.loads(
    (__import__("pathlib").Path(__file__).parent / "fixtures" / "gate_return_resume_no_stage_reply_44e645.json").read_text(encoding="utf-8")
)


def test_a_live_resume_gate_that_omits_stage_reply_re_runs_one_stage_over_its_resume_plan():
    out = _fold(dict(_LIVE_RESUME_GATE), recovered={"stage_reply": stage_reply(_WHOLE)})
    assert out["refusal"] is None and out["cause"] is None, out
    assert out["manifest"] == _WHOLE
    assert [s["label"] for s in out["seen"]] == ["stage-recover"]
    prompt = out["seen"][0]["prompt"]
    assert "2026-11-04-photo-face-record-neutrality-rank-44e645.md" in prompt and '"only_rows":["C2"]' in prompt

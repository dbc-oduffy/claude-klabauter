"""Incident pin: an S-arm launcher child whose launcher already sized, planned and staged runs from its staged twin's
inline manifest, runs no pre-stage leg (no second `size`), and reports `committed: true`; a plan-edged sizing is never
written down to XS.

Shaped on child ask-20261023T023429-62e0ec: sizing tshirt S with a `plan:` edge, a raw-ask child render, the twin minted
by `reply_sidecar.write_reply` from `state/warp/<run>/run.json`, the child started with `args` undefined."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import ask_compose, reply_sidecar, run_token
from coordinator_core.ops.dispatch_emit.ask_compose import compose_ask_script
from coordinator_core.ops.dispatch_emit.tests._node_support import payload_file
from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW
from coordinator_core.ops.dispatch_emit.tests.test_ask_compose_stage_handle import (
    _BLITZ_FN,
    _LANDED,
    _MET,
    big_manifest,
)
from coordinator_core.ops.dispatch_emit.tests.test_launcher_child_staged_twin_incident import _NESTING
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_RUN_ID = "run-1"
_SIZING_REL = "state/sizings/2026-10-23-incident-s.yaml"
_PLAN_REL = "docs/plans/2026-10-23-incident-s.md"
_PRE_STAGE_LABELS = {"size", "size-retry", "size-reword", "gate", "xl-exit", "plan", "plan-review", "plan-revise"}

_PROGRAM = r"""
const fs = require('fs');
const cfg = JSON.parse(process.env.CFG);
const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;
const body = fs.readFileSync(process.env.TWIN_FILE, 'utf8').replace(/^export const meta/m, 'const meta');
const labels = [];
const workflowCalls = [];
const agent = async (prompt, opts) => {
  const label = String(opts && opts.label);
  labels.push(label);
  if (label === 'halt-commit' || label === 'commit') return cfg.commit;
  if (label === 'verify') return cfg.verify;
  if (label.startsWith('review:')) return cfg.review;
  return {};
};
const parallel = async (xs) => Promise.all(xs.map((f) => f()));
const noop = async () => ({});
const workflow = async (spec) => { workflowCalls.push(String(spec && spec.scriptPath)); throw new Error(cfg.nesting); };
const child = new AsyncFunction('args', 'agent', 'parallel', 'pipeline', 'phase', 'workflow', body);
child(undefined, agent, parallel, noop, () => {}, workflow)
  .then((result) => console.log(JSON.stringify({ result, labels, workflowCalls })))
  .catch((e) => { console.error(String((e && e.stack) || e)); process.exit(1); });
"""


def _stage(root: Path, monkeypatch) -> dict:
    """Lay down run.json, the S sizing and its plan, the child script, then mint the handle (which leaves the twin)."""
    run_dir = root / "state/warp" / _RUN_ID
    run_dir.mkdir(parents=True)
    (run_dir / "run.json").write_text(json.dumps({"sizing_rel": _SIZING_REL}), encoding="utf-8")
    (root / _SIZING_REL).parent.mkdir(parents=True, exist_ok=True)
    (root / _SIZING_REL).write_text(f"estimate:\n  tshirt: S\nplan: {_PLAN_REL}\n", encoding="utf-8")
    (root / _PLAN_REL).parent.mkdir(parents=True, exist_ok=True)
    (root / _PLAN_REL).write_text("# plan\n", encoding="utf-8")
    monkeypatch.setattr(ask_compose, "_known_arm", lambda *_: "s")
    script = compose_ask_script(
        repo_root="REPO", prompt="do the thing", sizing_rel=None, run_id=_RUN_ID, session_id=None,
        wrap_stage=lambda _t: (_BLITZ_FN, ["Size", "Plan"]), plan_blitz_text="stub",
        script_path=f"scratch/warp/{_RUN_ID}.workflow.mjs", in_run_commit=True, launcher_child=True, **REVIEW_KW,
    )
    script_path = root / f"scratch/warp/{_RUN_ID}.workflow.mjs"
    script_path.parent.mkdir(parents=True, exist_ok=True)
    script_path.write_text(script, encoding="utf-8", newline="")
    manifest = big_manifest(3)
    (root / ".git").mkdir(exist_ok=True)
    token = run_token.mint(root, _RUN_ID, f"scratch/warp/{_RUN_ID}.workflow.mjs")
    assert run_token.escrow(root, _RUN_ID, token)
    handle = reply_sidecar.write_reply(root, run_dir / "manifest.json", manifest)
    twin = root / f"scratch/warp/{_RUN_ID}.staged-{handle['sha256']}.workflow.mjs"
    assert twin.is_file()
    assert token in twin.read_text(encoding="utf-8")
    return {"script_path": script_path, "manifest": manifest, "handle": handle, "twin": twin}


def _run(staged: dict) -> dict:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not on PATH")
    slice_ = {"id": "s1", "files": [], "diff_path": None, "sidecar_path": None, "contract_blocks": 0}
    cfg = {
        "nesting": _NESTING, "commit": _LANDED,
        "verify": {"items": _MET["items"], "test": {"status": "passed", "sidecar_path": "x.md"}},
        "review": {"prep": {"slices": [slice_], "error": None}, "review": {"slice_id": "s1", "findings": []}},
    }
    proc = subprocess.run(
        [node, "-"], input=_PROGRAM, capture_output=True, text=True, encoding="utf-8", timeout=60,
        env={**os.environ, "TWIN_FILE": payload_file(staged["twin"].read_text(encoding="utf-8")), "CFG": json.dumps(cfg)},
        **no_console_creationflags(),
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_an_s_arm_twin_adopts_its_manifest_runs_no_pre_stage_leg_and_commits(tmp_path, monkeypatch):
    staged = _stage(tmp_path, monkeypatch)
    out = _run(staged)
    assert out["workflowCalls"] == []
    assert not _PRE_STAGE_LABELS & set(out["labels"]), out["labels"]
    result = out["result"]
    assert result.get("halted_by") is None, result
    assert result["rows"] == [r["id"] for r in staged["manifest"]["rows"]]
    assert result["commit"]["committed"] is True, result


def test_a_plan_edged_sizing_is_never_written_down_to_xs(tmp_path):
    import coordinator_core.sizing_assemble as sizing_assemble

    (tmp_path / ".git").mkdir()
    sizing = tmp_path / _SIZING_REL
    sizing.parent.mkdir(parents=True)
    text = (
        "schema: sizing-object\nintent: \"Ship the thing\"\n"
        f"plan: {_PLAN_REL}\nestimate:\n  tshirt: S\n  provisional: true\nroute: dispatch\ndetents: []\n"
        "fork: null\nxl_exit: null\nstatus: draft\n"
        "premise:\n  provenance: unrecorded\n  evidence: PLACEHOLDER - cite the evidence\ndeliverable_id: null\n"
    )
    sizing.write_text(text, encoding="utf-8")
    xs = sizing_assemble.route(estimate={"tshirt": "XS"}, intent="Ship the thing")
    with pytest.raises(sizing_assemble.SizingAssembleError, match="SIZING-PLAN-EDGE-RESIZE-TO-XS") as refused:
        sizing_assemble.write_back(tmp_path, _SIZING_REL, xs)
    assert _PLAN_REL in str(refused.value)
    assert sizing.read_text(encoding="utf-8") == text

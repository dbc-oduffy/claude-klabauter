"""A ledger resume whose manifest owes code review (earlier legs landed a .py file) and whose review returns
zero verdicts halts review_floor, never ok; a carried prior-run verdict stands in and does not halt.
The whole composed script runs under node with every agent leg stubbed by label."""

from __future__ import annotations

import json
import os
import shutil
import subprocess

import pytest

from coordinator_core.ops.dispatch_emit import ask_compose
from coordinator_core.ops.dispatch_emit.ask_compose import compose_ask_script
from coordinator_core.ops.dispatch_emit.review_floor import HALT_REVIEW_FLOOR
from coordinator_core.ops.dispatch_emit.tests._node_support import payload_file
from coordinator_core.ops.dispatch_emit.tests._review_fixture import REVIEW_SIDECAR
from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW
from coordinator_core.ops.dispatch_emit.tests.test_ask_compose_stage_handle import (  # noqa: F401
    WORKFLOW_FAKE_JS,
    readers_cfg,
    stage_reply,
    stage_root,
)
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_BLITZ_FN = "  async function planBlitz(args) {\n    return { ready: [] };\n  }"
_PLAN = "docs/plans/2026-11-07-resume-floor.md"
_SIZING = "state/sizings/x.yaml"
_TOKEN = "0123456789abcdef0123456789abcdef"
_LANDED_CODE = "pkg/landed_c1.py"
_DOCS = "docs/remaining_c2.md"
_RESUME = {
    "run_id": "none", "plan": _PLAN, "blocking": ["R1"], "halt_sha": None, "remaining": {},
}
_MANIFEST = {
    "run_dir": "d", "plan_id": "p", "marker_path": "m", "head_sha": "c" * 40, "test_targets": [],
    "rows": [{
        "id": "C2", "brief_path": "b/C2.md", "agent_type": "coordinator:executor", "model": "opus",
        "writes": [_DOCS], "wave": 0, "deps": [],
    }],
    "review_declared_paths": [_DOCS],
    "review_code_paths": [_LANDED_CODE],
}
_LANDED = {"committed": True, "sha": "abc1234", "error": None, "refused": None}
_MET = {"items": [{"id": "R1", "status": "met", "wired": True}]}
_CARRIED = {
    "slice_id": "s1", "verdict": "PASS", "findings": 0, "applied": 0, "sidecar_path": REVIEW_SIDECAR,
}
_NODE_PROGRAM = WORKFLOW_FAKE_JS + r"""
const src = require('fs').readFileSync(process.env.SRC_FILE, 'utf8').replace(/^export const meta/m, 'const meta');
const cfg = JSON.parse(process.env.CFG);
const workflow = makeWorkflow(cfg);
const calls = [];
const agent = async (prompt, opts) => {
  const label = opts && opts.label;
  calls.push({ label, prompt: String(prompt) });
  if (label === 'halt-commit' || label === 'commit') return cfg.commit;
  if (label === 'verify') return cfg.verify;
  if (cfg.replies[label] !== undefined) return cfg.replies[label];
  return {};
};
const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;
const run = new AsyncFunction('args', 'agent', 'parallel', 'pipeline', 'phase', 'workflow', src);
run({ runToken: cfg.token }, agent, async (xs) => Promise.all(xs.map((f) => f())), async () => ({}), () => {}, workflow)
  .then((result) => console.log(JSON.stringify({ calls, result })))
  .catch((e) => { console.error(String((e && e.stack) || e)); process.exit(1); });
"""


def _script(monkeypatch) -> str:
    monkeypatch.setattr(ask_compose, "_known_arm", lambda *_: "m_plus")
    return compose_ask_script(
        repo_root="REPO", prompt=None, sizing_rel=_SIZING, run_id="run-1", session_id=None,
        wrap_stage=lambda _t: (_BLITZ_FN, ["Size", "Plan"]), plan_blitz_text="stub",
        script_path="scratch/warp/x.mjs", in_run_commit=True, **REVIEW_KW,
    )


def _run(script: str, *, manifest=_MANIFEST, resume_phases=None, replies=None) -> dict:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not on PATH")
    gate_reply = {
        "arm": "m_plus", "halt": None, "baton": None, "resume_ledger": _RESUME,
        "tshirt": "M", "route": "spec-dispatch", "stage_reply": stage_reply(manifest),
        **({"resume_phases": resume_phases} if resume_phases is not None else {}),
    }
    cfg = {
        "token": _TOKEN, "verify": _MET, "commit": _LANDED, **readers_cfg(),
        "replies": {"gate": gate_reply, "gate-stage": gate_reply, **(replies or {})},
    }
    proc = subprocess.run(
        [node, "-"], input=_NODE_PROGRAM, capture_output=True, text=True, encoding="utf-8", timeout=60,
        env={**os.environ, "SRC_FILE": payload_file(script), "CFG": json.dumps(cfg)}, **no_console_creationflags(),
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def _prep(product_files: int) -> dict:
    return {
        "prep": {
            "verdict": "single-reviewer-ok", "slices": [], "product_files": product_files, "error": None,
            "foreign_claims": [],
        },
        "review": None,
    }


def _resumed(script: str, *, prep_reply: dict, **kw) -> dict:
    """A ledger resume that dispatches the docs-only row C2 and whose review wave yields `prep_reply`."""
    replies = {"work:C2": "DONE: C2 wrote docs", "review:coordinator:code-reviewer": prep_reply}
    return _run(script, replies=replies, **kw)


@pytest.fixture(autouse=True)
def _unlanded_c2(monkeypatch):
    monkeypatch.setitem(_RESUME, "unlanded", ["C2"])


def test_zero_verdicts_over_landed_code_halts_review_floor_and_never_commits(monkeypatch):
    out = _resumed(_script(monkeypatch), prep_reply=_prep(1))
    res = out["result"]
    assert res["halted_by"]["halted"] == HALT_REVIEW_FLOOR, res["halted_by"]
    assert _LANDED_CODE in res["halted_by"]["reason"]
    assert res["review"]["receipt"] == "review: 0 verdicts"
    assert res["commit"] is None and "commit" not in [c["label"] for c in out["calls"]]


def test_a_review_that_returns_no_prep_halts_instead_of_reporting_ok(monkeypatch):
    out = _resumed(_script(monkeypatch), prep_reply={})
    res = out["result"]
    assert res["halted_by"] is not None, "a ledger resume owing code review reported ok with zero verdicts"
    assert res["commit"] is None


def test_a_carried_prior_verdict_stands_for_the_review_and_does_not_halt(monkeypatch):
    out = _resumed(
        _script(monkeypatch), prep_reply=_prep(1),
        resume_phases={"verdicts": {"review": {"wave": [_CARRIED]}}},
    )
    res = out["result"]
    assert res["halted_by"] is None, res["halted_by"]
    assert res["review"]["receipt"] == "review: 1 verdicts"
    assert res["commit"]["committed"] is True


def test_a_manifest_owing_no_code_review_is_not_floored(monkeypatch):
    docs_only = {k: v for k, v in _MANIFEST.items() if k != "review_code_paths"}
    out = _resumed(_script(monkeypatch), prep_reply=_prep(1), manifest=docs_only)
    res = out["result"]
    assert (res["halted_by"] or {}).get("halted") != HALT_REVIEW_FLOOR, res["halted_by"]


# A prep that counts product files off the resumed run's base diff reports 0 for docs-only remaining legs, and the
# floor expression (`_reviewPrep?.product_files !== 0`) then skips the halt: the run commits with zero verdicts.
def test_zero_verdicts_with_a_zero_product_files_prep_still_halts(monkeypatch):
    out = _resumed(_script(monkeypatch), prep_reply=_prep(0))
    res = out["result"]
    assert res["halted_by"] is not None, "ledger resume committed with zero review verdicts over landed code"
    assert res["commit"] is None

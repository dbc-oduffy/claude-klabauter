"""The composed ask script's ledger-fix loop: compose-and-grep pins plus node drives with stubbed replies.

The loop sits between the verify leg and the ledger halt: it reopens only the chunks a blocking row names,
caps its attempts, writes `recovery.json`, and withholds the chunks it could not fix."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess

import pytest

from coordinator_core.ops.dispatch_emit import ask_compose
from coordinator_core.ops.dispatch_emit.ask_compose import compose_ask_script
from coordinator_core.ops.dispatch_emit.ledger_recovery import (
    LEDGER_FIX_LABEL,
    LEDGER_REVERIFY_LABEL,
    RECOVERY_ATTEMPT_CAP,
    RECOVERY_WITHHELD_PARAM,
)
from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW
from coordinator_core.ops.dispatch_emit.tests.test_ask_compose_stage_handle import (  # noqa: F401
    WORKFLOW_FAKE_JS,
    readers_cfg,
    stage_reply,
    stage_root,
)
from coordinator_core.ops.dispatch_emit.tests._node_support import payload_file
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_BLITZ_FN = "  async function planBlitz(args) {\n    return { ready: [] };\n  }"
_PLAN = "docs/plans/2026-10-18-landed.md"
_SIZING = "state/sizings/x.yaml"
_TOKEN = "0123456789abcdef0123456789abcdef"
_RESUME = {"run_id": "ask-prior", "plan": _PLAN, "blocking": ["R1"], "em_discharged": {}}
_LANDED = {"committed": True, "sha": "abc1234", "error": None, "refused": None}
_ROWS = [
    {"id": "C1", "brief_path": "b/C1.md", "agent_type": "coordinator:executor", "model": "opus",
     "writes": ["pkg/a.py", "tests/test_a.py"], "wave": 0, "deps": [], "register_rows": ["R1"]},
    {"id": "C2", "brief_path": "b/C2.md", "agent_type": "coordinator:executor", "model": "opus",
     "writes": ["pkg/b.py"], "wave": 0, "deps": [], "register_rows": ["R1"]},
]
_DISK = {
    "run_dir": "d", "plan_id": "p", "rows": _ROWS, "review_declared_paths": ["pkg/a.py", "pkg/b.py"],
    "test_targets": [], "marker_path": "m", "head_sha": "c" * 40,
}
_MET = {"id": "R1", "status": "met", "wired": True}


def _unmet(remaining, evidence="", rid="R1"):
    return {"id": rid, "status": "not_met", "essential": True, "remaining": remaining, "evidence": evidence}


_NODE_PROGRAM = (
    WORKFLOW_FAKE_JS
    + r"""
const src = require('fs').readFileSync(process.env.SRC_FILE, 'utf8').replace(/^export const meta/m, 'const meta');
const cfg = JSON.parse(process.env.CFG);
const workflow = makeWorkflow(cfg);
const calls = [];
const counts = {};
const agent = async (prompt, opts) => {
  const label = opts && opts.label;
  calls.push({ label, prompt: String(prompt) });
  counts[label] = (counts[label] ?? 0) + 1;
  if (label === 'halt-commit' || label === 'commit') return cfg.commit;
  if (label === 'verify') return cfg.verify;
  const r = cfg.replies[label];
  if (Array.isArray(r)) return r[Math.min(counts[label] - 1, r.length - 1)];
  if (r !== undefined) return r;
  return {};
};
const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;
const run = new AsyncFunction('args', 'agent', 'parallel', 'pipeline', 'phase', 'workflow', src);
run({ runToken: cfg.token }, agent, async (xs) => Promise.all(xs.map((f) => f())), async () => ({}), () => {}, workflow)
  .then((result) => console.log(JSON.stringify({ calls, result })))
  .catch((e) => { console.error(String((e && e.stack) || e)); process.exit(1); });
"""
)


def _script(monkeypatch) -> str:
    monkeypatch.setattr(ask_compose, "_known_arm", lambda *_: "m_plus")
    return compose_ask_script(
        repo_root="REPO", prompt=None, sizing_rel=_SIZING, run_id="run-1", session_id=None,
        wrap_stage=lambda _t: (_BLITZ_FN, ["Size", "Plan"]), plan_blitz_text="stub",
        script_path="scratch/warp/x.mjs", in_run_commit=True, **REVIEW_KW,
    )


def _run(script: str, *, items, replies=None, manifest=None, test=None) -> dict:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not on PATH")
    disk = manifest or _DISK
    gate = {
        "arm": "m_plus", "halt": None, "baton": None, "resume_ledger": _RESUME, "tshirt": "M",
        "route": "spec-dispatch", "stage_reply": stage_reply(disk),
    }
    verify = {"items": items, **({"test": test} if test else {})}
    cfg = {
        "token": _TOKEN, "verify": verify, "commit": _LANDED, **readers_cfg(),
        "replies": {"gate": gate, "gate-stage": gate, **(replies or {})},
    }
    proc = subprocess.run(
        [node, "-"], input=_NODE_PROGRAM, capture_output=True, text=True, encoding="utf-8", timeout=60,
        env={**os.environ, "SRC_FILE": payload_file(script), "CFG": json.dumps(cfg)}, **no_console_creationflags(),
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def _labels(out: dict) -> list[str]:
    return [c["label"] for c in out["calls"]]


def _fix_labels(out: dict) -> list[str]:
    return [lab for lab in _labels(out) if lab and lab.startswith(LEDGER_FIX_LABEL)]


def _commit_prompt(out: dict) -> str:
    return next(c for c in out["calls"] if c["label"] == "commit")["prompt"]


def _record(out: dict) -> dict:
    prompt = next(c for c in out["calls"] if c["label"] == "ledger-recovery-record")["prompt"]
    text = prompt.split("holding exactly this JSON text and nothing else: ", 1)[1].split(". Edit nothing else", 1)[0]
    return json.loads(text)


_FIXED = {"status": "DONE", "paths": ["pkg/a.py"], "reason": "fixed"}
_NAMED = _unmet(["C1: fix pkg/a.py"])


def test_the_loop_composes_once_with_the_cap_rendered_from_the_constant(monkeypatch):
    script = _script(monkeypatch)
    assert script.count("const _ledgerRecover = async") == 1
    assert script.count("await _ledgerRecover()") == 1
    assert f"const _recoveryCap = {RECOVERY_ATTEMPT_CAP};" in script
    assert script.count(f"label: '{LEDGER_FIX_LABEL}' + _cid") == 1
    assert script.count(f"label: '{LEDGER_REVERIFY_LABEL}'") == 1
    assert script.index("const _ledgerRecover = async") < script.index("const _rulable = ")
    assert re.search(r"phases: \[[^\]]*'ledger-fix'", script)


def test_an_xs_arm_composes_no_loop(monkeypatch):
    monkeypatch.setattr(ask_compose, "_known_arm", lambda *_: ask_compose.ARM_XS)
    script = compose_ask_script(
        repo_root="REPO", prompt=None, sizing_rel=_SIZING, run_id="run-1", session_id=None,
        wrap_stage=lambda _t: (_BLITZ_FN, ["Size", "Plan"]), plan_blitz_text="stub",
        script_path="scratch/warp/x.mjs", in_run_commit=True, **REVIEW_KW,
    )
    assert "_ledgerRecover" not in script
    assert "'ledger-fix'" not in script.split("phases: [", 1)[1].split("]", 1)[0]


def test_a_named_chunk_is_fixed_and_the_run_commits_with_no_halt(monkeypatch):
    out = _run(
        _script(monkeypatch), items=[_NAMED],
        replies={"ledger-fix:C1": _FIXED, LEDGER_REVERIFY_LABEL: {"items": [_MET]}},
    )
    labels = _labels(out)
    assert _fix_labels(out) == ["ledger-fix:C1"], labels
    assert labels.index("ledger-fix:C1") < labels.index(LEDGER_REVERIFY_LABEL) < labels.index("commit")
    assert out["result"]["halted_by"] is None
    rec = _record(out)
    assert rec["chunks"]["C1"]["status"] == "fixed" and rec["chunks"]["C1"]["attempts"] == 1
    assert "C2" not in rec["chunks"]
    assert RECOVERY_WITHHELD_PARAM not in _commit_prompt(out)


def test_every_fix_failing_stops_at_the_cap_and_withholds_the_chunk(monkeypatch):
    out = _run(
        _script(monkeypatch), items=[_NAMED],
        replies={"ledger-fix:C1": _FIXED, LEDGER_REVERIFY_LABEL: {"items": [_NAMED]}},
    )
    assert _fix_labels(out) == ["ledger-fix:C1"] * RECOVERY_ATTEMPT_CAP
    assert _labels(out).count(LEDGER_REVERIFY_LABEL) == RECOVERY_ATTEMPT_CAP
    rec = _record(out)["chunks"]["C1"]
    assert rec["status"] == "exhausted" and rec["attempts"] == RECOVERY_ATTEMPT_CAP
    res = out["result"]
    assert res["halted_by"] is None
    assert "C1" in res["incomplete"]
    assert f'"{RECOVERY_WITHHELD_PARAM}":["C1"]' in _commit_prompt(out)


def test_a_blocked_fix_spends_no_more_attempts(monkeypatch):
    blocked = {"status": "BLOCKED", "paths": [], "reason": "needs a path outside the footprint"}
    out = _run(_script(monkeypatch), items=[_NAMED], replies={"ledger-fix:C1": blocked})
    assert _fix_labels(out) == ["ledger-fix:C1"]
    assert LEDGER_REVERIFY_LABEL not in _labels(out)
    rec = _record(out)["chunks"]["C1"]
    assert rec["status"] == "blocked" and rec["attempts"] == 1
    assert f'"{RECOVERY_WITHHELD_PARAM}":["C1"]' in _commit_prompt(out)


def test_a_path_outside_the_footprint_blocks_the_chunk_and_is_recorded(monkeypatch):
    stray = {"status": "DONE", "paths": ["pkg/a.py", "pkg/_substrate.py"], "reason": "fixed"}
    out = _run(_script(monkeypatch), items=[_NAMED], replies={"ledger-fix:C1": stray})
    rec = _record(out)["chunks"]["C1"]
    assert rec["status"] == "blocked"
    assert rec["paths"] == ["pkg/_substrate.py"]
    assert rec["footprint"] == ["pkg/a.py", "tests/test_a.py"]


def test_an_unattributed_item_still_halts_exactly_as_today(monkeypatch):
    out = _run(_script(monkeypatch), items=[_unmet(["something no chunk owns"])])
    assert out["result"]["halted_by"]["halted"] == "ledger-check"
    assert _fix_labels(out) == []
    assert "ledger-recovery-record" not in _labels(out)


def test_a_held_only_item_runs_no_fix_leg_and_is_withheld(monkeypatch):
    manifest = {**_DISK, "gated": [{"id": "C9", "reason": "depends_on_plan"}]}
    out = _run(_script(monkeypatch), items=[_unmet(["C9 waits on another plan"])], manifest=manifest)
    assert _fix_labels(out) == []
    assert _record(out)["chunks"]["C9"]["status"] == "held"
    assert _record(out)["chunks"]["C9"]["attempts"] == 0
    assert out["result"]["halted_by"] is None


def test_a_rulable_blocking_item_runs_no_fix_leg(monkeypatch):
    manifest = {**_DISK, "rulable_ids": ["R1"]}
    out = _run(_script(monkeypatch), items=[_NAMED], manifest=manifest)
    assert _fix_labels(out) == []
    assert out["result"]["halted_by"]["halted"] == "ledger-check"


def test_an_owed_blocking_item_runs_no_fix_leg(monkeypatch):
    owed = {**_NAMED, "owed_by": ["X1"]}
    manifest = {**_DISK, "owed": [{"id": "X1", "reason": "em", "detail": "d"}]}
    out = _run(_script(monkeypatch), items=[owed], manifest=manifest)
    assert _fix_labels(out) == []


def test_a_test_file_one_chunk_owns_reopens_only_that_chunk_with_it_in_the_footprint(monkeypatch):
    out = _run(
        _script(monkeypatch), items=[_unmet(["Fix the 2 failing tests in tests/test_a.py"])],
        replies={"ledger-fix:C1": _FIXED, LEDGER_REVERIFY_LABEL: {"items": [_MET]}},
    )
    assert _fix_labels(out) == ["ledger-fix:C1"]
    prompt = next(c for c in out["calls"] if c["label"] == "ledger-fix:C1")["prompt"]
    assert '["pkg/a.py","tests/test_a.py"]' in prompt
    assert "pkg/b.py" not in prompt


def test_two_chunks_claiming_the_row_are_not_both_reopened_without_a_name(monkeypatch):
    out = _run(_script(monkeypatch), items=[_unmet(["still red"])])
    assert _fix_labels(out) == []
    assert out["result"]["halted_by"]["halted"] == "ledger-check"

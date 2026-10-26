"""A staged resume after a guard denial resumes the accepted plan with the same tshirt seed and never re-enters plan-blitz."""

from __future__ import annotations

import hashlib
import json
import re

from coordinator_core.ops.dispatch_emit import ask_compose, launcher_staged_twin as twin, nested_run_contract as nrc
from coordinator_core.ops.dispatch_emit.ask_compose import compose_ask_script
from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW
from coordinator_core.ops.dispatch_emit.tests.test_ask_compose_stage_handle import _BLITZ_FN, _SIZING

RAW = b'{"rows":[{"id":"C1"}]}'
SHA = hashlib.sha256(RAW).hexdigest()
HANDLE = {"path": "state/warp/run1/manifest.json", "sha256": SHA, "bytes": len(RAW)}
SCRIPT = f"export const meta = {{ name: 'x' }}\n{nrc.STAGED_INLINE_SLOT}\nreturn 1;\n"
SIZING_TEXT = "estimate:\n  tshirt: L\nplan: docs/plans/accepted.md\n"


def _stage(root):
    return twin.ensure_twin(root, "state/warp/run1/manifest.json", RAW, HANDLE, max_bytes=524288, refuse=ValueError)


def _resume(root) -> dict:
    path = root / nrc.staged_twin_rel("run1", SHA)
    line = next(ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.startswith("const _STAGED_INLINE = "))
    return json.loads(line[len("const _STAGED_INLINE = "):-1])[nrc.STAGED_RESUME_KEY]


def _setup(root, run_record: dict) -> None:
    script = root / "scratch" / "warp" / "run1.workflow.mjs"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text(SCRIPT, encoding="utf-8")
    run_dir = root / "state" / "warp" / "run1"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "run.json").write_text(json.dumps(run_record), encoding="utf-8")
    sizing = root / "state/sizings/s1.yaml"
    sizing.parent.mkdir(parents=True, exist_ok=True)
    sizing.write_text(SIZING_TEXT, encoding="utf-8")


def test_restage_after_guard_denial_keeps_the_accepted_plan_and_tshirt_seed(tmp_path):
    _setup(tmp_path, {"sizing_rel": "state/sizings/s1.yaml"})
    _stage(tmp_path)
    before = _resume(tmp_path)
    assert before["plan_rel"] == "docs/plans/accepted.md"
    assert before["tshirt"] == "L"

    denied = {"sizing_rel": "state/sizings/s1.yaml", "status": "halted", "halt": {"kind": "guard-denial", "reason": "write denied"}}
    (tmp_path / "state/warp/run1/run.json").write_text(json.dumps(denied), encoding="utf-8")
    _stage(tmp_path)
    assert _resume(tmp_path) == before


def test_restage_after_denial_without_a_sizing_plan_edge_seeds_no_plan_rather_than_a_fresh_one(tmp_path):
    _setup(tmp_path, {"sizing_rel": "state/sizings/s1.yaml", "status": "halted"})
    (tmp_path / "state/sizings/s1.yaml").write_text("estimate:\n  tshirt: L\n", encoding="utf-8")
    _stage(tmp_path)
    block = _resume(tmp_path)
    assert block["plan_rel"] is None and block["tshirt"] == "L"


def _script(monkeypatch) -> str:
    monkeypatch.setattr(ask_compose, "_known_arm", lambda *_: "m_plus")
    return compose_ask_script(
        repo_root="REPO", prompt=None, sizing_rel=_SIZING, run_id="run-1", session_id=None,
        wrap_stage=lambda _t: (_BLITZ_FN, ["Size", "Plan"]), plan_blitz_text="stub",
        script_path="scratch/warp/x.mjs", in_run_commit=True, launcher_child=True, **REVIEW_KW,
    )


def test_composed_script_runs_plan_blitz_only_when_no_resume_block(monkeypatch):
    script = _script(monkeypatch)
    lines = script.splitlines()
    calls = [i for i, ln in enumerate(lines) if re.search(r"await \w*\(\{ .*mode: 'single'", ln)]
    assert calls, "the composed script carries a plan-blitz call"
    for i in calls:
        opener = next(lines[j] for j in range(i, -1, -1) if re.match(r"  if \(", lines[j]))
        assert "!_STAGED_RESUME" in opener, opener
    assert "const _STAGED_RESUME" in script


def test_the_plan_legs_sit_behind_the_no_resume_guard(monkeypatch):
    script = _script(monkeypatch)
    guards = [ln for ln in script.splitlines() if re.match(r"  if \(!_halted && !_STAGED_RESUME && _gate\.arm === ", ln)]
    assert len(guards) >= 2

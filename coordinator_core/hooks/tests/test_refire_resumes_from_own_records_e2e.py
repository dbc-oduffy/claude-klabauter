"""Exit criterion end to end, df88cd shape, through the default refire call (`{sizing}` only): a run whose
rows are executed and reviewed, whose marker already reads `resumed` and which holds no `resume.json`,
resumes under its own run id from its manifest, returns and kept.json; the premise check never contradicts
its own paths; one pack-read stall in the recompose is retried."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from coordinator_core.git.git_objects import PackReadStalled
from coordinator_core.hooks import submit_engine_ask as mod
from coordinator_core.ops import plan_readiness_verdict
from coordinator_core.ops.dispatch_emit import ask_compose, halt_resume_build, kept_work, op, row_returns, sizing_fire
from coordinator_core.ops.dispatch_emit import halt_resume_record as hrr
from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT

pytestmark = pytest.mark.complete

SIZING_REL = "state/sizings/2026-10-16-own-fixture.yaml"
RUN = "warp-2026-10-16-own-fixture-20261013T010101-abc123"
FRESH = "warp-2026-10-16-own-fixture-20261013T020202-def456"
RETURNED_ONLY = "a/one.py"
KEPT = "a/two.py"
PLAN_REL = "docs/plans/2026-10-16-readiness.md"
BLITZ = "export const meta = { phases: [{ title: 'Plan' }] };\nreturn { ready: [] };\n"
_SPINE = (
    "- id: C1\n  title: Ship it\n  body: Do the named work and pin it with a test.\n  change_kind: code-edit\n"
    "  surface: coordinator_core/ops/plan_prep_gate.py\n  writes: [coordinator_core/ops/plan_prep_gate.py]\n"
    "  queue_scope: project\n  disposition: open\n"
)


def _put(path: Path, doc) -> None:
    path.write_text(json.dumps(doc), encoding="utf-8")


def _repo(tmp_path: Path, *, orphan: bool = True) -> Path:
    """The df88cd shape: marker `resumed`, C1 and C2 returned, kept.json holding C2 only, review passed, no
    `resume.json`, and an orphan marker-less run dir an earlier refire minted."""
    (tmp_path / ".git").mkdir()
    (tmp_path / "state" / "sizings").mkdir(parents=True)
    doc = {
        "schema": "sizing-object", "intent": "build the op", "estimate": {"tshirt": "XS", "provisional": True},
        "route": "dispatch", "detents": [], "fork": None, "xl_exit": None, "status": "sized",
        "premise": {"provenance": "not-applicable", "evidence": "fixture"},
        "pm_verbatims": [{"turn": 1, "text": "build the op"}],
        "interaction_mode": "pm", "deliverable_id": "dlv-refire-abc123",
        "exit_criterion": {"statement": "done", "accepted": {"pm_quote": "yes", "on": "2026-10-16", "mode": "pm"}},
    }
    (tmp_path / SIZING_REL).write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    for rel in (RETURNED_ONLY, KEPT):
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(f"# {rel}\n", encoding="utf-8")
    rd = tmp_path / RECORD_DIR_ROOT / RUN
    rd.mkdir(parents=True)
    _put(rd / "manifest.json", {"rows": [{"id": "C1", "writes": [RETURNED_ONLY]}, {"id": "C2", "writes": [KEPT]}]})
    row_returns.write(rd, {"C1": "DONE: C1", "C2": "DONE: C2"})
    _put(rd / "kept.json", {
        "chunks": {"C2": {"paths": {KEPT: kept_work.fingerprint(tmp_path, KEPT)}}},
        "plan_gate": None, "post_checkpoint": {},
    })
    _put(rd / "run-outcome.json", {
        "incomplete_chunks": [], "plan_path": "docs/plans/p.md",
        "inline_review": {"wave_reviews": [{"verdict": "WARN"}], "tests": {"status": "pass"}},
    })
    _put(rd / "halted.json", {"status": "resumed", "halted": "commit", "sizing_rel": SIZING_REL})
    _put(rd / "run-stage.json", {"stage": "commit"})
    if orphan:
        fresh = tmp_path / RECORD_DIR_ROOT / FRESH
        fresh.mkdir()
        _put(fresh / "run-stage.json", {"stage": "composed"})
    (tmp_path / "docs" / "plans").mkdir(parents=True)
    (tmp_path / "coordinator_core").mkdir()
    (tmp_path / PLAN_REL).write_text(
        "---\ntitle: fixture\nauthor: fixture\nstatus: draft\ncreated: 2026-10-01\n"
        "census:\n  - question: q\n    command: echo a\n    result: '1'\n"
        "capabilities: []\nanchor_premises: []\n"
        f"sizing_object: {SIZING_REL}\n"
        f"prime_exit_criterion:\n  statement: done\n  derived_from: {SIZING_REL}\n"
        "---\n\n# Fixture\n\n## Tasks\n\n```yaml plan-tasks\n" + _SPINE + "```\n",
        encoding="utf-8",
    )
    return tmp_path


def _payload(root: Path) -> dict:
    return {
        "tool_name": "Workflow", "cwd": str(root), "session_id": "s1", "tool_use_id": "toolu_x",
        "tool_input": {"name": mod.SUBMIT_WORKFLOW_NAME, "args": {"sizing": SIZING_REL}},
    }


def _runs(root: Path) -> list:
    return sorted(p.name for p in (root / RECORD_DIR_ROOT).iterdir())


def _emit(emitted: list):
    def emit(params, root):
        emitted.append(params)
        return {"path": str(root / "emitted.js"), "run_id": "fresh", "fire_args": {}}

    return emit


def test_default_refire_resumes_the_run_at_landing_from_its_own_records_with_no_resume_json(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    spawned: list = []
    emitted: list = []
    monkeypatch.setattr(mod, "_spawn_recompose", lambda r, run_id: spawned.append(run_id) or True)
    rd = root / RECORD_DIR_ROOT / RUN
    assert not (rd / "resume.json").exists()

    env = mod.evaluate(_payload(root), emit=_emit(emitted))

    hso = env["hookSpecificOutput"]
    assert hso["permissionDecision"] == "allow"
    assert RUN in Path(hso["updatedInput"]["scriptPath"]).read_text(encoding="utf-8")
    assert spawned == [RUN] and emitted == []
    assert _runs(root) == [RUN, FRESH]
    assert json.loads((rd / "halted.json").read_text(encoding="utf-8"))["status"] == "halted"
    assert sizing_fire._kept_rows(root, SIZING_REL, ["C1", "C2"]) == (("C2",), RUN)
    from coordinator_core.ops.dispatch_emit.own_records_resume import landing_resume_from_own_records

    verdict = landing_resume_from_own_records(root, RUN)
    assert verdict is not None and set(verdict.paths) >= {RETURNED_ONLY, KEPT}


def _readiness(root: Path, citation_path: str) -> dict:
    row = {"verdict": "CONTRADICTED", "citation": f"{citation_path}:3 the plan cites base content"}
    return plan_readiness_verdict._handler({"plan": PLAN_REL, "premise": {"rows": [row]}}, root / ".git")


def test_premise_verdict_never_contradicts_the_runs_own_kept_or_returned_paths(tmp_path):
    root = _repo(tmp_path)
    for own in (KEPT, RETURNED_ONLY):
        verdict = _readiness(root, own)
        assert verdict["verdict"] == "ready" and verdict["blocking_findings"] == 0, own
    untouched = _readiness(root, "src/untouched.py")
    assert untouched["verdict"] == "judgment" and untouched["blocking_findings"] == 1


def test_one_pack_read_stall_in_the_recompose_is_retried_and_the_run_id_reused(tmp_path, monkeypatch):
    root = _repo(tmp_path, orphan=False)  # an orphan composed run would itself make the recompose gate refuse
    monkeypatch.chdir(root)
    monkeypatch.setattr(ask_compose, "_read_plan_blitz", lambda: BLITZ)
    real_gate = op._gate_sizing_at_emit
    gate_calls: list = []

    def stalling_gate(*args, **kwargs):
        gate_calls.append(1)
        if len(gate_calls) == 1:
            raise PackReadStalled("pack read exceeded 4.0s")
        return real_gate(*args, **kwargs)

    monkeypatch.setattr(op, "_gate_sizing_at_emit", stalling_gate)
    recomposed: list = []

    def recompose_in_process(r, run_id):
        recomposed.append((run_id, *halt_resume_build.recompose_script(r, run_id)))
        return True

    monkeypatch.setattr(mod, "_spawn_recompose", recompose_in_process)
    emitted: list = []

    env = mod.evaluate(_payload(root), emit=_emit(emitted))

    assert env["hookSpecificOutput"]["permissionDecision"] == "allow"
    assert emitted == [] and _runs(root) == [RUN]
    (run_id, text, why), = recomposed
    assert run_id == RUN and why == "" and RUN in text
    assert len(gate_calls) == 2
    stored = hrr.read(root / RECORD_DIR_ROOT / RUN, 1000)
    assert isinstance(stored, dict) and stored["composed_script_rel"]

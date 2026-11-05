"""End to end: default-path runs through the completion gate read green in run-outcome-audit.

Drives dispatch.terminal_commit and dispatch.record_halted_run_landed at default params over five
fixture runs (standing halt, withheld clause, executor-template criterion, met without falsifier
output, backed judge-authored met), then reads the run records through the detector's `audit`.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import ask_contract, halted_run
from coordinator_core.ops.dispatch_emit import halted_run_landed as landed_op
from coordinator_core.ops.dispatch_emit import terminal_commit
from coordinator_core.ops.dispatch_emit.ask_contract import (
    ASK_MANIFEST_MARKER,
    RUN_DIR_ROOT,
    StageManifest,
    write_run_outcome,
)
from coordinator_core.ops.dispatch_emit.commit_request import ChunkCommit, CommitRequest, render_marker
from coordinator_core.ops.dispatch_emit.completion_gate import JUDGE_AGENT
from coordinator_core.ops.dispatch_emit.emit import _JUDGE_VERDICT_RULES_JS
from coordinator_core.ops.dispatch_emit.tests._review_fixture import reviewed
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_AUDIT_SCRIPT = Path(__file__).resolve().parents[4] / "coordinator" / "bin" / "run-outcome-audit.py"
_spec = importlib.util.spec_from_file_location("run_outcome_audit", _AUDIT_SCRIPT)
audit_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(audit_mod)

_PLAN = """---
title: p
status: executing
plan_id: pln-x-123456
prime_exit_criterion:
  text: a.py exists
---

# Plan

## Tasks

```yaml plan-tasks
- id: C3
  title: t3
  change_kind: code-edit
  surface: a.py
  disposition: open
  deferred: false
```
"""
_PASS = {"status": "pass", "run": 1, "failed": 0, "sidecar": "t.md"}
_BACKED = {
    "status": "met", "observation": "a.py exists at HEAD", "sidecar": "j.md",
    "authored_by": JUDGE_AGENT, "falsifier": {"verdict": "pass", "output": "ls a.py\na.py"},
}
_TEMPLATE_BYTES = 1566
_SESSION = "11111111-2222-3333-4444-555555555555"


def _git(args, cwd: Path) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args], cwd=str(cwd),
        capture_output=True, text=True, check=True, **no_console_creationflags(),
    ).stdout.strip()


def _repo(base: Path, name: str) -> Path:
    root = base / name
    (root / "docs").mkdir(parents=True)
    _git(["init", "-q"], root)
    (root / "docs" / "plan.md").write_text(_PLAN, encoding="utf-8")
    _git(["add", "."], root)
    _git(["commit", "-q", "-m", "plan"], root)
    return root


def _template_sidecar(root: Path) -> None:
    """The executor's empty judge template: headings and blank fields, no verdict."""
    body = "# Criterion judge\n\nstatus:\nobservation:\nfalsifier output:\n"
    (root / "j.md").write_bytes((body + "-" * (_TEMPLATE_BYTES - len(body) - 1) + "\n").encode("utf-8"))
    assert (root / "j.md").stat().st_size == _TEMPLATE_BYTES


def _land(base: Path, name: str, criterion: dict, *, pre_halt: bool = False) -> dict:
    """Commit one run through terminal_commit at default params; return the reply plus its handles."""
    root = _repo(base, name)
    _template_sidecar(root)
    rid = f"run-{name}"
    (root / "a.py").write_text("a\n", encoding="utf-8")
    run_dir = root / RUN_DIR_ROOT / rid
    run_dir.mkdir(parents=True)
    request = CommitRequest(chunks=(ChunkCommit(id="C3", title="t3", paths=("a.py",)),), plan_path="docs/plan.md")
    (run_dir / "commit-request.txt").write_text(render_marker(request) + "\n", encoding="utf-8")
    manifest = StageManifest(
        run_dir=f"{RUN_DIR_ROOT}/{rid}", rows=(), review_declared_paths=(),
        marker_path=f"{RUN_DIR_ROOT}/{rid}/commit-request.txt",
    )
    (run_dir / "manifest.json").write_text(json.dumps(manifest.to_json()), encoding="utf-8")
    (root / "run.mjs").write_text(f"{ASK_MANIFEST_MARKER}{RUN_DIR_ROOT}/{rid}/manifest.json\n", encoding="utf-8")
    inline_review = {
        **reviewed(),
        "plan_id": "pln-x-123456", "prep_sidecar": None, "wave_sidecar_paths": [],
        "prep": {"run_base_sha": "a" * 40, "product_files": 1, "foreign_claims": [], "slice_files": ["a.py"]},
        "delivery": {"verdict": "PASS", "product_files": 1, "claims_unbacked": 0},
        "tests": dict(_PASS), "criterion": criterion,
    }
    record = root / "state" / "warp" / rid
    record.mkdir(parents=True)
    write_run_outcome(root, rid, {"inline_review": inline_review, "plan_path": "docs/plan.md"})
    if pre_halt:
        halted_run.write_halt_marker(record, halted="ledger-check", sizing_rel=None, blocking=("R1",))
    reply = terminal_commit._handler(
        {"script_path": "run.mjs", "incomplete_chunks": [], "inline_review": inline_review, "session_id": _SESSION},
        repo_root=root / ".git",
    )
    return {"root": root, "rid": rid, "record": record, "reply": reply}


def _try_landed(run: dict) -> dict:
    return landed_op._handler({"run_id": run["rid"], "sha": run["reply"]["sha"]}, run["root"])


def _plan_text(run: dict) -> str:
    return (run["root"] / "docs" / "plan.md").read_text(encoding="utf-8")


@pytest.fixture(autouse=True)
def _no_launch(monkeypatch):
    monkeypatch.setattr(landed_op, "_launch", lambda root, run_id: True)


@pytest.fixture(scope="module")
def runs(tmp_path_factory):
    base = tmp_path_factory.mktemp("completion-gate-e2e")
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(landed_op, "_launch", lambda root, run_id: True)
        fixtures = {
            "halt": _land(base, "halt", dict(_BACKED), pre_halt=True),
            "withheld": _land(base, "withheld", {**_BACKED, "withheld_clauses": [{"clause": "wired", "rows": ["C3"]}]}),
            "template": _land(base, "template", {"status": "met", "observation": "", "sidecar": "j.md"}),
            "nofalsifier": _land(base, "nofalsifier", {**_BACKED, "falsifier": {"verdict": "pass", "output": ""}}),
            "backed": _land(base, "backed", dict(_BACKED)),
        }
        backed = fixtures["backed"]
        ask_contract.write_run_stage_at(backed["record"], "landed")

        corpus = base / "corpus"
        for run in fixtures.values():
            shutil.copytree(run["record"], corpus / "state" / "warp" / run["rid"])
        control = corpus / "state" / "warp" / "run-control-unbacked"
        control.mkdir(parents=True)
        write_run_outcome(corpus, "run-control-unbacked", {"inline_review": {"criterion": {"status": "met"}}})
        halted_run.write_halt_marker(control, halted="ledger-check", sizing_rel=None)
        halted_run.mark_landed(control, "b" * 40)
        fixtures["_rows"] = {r["run_id"]: r for r in audit_mod.audit(str(corpus), True)}
        yield fixtures


@pytest.mark.parametrize("name", ["halt", "withheld", "template", "nofalsifier"])
def test_unbacked_run_commits_its_work_but_reports_halted_and_stamps_nothing(runs, name):
    run = runs[name]
    reply = run["reply"]

    assert reply["committed"] is True and reply["sha"], reply
    assert reply["completion_gate"]["blockers"], reply
    assert reply["plan_status"] == "not-stamped" and reply["plan_status_reason"].startswith("completion gate:")
    assert "exit_criterion_met" not in _plan_text(run) and "status: executing" in _plan_text(run)
    marker = halted_run.read_strict(run["record"])
    assert marker["status"] == halted_run.STATUS_HALTED


@pytest.mark.parametrize(
    "name,blocker",
    [
        ("halt", "halt standing"),
        ("withheld", "withheld clause: "),
        ("template", "verdict not judge-authored"),
        ("nofalsifier", "no captured falsifier output"),
    ],
)
def test_each_fixture_names_its_own_blocker(runs, name, blocker):
    assert any(b.startswith(blocker) for b in runs[name]["reply"]["completion_gate"]["blockers"])


@pytest.mark.parametrize("name", ["halt", "withheld", "template", "nofalsifier"])
def test_record_halted_run_landed_at_default_params_refuses_an_unbacked_run(runs, name):
    out = _try_landed(runs[name])

    assert out["ok"] is False and "halt reason stands" in out["error"], out
    assert halted_run.read_strict(runs[name]["record"])["status"] == halted_run.STATUS_HALTED


def test_backed_judge_authored_met_commits_with_no_gate_and_no_halt_marker(runs):
    run = runs["backed"]

    assert run["reply"]["committed"] is True and "completion_gate" not in run["reply"], run["reply"]
    assert halted_run.read_strict(run["record"]) is None


@pytest.mark.xfail(
    strict=True,
    reason=(
        "review_stamp.mint's criterion_block keeps status/observation/sidecar only (no authored_by, no falsifier) "
        "and records tests as build_test, so _stamp_plan_implemented's completion_blockers over the minted "
        "stamp always fires and a backed run never stamps exit_criterion_met"
    ),
)
def test_backed_judge_authored_met_stamps_exit_criterion_met(runs):
    run = runs["backed"]

    assert run["reply"]["plan_status"] == "implemented", {
        k: v for k, v in run["reply"].items() if k.startswith("plan_status") or k.startswith("coded")
    }
    assert "exit_criterion_met" in _plan_text(run)


def test_no_fixture_row_is_red_in_the_detector(runs):
    rows = runs["_rows"]
    fixture_rows = [rows[runs[n]["rid"]] for n in ("halt", "withheld", "template", "nofalsifier", "backed")]

    assert not [r["run_id"] for r in fixture_rows if r["red"]]


def test_only_the_backed_fixture_reads_committed_in_the_detector(runs):
    rows = runs["_rows"]

    terminals = {n: rows[runs[n]["rid"]]["terminal"] for n in ("halt", "withheld", "template", "nofalsifier", "backed")}
    assert terminals == {
        "halt": "halted", "withheld": "halted", "template": "halted", "nofalsifier": "halted", "backed": "committed",
    }
    assert rows[runs["backed"]["rid"]]["backed"] is True


def test_the_detector_still_reds_a_committed_unbacked_run(runs):
    control = runs["_rows"]["run-control-unbacked"]

    assert control["terminal"] == "committed" and control["red"] is True


def _node(source: str) -> str:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not on PATH")
    done = subprocess.run(
        [node, "-"], input=source, capture_output=True, text=True, check=True, timeout=30,
        **no_console_creationflags(),
    )
    return done.stdout


def _judge_rules(register_rows: list, pm_ids: list) -> dict:
    verdict = {
        "status": "not_met", "reason": "callers not switched", "differs_from_baseline": False,
        "observed": [{"kind": "command", "ref": "x", "result": "y"}],
        "clauses": [{"clause": "old op deleted", "status": "met", "observed_ref": "r"}],
        "falsifier": {"present": True, "ran": True, "verdict": "pass", "output_anchor": "ok"},
        "withheld_clauses": [{"clause": "wired", "rows": ["C12"], "register_rows": register_rows}],
    }
    call = f"{_JUDGE_VERDICT_RULES_JS}({json.dumps(verdict)}, {json.dumps(['C12'])}, {json.dumps(pm_ids)})"
    return json.loads(_node(f"const log = () => {{}}; console.log(JSON.stringify({call}));"))


def test_a_pm_verbatim_row_stays_not_met_through_the_reconcile_and_a_peer_row_still_flips():
    assert _judge_rules(["R1"], ["R1"])["status"] == "not_met"
    assert _judge_rules(["R2"], ["R1"])["status"] == "met"

"""A run whose product is already at HEAD, with a met criterion, closes through `dispatch.land_run`.

The run's reviewed diff holds zero files, so the review stamp mints only on a met criterion. The
sizing is a sibling slice whose shared PM turn no slice row covers: the register gate must leave the
judge's met criterion alone, so the close job mints and stamps the plan implemented with no re-judge.
"""

from __future__ import annotations

import copy
import json
import os
import re
import shutil
import subprocess

import pytest

from coordinator_core.benchmarks import terminal_commit_full_path as driver
from coordinator_core.ops.ceremony import close_background
from coordinator_core.ops.dispatch_emit import land_run
from coordinator_core.ops.dispatch_emit.commit_request import ChunkCommit, CommitRequest, render_marker
from coordinator_core.ops.dispatch_emit.run_close import close_job_path
from coordinator_core.win_portability import no_console_creationflags
from pathlib import Path

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

PLAN_REL = "docs/plans/plan.md"
SIZING_REL = "state/sizings/2026-10-16-slice.yaml"

# The pm-verbatim intent cites turn 29, so a non-slice owes it; no row covers it.
_SIZING = """schema: sizing-object
intent: please stop leaving things still owed
intent_source: pm-verbatim
estimate:
  tshirt: S
  provisional: true
route: spec-dispatch
detents: []
fork: null
xl_exit: null
status: routed
premise:
  provenance: read
  evidence: fixture
{parent}pm_verbatims:
  - {{turn: 29, text: 'please stop leaving things still owed'}}
  - {{turn: 30, text: 'ship the slice row'}}
requirement_register:
  sources:
    - {{doc: docs/fixture.md, ref: fixture}}
  rows:
    - {{id: R1, source_text: ship the slice row, anchor: a1, surface: api, status: open, claimed_by: [docs/plans/plan.md], wired: false}}
"""

_PLAN_HEAD = f"""---
title: p
status: executing
plan_id: {driver.PLAN_ID}
sizing_object: {SIZING_REL}
register_claims:
  sizing: {SIZING_REL}
  rows: [R1]
prime_exit_criterion:
  text: a.py exists
---
"""


def _git(repo, *args) -> None:
    subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True, **no_console_creationflags()
    )


def _review() -> dict:
    review = copy.deepcopy(driver.INLINE_REVIEW)
    review["prep"] = {"run_base_sha": "a" * 40, "product_files": 0, "foreign_claims": [], "slice_files": []}
    review["delivery"] = {"verdict": "PASS", "product_files": 0, "claims_unbacked": 0}
    review["criterion"]["register_rows"] = [
        {"id": "R1", "claim": "this-plan", "status": "met", "met_by": "a.py", "wired": True, "surface": "api",
         "command": "python a.py", "observed": "a.py ran", "observed_ref": "a.py::f"}
    ]
    return review


def _landed(tmp_path, monkeypatch, *, slice_sizing: bool) -> dict:
    from coordinator_core.session import claims

    parent = tmp_path / "work"
    parent.mkdir()
    repo, script = driver.build_fixture(parent)
    plan = (repo / driver.PLAN_REL).read_text(encoding="utf-8")
    body = plan.split("---\n", 2)[2]
    body = body.replace("  disposition: open\n", "  disposition: open\n  register_rows: [R1]\n  writes: [a.py]\n")
    (repo / PLAN_REL).parent.mkdir(parents=True)
    (repo / driver.PLAN_REL).unlink()
    (repo / PLAN_REL).write_text(_PLAN_HEAD + body, encoding="utf-8")
    request = CommitRequest(chunks=(ChunkCommit(id="C3", title="t3", paths=("a.py",)),), plan_path=PLAN_REL)
    (repo / script).write_text("// emitted script\n" + render_marker(request) + "\n", encoding="utf-8")
    (repo / SIZING_REL).parent.mkdir(parents=True)
    parent_line = "em_analysis:\n  ask_parent: ask-parent-x ask-x\n" if slice_sizing else ""
    (repo / SIZING_REL).write_text(_SIZING.format(parent=parent_line), encoding="utf-8")
    _git(repo, "add", "-A", "docs", "state", "a.py")
    _git(repo, "commit", "-q", "-m", "slice sizing and product")
    outbox = parent / "lessons-outbox"
    outbox.mkdir()
    monkeypatch.setenv("LESSON_PROMOTE_OUTBOX_ROOT", str(outbox))
    monkeypatch.setenv("COORDINATOR_SESSION_ID", driver.SESSION_ID)
    monkeypatch.setattr(land_run, "_launch_close_job", lambda *a, **k: True)
    monkeypatch.setattr(
        claims, "list_claims_by_session_checked", lambda sid, cwd=None: ([("handoff", "b.md")], [])
    )
    monkeypatch.setattr(close_background, "_launch_close_review", lambda ctx: True)
    out = land_run._handler(
        {"script_path": script, "incomplete_chunks": [], "session_id": driver.SESSION_ID,
         "inline_review": _review(), "allow_empty": True},
        repo_root=repo / ".git",
    )
    assert out.get("committed") is True and out["sha"], out
    return {"repo": repo, "out": out}


def _close(env) -> dict:
    """Run the landing's close job in-process; return its settled leg statuses and details."""
    repo, run_id = env["repo"], env["out"]["close"]["run_id"]
    record = json.loads((repo / close_job_path(run_id)).read_text(encoding="utf-8"))
    close_background._handler(
        {"run_id": run_id, "claimed_batons": record["claimed_batons"], "source_memo": record.get("source_memo")},
        repo_root=repo / ".git",
    )
    settled = json.loads((repo / close_job_path(run_id)).read_text(encoding="utf-8"))
    return {leg: (r["status"], r["detail"]) for leg, r in settled["legs"].items()}


def _rejudge_records(repo) -> list:
    root = repo / ".coordinator-local"
    if not root.exists():
        return []
    return [p for p in root.rglob("*") if p.is_file() and "rejudge" in p.name]


def test_run_with_product_at_head_and_met_criterion_closes_in_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env = _landed(tmp_path, monkeypatch, slice_sizing=True)
    repo = env["repo"]

    legs = _close(env)

    assert legs["review-stamp"][0] == "ran", legs
    text = (repo / PLAN_REL).read_text(encoding="utf-8")
    assert "status: implemented" in text and "review_stamp:" in text, json.dumps(legs, indent=2)
    assert "status: met" in text.split("review_stamp:", 1)[1]
    assert _rejudge_records(repo) == []


def test_same_run_without_ask_parent_leaves_the_plan_unimplemented_on_the_uncovered_turn(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Without an ask-parent no register row covers PM turn 29: the close job's plan-reconciliation leg
    fails naming the turn and the plan stays unimplemented (the old refire refused at the stamp instead)."""
    env = _landed(tmp_path, monkeypatch, slice_sizing=False)
    repo = env["repo"]

    legs = _close(env)

    assert legs["plan-reconciliation"][0] == "failed", legs
    assert "PM turn 29 is covered by no register row" in legs["plan-reconciliation"][1]
    assert "status: implemented" not in (repo / PLAN_REL).read_text(encoding="utf-8")


def test_zero_file_pass_anchor_lands_an_empty_delta_commit_carrying_the_review_trailer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import hashlib

    env = _landed(tmp_path, monkeypatch, slice_sizing=True)
    repo = env["repo"]
    before = env["out"]["sha"]
    (repo / "anchor.mjs").write_text("// emitted\n", encoding="utf-8")
    review = _review()
    review["integration_stem"] = f"{driver.PLAN_ID}.review-wave-bookkeeping"
    review["prep_sidecar"] = None
    review["wave_sidecar_paths"] = []

    out = land_run._handler(
        {"script_path": "anchor.mjs", "plan_path": PLAN_REL, "incomplete_chunks": [],
         "session_id": "99999999-2222-3333-4444-555555555555", "inline_review": review},
        repo_root=repo / ".git",
    )

    assert out.get("committed") is True and out["sha"] != before, out
    body = subprocess.run(
        ["git", "show", "-s", "--format=%B", out["sha"]], cwd=repo, capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    ).stdout
    record = next((repo / ".coordinator-local" / "subagent-share" / "99999999-2222-3333-4444-555555555555").glob("*.md"))
    assert f"Review: {hashlib.sha256(record.read_bytes()).hexdigest()}" in body


@pytest.mark.parametrize(
    "integration, expected",
    [(None, {}), ({"unresolved": [], "confinement_violations": []}, {"C1": "ledger-met: R1, R2"})],
)
def test_delivered_report_gets_ledger_credit_only_with_a_clean_integration_verdict(tmp_path, integration, expected):
    from types import SimpleNamespace

    (tmp_path / "report.md").write_text("status: DONE\n", encoding="utf-8")
    request = CommitRequest(chunks=(ChunkCommit(id="C1", title="t", paths=("a.py",), report="report.md"),))
    review = {"prep": {"slice_files": ["a.py"]}, "delivery": {"verdict": "PASS"}}
    if integration is not None:
        review["integration"] = integration
    got = land_run._grade(
        tmp_path, request, {"C1"}, review, SimpleNamespace(items=[]),
        {"items": [{"id": "R1", "status": "met", "met_by": "a.py"}, {"id": "R2", "status": "met", "met_by": "a.py"}]},
        set(), None, None, None, None, {},
    )
    assert got == expected


@pytest.mark.parametrize("case", ["held", "unreviewed", "unresolved", "no-ledger", "blocked", "no-delivery", "delivery-failed"])
def test_delivery_credit_still_requires_row_evidence(tmp_path, case):
    from types import SimpleNamespace

    (tmp_path / "report.md").write_text("BLOCKED: C1\n" if case == "blocked" else "DONE: C1\n", encoding="utf-8")
    request = CommitRequest(chunks=(ChunkCommit(id="C1", title="t", paths=("a.py",), report="report.md"),))
    review = {"prep": {"slice_files": [] if case == "unreviewed" else ["a.py"]},
              "delivery": {"verdict": "PASS"}}
    if case == "unresolved":
        review["integration"] = {"unresolved": ["unfinished"]}
    if case == "no-delivery":
        review.pop("delivery")
    if case == "delivery-failed":
        review["delivery"] = {"verdict": "FAIL"}
    assert land_run._grade(
        tmp_path, request, {"C1"}, review, None if case == "no-ledger" else SimpleNamespace(items=[]),
        {"items": [{"id": "R1", "status": "met", "met_by": "a.py"}]}, {"C1"} if case == "held" else set(),
        None, None, None, None, {},
    ) == {}


_LIVE_AGENTS = r"""
const fs = require('fs');
const cfg = JSON.parse(process.env.CFG);
const calls = [];
function ask(request) {
  console.log(JSON.stringify(request));
  let line = '', byte = Buffer.alloc(1);
  while (fs.readSync(0, byte, 0, 1, null)) { if (byte[0] === 10) break; line += byte.toString(); }
  return JSON.parse(line);
}
const agent = async (prompt, opts) => {
  calls.push(opts.label);
  return ask({label: opts.label, type: opts.agentType, prompt: String(prompt)});
};
const workflow = async (spec) => {
  const body = fs.readFileSync(spec.scriptPath, 'utf8').replace(/^export const meta[^\n]*\n/, '');
  return await new AsyncFunction(body)();
};
const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;
const src = fs.readFileSync(cfg.script, 'utf8').replace(/^export const meta/m, 'const meta');
new AsyncFunction('args', 'agent', 'parallel', 'pipeline', 'phase', 'workflow', 'log', src)(
  {runToken: cfg.token}, agent, async xs => Promise.all(xs.map(f => f())), async () => ({}),
  () => {}, workflow, () => {}
).then(result => console.log(JSON.stringify({result, calls}))).catch(e => {
  console.log(JSON.stringify({error: String(e.stack)})); process.exitCode = 1;
});
"""


def test_default_sizing_refire_delivers_discharge_and_consistent_result(tmp_path, monkeypatch):
    import yaml
    from coordinator_core.ops.dispatch_emit import ask_compose, ask_gate, op, requirements_ledger as rl
    from coordinator_core.ops.dispatch_emit import emit
    from coordinator_core.ops.dispatch_emit.terminal_commit_params_channel import prompt_params
    from coordinator_core.ops.dispatch_emit.tests.test_ask_run_product_commit import _git as git, _write
    from coordinator_core.ops.dispatch_emit.tests.test_ask_compose_stage_handle import agent_gate_reply
    from coordinator_core.ops.dispatch_emit.halted_run import write_halt_marker
    from coordinator_core.ops.dispatch_emit.ask_contract import record_dir
    from coordinator_core.ops.dispatch_emit.sizing_fire import s_plan_path

    node = shutil.which("node")
    if node is None:
        pytest.skip("node unavailable")
    root = tmp_path / "repo"
    root.mkdir()
    plan_rel = s_plan_path(SIZING_REL)
    monkeypatch.chdir(root)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", driver.SESSION_ID)
    monkeypatch.setattr(land_run, "_launch_close_job", lambda *a, **k: True)
    monkeypatch.setattr(ask_compose, "_read_plan_blitz", lambda: "return {ready: []};")
    git(root, "init", "-q")
    git(root, "config", "user.email", "t@example.com")
    git(root, "config", "user.name", "t")
    git(root, "config", "commit.gpgsign", "false")
    _write(root, ".gitignore", "scratch/\n.coordinator-local/\n")
    _write(root, plan_rel, _PLAN_HEAD.replace("rows: [R1]", "rows: [R1, R2]") + "\n## Tasks\n\n```yaml plan-tasks\n- id: C1\n"
           "  title: deliver a\n  disposition: open\n  change_kind: script-edit\n  surface: a.py\n"
           "  agent_type: coordinator:executor\n  body: deliver a\n  register_rows: [R1, R2]\n"
           "  writes: [a.py]\n```\n")
    doc = {"schema": "sizing-object", "name": "incident", "intent": "deliver a", "plan": plan_rel,
           "estimate": {"tshirt": "S", "provisional": True}, "route": "spec-dispatch", "detents": [],
           "fork": None, "xl_exit": None, "status": "routed", "interaction_mode": "ceo",
           "premise": {"provenance": "read", "evidence": "fixture"},
           "exit_criterion": {"statement": "a delivered", "accepted": None},
           "requirement_register": {"sources": [{"doc": SIZING_REL, "ref": "ask"}], "rows": [
               {"id": rid, "source_text": "deliver a", "anchor": "ask", "surface": "internal",
                "status": "open", "claimed_by": [plan_rel]} for rid in ("R1", "R2")]}}
    _write(root, SIZING_REL, yaml.safe_dump(doc, sort_keys=False))
    git(root, "add", ".")
    git(root, "commit", "-qm", "seed")
    prior = "warp-2026-10-16-slice-20261116T000000-aaaaaa"
    rl.start(root, prior, all_of_pm_ask=False)
    rl.bind(root, prior, SIZING_REL)
    write_halt_marker(record_dir(root, prior), halted="criterion", sizing_rel=SIZING_REL)
    emitted = op._dispatch_emit({"ask": True, "sizing_path": SIZING_REL}, repo_root=root)
    run_id = emitted["run_id"]
    assert run_id == prior, emitted
    script = root / "scratch" / "warp" / f"{run_id}.workflow.mjs"
    gate = agent_gate_reply(root, ask_gate._handler({"sizing_path": SIZING_REL, "run_id": run_id}, repo_root=root))
    assert gate.get("resume_plan") == plan_rel, json.dumps(gate, indent=2)
    verdict = {"items": [{"id": rid, "status": "met", "met_by": "a.py", "wired": True,
                          "command": "python a.py", "observed": "a.py ran"} for rid in ("R1", "R2")]}
    cfg = {"script": str(script), "token": emitted["fire_args"]["runToken"]}
    landed = None
    output: dict = {"error": "node driver exited without a result line"}
    with subprocess.Popen(
        [node, "-e", _LIVE_AGENTS], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", env={**os.environ, "CFG": json.dumps(cfg)}, **no_console_creationflags(),
    ) as proc:
        for line in proc.stdout:
            request = json.loads(line)
            if "label" not in request:
                output = request
                break
            label = request["label"]
            reply = {}
            if label in ("gate", "gate-stage"):
                reply = gate
            elif label.startswith("work:"):
                _write(root, "a.py", "a = 1\n")
                _write(root, emit._dispatch_report_path(plan_rel, "C1"), "status: DONE\n")
                reply = "PARTIAL: C1 — waiting for run verification"
            elif label == "verify":
                reply = {"items": [{"id": "R1", "status": "not_met", "essential": True,
                                    "remaining": ["a.py still needs the remnant fix"]}, verdict["items"][1]]}
            elif label.startswith("ledger-fix:"):
                reply = {"status": "BLOCKED", "paths": [], "reason": "requires the remnant pass"}
            elif label == "remnant-complete":
                _write(root, "a.py", "a = 2\n")
                reply = {"status": "DONE", "paths": ["a.py"], "reason": "delivered"}
            elif label == "remnant-reverify":
                reply = {**verdict, "delivery": {"verdict": "PASS", "reason": "delivered"}}
            elif label in ("ledger-recovery-record", "remnant-record"):
                match = re.search(r"(\{[^\n]*\})\. Edit nothing else", request["prompt"])
                assert match, request["prompt"]
                path = record_dir(root, run_id) / "recovery.json"
                record = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
                change = json.loads(match[1])
                record = {**record, "remnant": change} if label == "remnant-record" else change
                path.write_text(json.dumps(record), encoding="utf-8")
                reply = {"written": True}
            elif request.get("type") == "coordinator:review-prep":
                reply = {"slice_files": ["a.py"], "foreign_claims": [], "product_files": 1}
            elif request.get("type") == "coordinator:code-reviewer":
                _write(root, "review.md", "PASS\n")
                reply = {"prep": {"slices": [{"id": "S1", "files": ["a.py"]}], "slice_files": ["a.py"],
                                  "product_files": 1, "foreign_claims": []},
                         "review": {"slice_id": "S1", "verdict": "OK", "findings": 0, "applied": 0,
                                    "out_of_slice": 0, "sidecar_path": "review.md"}}
            elif label == "review:coordinator:op-relay":
                reply = {"status": "pass", "tests_run": 1, "tests_failed": 0}
            elif request.get("type") == "coordinator:test-runner":
                reply = {"status_before": "pass", "status_after": "pass", "failing": [], "reverted": []}
            elif request.get("type") == "coordinator:integrator":
                reply = {"unresolved": [], "confinement_violations": []}
            elif request.get("type") == "coordinator:delivery-verifier":
                reply = {"verdict": "PASS", "claims_unbacked": 0}
            elif label == "commit":
                params = prompt_params(request["prompt"])
                assert params["incomplete_chunks"] == ["C1"], params
                assert params["inline_review"]["delivery"]["verdict"] == "PASS", params
                assert params["inline_review"]["prep"]["slice_files"] == ["a.py"], params
                assert not params["inline_review"].get("integration"), params
                params["inline_review"]["integration"] = {"unresolved": [], "confinement_violations": []}
                assert [i["status"] for i in params["ledger_verdict"]["items"]] == ["met", "met"], params
                landed = land_run._handler(params, repo_root=root / ".git")
                assert landed.get("committed") is True, landed
                reply = landed
            proc.stdin.write(json.dumps(reply) + "\n")
            proc.stdin.flush()
        proc.stdin.close()
        assert proc.wait(timeout=20) == 0, proc.stderr.read()
    assert "error" not in output, output
    assert landed is not None, json.dumps(output, indent=2)
    result = output["result"]
    assert "plan" not in output["calls"]
    assert {"ledger-fix:C1", "remnant-complete", "remnant-reverify", "commit"} <= set(output["calls"])
    assert result["commit"]["sha"] == landed["sha"]
    assert result["incomplete"] == [], json.dumps(landed, indent=2)
    assert result["next_action"]["kind"] == "close_job", result
    assert result["revise_and_refire"] is None
    assert git(root, "show", f"{landed['sha']}:a.py") == "a = 2\n"
    legs = _close({"repo": root, "out": landed})
    assert legs["coded-stamp"][0] == "ran", legs
    spine = (root / plan_rel).read_text(encoding="utf-8")
    assert "disposition: coded" in spine and landed["sha"] in spine
    assert "disposition: coded" in git(root, "show", f"HEAD:{plan_rel}")

"""The warp-2026-11-20-def60d-3 refire rebuilt under tmp_path: plan C0..C6, C0/C1 landed, resume_rows
[C2, C4, C5, C6], the plan-blitz stage read answered `already_staged` by the real `dispatch.ask_stage`
receipt. The composed script runs under node with every agent leg stubbed by label.

Designed red until the compose fold and the stage reply honour the resume rows."""

from __future__ import annotations

import json
import os
import shutil
import subprocess

import pytest

from coordinator_core.ops.dispatch_emit import ask_compose, ask_gate, ask_stage, ask_stage_receipt
from coordinator_core.ops.dispatch_emit.ask_compose import compose_ask_script
from coordinator_core.ops.dispatch_emit.ask_contract import HALT_NO_DISPATCHABLE_EXECUTOR, record_dir
from coordinator_core.ops.dispatch_emit.ask_stage_receipt import ALREADY_STAGED_KEY
from coordinator_core.ops.dispatch_emit.reply_sidecar import read_reply, write_reply
from coordinator_core.ops.dispatch_emit.review_floor import HALT_REVIEW_FLOOR, no_delta_code_paths
from coordinator_core.ops.dispatch_emit.tests._node_support import payload_file
from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW, write_run_scripts
from coordinator_core.ops.dispatch_emit.tests.test_ask_compose_stage_handle import (
    WORKFLOW_FAKE_JS,
    agent_gate_reply,
    readers_cfg,
)
from coordinator_core.ops.dispatch_emit.tests.test_ask_gate import ACCEPTED, REL, _put, repo  # noqa: F401
from coordinator_core.ops.dispatch_emit.tests.test_ask_gate_resume import PLAN_REL
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

RUN = "warp-2026-11-20-def60d-3-warm-collection-opens-20261120T231514-8cb88f"
TOKEN = "0123456789abcdef0123456789abcdef"
ALL_ROWS = ("C0", "C1", "C2", "C3", "C4", "C5", "C6")
LANDED = ("C0", "C1")
RESUME_ROWS = ["C2", "C4", "C5", "C6"]
COMMITTED = {"committed": True, "sha": "abc1234", "error": None, "refused": None}

_NODE_PROGRAM = WORKFLOW_FAKE_JS + r"""
const src = require('fs').readFileSync(process.env.SRC_FILE, 'utf8').replace(/^export const meta/m, 'const meta');
const cfg = JSON.parse(process.env.CFG);
const workflow = makeWorkflow(cfg);
const calls = [];
const agent = async (prompt, opts) => {
  const label = opts && opts.label;
  calls.push({ label, prompt: String(prompt) });
  if (label === 'halt-commit' || label === 'commit') return cfg.commit;
  if (cfg.replies[label] !== undefined) return cfg.replies[label];
  return {};
};
const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;
const run = new AsyncFunction('args', 'agent', 'parallel', 'pipeline', 'phase', 'workflow', src);
run({ runToken: cfg.token }, agent, async (xs) => Promise.all(xs.map((f) => f())), async () => ({}), () => {}, workflow)
  .then((result) => console.log(JSON.stringify({ calls, result })))
  .catch((e) => { console.error(String((e && e.stack) || e)); process.exit(1); });
"""


def _git(root, *args):
    subprocess.run(["git", *args], cwd=str(root), check=True, capture_output=True, **no_console_creationflags())


def _refire_repo(root):
    """C0/C1 landed by a checkpoint commit; C3 coded; C2, C4, C5, C6 open code rows with no delta."""
    _put(root, tshirt="M", route="plan", accepted=ACCEPTED, plan=PLAN_REL)
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@t.example")
    _git(root, "config", "user.name", "t")
    body = "".join(
        f"- id: {i}\n  title: T{i}\n  change_kind: code-edit\n  surface: some/{i}.py\n"
        f"  disposition: {'coded' if i == 'C3' else 'open'}\n  writes:\n    - some/{i}.py\n"
        for i in ALL_ROWS
    )
    plan = root / PLAN_REL
    plan.parent.mkdir(parents=True, exist_ok=True)
    plan.write_text(f"---\nrun_id: fixture\n---\n\n## Tasks\n\n```yaml plan-tasks\n{body}```\n", encoding="utf-8", newline="\n")
    (root / "some").mkdir(exist_ok=True)
    for i in ALL_ROWS:
        (root / "some" / f"{i}.py").write_text("x = 1\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "base")
    for i in LANDED:
        (root / "some" / f"{i}.py").write_text("x = 2\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", f"checkpoint(wave 1): {len(LANDED)} rows — {', '.join(LANDED)}",
         "-m", f"Checkpoint-Plan: {PLAN_REL}\nCheckpoint-Base: abc1234")
    write_run_scripts(root, RUN)


def _already_staged_reply(root, recorded=None) -> dict:
    """The plan-blitz stage read's params, sent twice: the second reply is the receipt's `already_staged`.
    `recorded` replaces the first stage's manifest under a receipt that vouches for it."""
    params = {"run_id": RUN, "plan_path": PLAN_REL, "cross_repo_approved": False, "commit_sizing_path": REL}
    first = ask_stage._handler(dict(params), repo_root=root)
    assert "manifest_handle" in first and ALREADY_STAGED_KEY not in first, first
    if recorded is not None:
        rec = record_dir(root, RUN)
        manifest = {**read_reply(root, first["manifest_handle"]), **recorded}
        handle = write_reply(root, rec / "manifest.json", manifest)
        ask_stage_receipt.write(rec, ask_stage_receipt.params_digest(params), handle, ask_stage._spine_sha(root, PLAN_REL))
    again = ask_stage._handler(dict(params), repo_root=root)
    assert again.get(ALREADY_STAGED_KEY) is True, again
    return {k: again[k] for k in ("manifest_handle", ALREADY_STAGED_KEY)}


def _blitz_fn() -> str:
    return f"  async function planBlitz(args) {{\n    return {{ ready: [{json.dumps({'planPath': PLAN_REL})}] }};\n  }}"


def _run_composed(root, monkeypatch, recorded=None) -> tuple[dict, dict]:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not on PATH")
    monkeypatch.setattr(ask_compose, "_known_arm", lambda *_: "m_plus")
    gate = ask_gate._handler({"sizing_path": REL, "run_id": RUN}, repo_root=root)
    assert gate.get("halt") is None and gate["resume_rows"] == RESUME_ROWS, gate
    staged = _already_staged_reply(root, recorded)
    script = compose_ask_script(
        repo_root="REPO", prompt=None, sizing_rel=REL, run_id=RUN, session_id=None,
        wrap_stage=lambda _t: (_blitz_fn(), ["Size", "Plan"]), plan_blitz_text="stub",
        script_path="scratch/warp/x.mjs", in_run_commit=True, **REVIEW_KW,
    )
    gate_reply = agent_gate_reply(root, gate)
    cfg = {
        "token": TOKEN, "commit": COMMITTED, **readers_cfg(root),
        "replies": {"gate": gate_reply, "gate-stage": gate_reply, "blitz-stage-read": {"stage_reply": staged}},
    }
    proc = subprocess.run(
        [node, "-"], input=_NODE_PROGRAM, capture_output=True, text=True, encoding="utf-8", timeout=60,
        env={**os.environ, "SRC_FILE": payload_file(script), "CFG": json.dumps(cfg)}, **no_console_creationflags(),
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1]), staged


STAGED_NONE_OF_THE_RESUME_ROWS = {"rows": [], "owed": [{"id": "C3", "reason": "memo-send"}], "gated": []}


def _labels(out: dict) -> list[str]:
    return [c["label"] for c in out["calls"]]


def _dispatched(out: dict) -> set[str]:
    """Row ids that got an executor leg (`work:g<wave>-<id>`)."""
    return {lab.split("-", 1)[1] for lab in _labels(out) if lab and lab.startswith("work:g")}


def test_the_gate_derives_the_resume_rows_with_no_ledger_so_the_ledger_derivation_cause_is_ruled_out(repo, monkeypatch):  # noqa: F811
    """The 2026-11-17 mechanism (a ledger resume whose derivation set `_xsTail`) needs `resume_ledger`; this gate has none."""
    _refire_repo(repo)
    monkeypatch.setattr(ask_compose, "_known_arm", lambda *_: "m_plus")
    gate = ask_gate._handler({"sizing_path": REL, "run_id": RUN}, repo_root=repo)
    assert gate.get("halt") is None and not gate.get("resume_ledger"), gate
    assert gate["resume_plan"] == PLAN_REL and gate["resume_rows"] == RESUME_ROWS, gate


def test_an_already_staged_reply_over_a_manifest_that_covers_the_resume_rows_dispatches_their_executors(repo, monkeypatch):  # noqa: F811
    """The shortcut stays right when the recorded manifest stages the unexecuted rows."""
    _refire_repo(repo)
    out, staged = _run_composed(repo, monkeypatch)
    assert staged[ALREADY_STAGED_KEY] is True
    assert [r["id"] for r in read_reply(repo, staged["manifest_handle"])["rows"]] == RESUME_ROWS
    assert {"C2", "C5"} <= _dispatched(out), _labels(out)


def _recorded_stage(root, recorded, only_rows) -> dict:
    """Stage once, replace the recorded manifest with `recorded` under a vouching receipt, stage again with `only_rows`."""
    params = {"run_id": RUN, "plan_path": PLAN_REL, "cross_repo_approved": False, "commit_sizing_path": REL,
              "only_rows": only_rows}
    first = ask_stage._handler(dict(params), repo_root=root)
    rec = record_dir(root, RUN)
    handle = write_reply(root, rec / "manifest.json", {**read_reply(root, first["manifest_handle"]), **recorded})
    ask_stage_receipt.write(rec, ask_stage_receipt.params_digest(params), handle, ask_stage._spine_sha(root, PLAN_REL))
    return ask_stage._handler(dict(params), repo_root=root)


@pytest.mark.parametrize("recorded", [
    STAGED_NONE_OF_THE_RESUME_ROWS,
    {"rows": [{"id": "C2"}], "owed": [], "gated": []},
])
def test_a_recorded_manifest_leaving_a_resume_row_unstaged_and_unowed_restages_it(repo, recorded):  # noqa: F811
    _refire_repo(repo)
    again = _recorded_stage(repo, recorded, RESUME_ROWS)
    assert ALREADY_STAGED_KEY not in again, again
    assert [r["id"] for r in read_reply(repo, again["manifest_handle"])["rows"]] == RESUME_ROWS


def test_a_recorded_manifest_covering_every_resume_row_stays_already_staged(repo):  # noqa: F811
    _refire_repo(repo)
    recorded = {"rows": [{"id": "C2"}], "owed": [{"id": i, "reason": "memo-send"} for i in RESUME_ROWS[1:]], "gated": []}
    again = _recorded_stage(repo, recorded, RESUME_ROWS)
    assert again.get(ALREADY_STAGED_KEY) is True, again


@pytest.mark.designed_red
def test_a_row_with_no_delta_and_no_executor_is_never_reported_done(repo, monkeypatch):  # noqa: F811
    _refire_repo(repo)
    out, _ = _run_composed(repo, monkeypatch, STAGED_NONE_OF_THE_RESUME_ROWS)
    res = out["result"]
    changed: list[str] = []
    not_done = set(res["incomplete"]) | set((res["halted_by"] or {}).get("stopped_rows") or ())
    reported_done = [
        r for r in RESUME_ROWS
        if no_delta_code_paths([f"some/{r}.py"], changed) and r not in not_done
    ]
    assert reported_done == [], f"rows reported done with no delta and no landed evidence: {reported_done}"


@pytest.mark.designed_red
def test_with_no_dispatchable_executor_the_run_halts_typed_before_any_review_leg(repo, monkeypatch):  # noqa: F811
    _refire_repo(repo)
    out, _ = _run_composed(repo, monkeypatch, STAGED_NONE_OF_THE_RESUME_ROWS)
    assert not _dispatched(out), _labels(out)
    halted = out["result"]["halted_by"]
    assert halted is not None and halted.get("kind") == HALT_NO_DISPATCHABLE_EXECUTOR, halted
    assert halted["halted"] != HALT_REVIEW_FLOOR
    labels = _labels(out)
    assert not [lab for lab in labels[: labels.index("halt-commit")] if lab and lab.startswith("review:")], labels

"""The example-game-repo shape end to end: every plan row coded, a register row open, no ledger pointer. The real gate
resumes it as an all-landed ledger resume; the composed script (agent legs stubbed under node) reaches verify
then commit with no executor; a not_met judge lands a ledger-check halt whose pointer makes the next gate call
a ledger resume bound to that run and carrying its remaining (every row stays coded, so the pointer reads as
stale and the resume is kind all-landed), not the already-ran refusal."""

from __future__ import annotations

import json
import os
import shutil
import subprocess

import pytest

from coordinator_core.ops.dispatch_emit import ask_compose, terminal_commit
from coordinator_core.ops.dispatch_emit.ask_compose import compose_ask_script
from coordinator_core.ops.dispatch_emit.ask_contract import (
    ALL_LANDED_HALT_KIND,
    HALTED_RECORD_PARAM,
    RECORD_DIR_ROOT,
    RESUME_LEDGER_KIND_FIELD,
    ledger_halt_path,
    read_ledger_halt,
)
from coordinator_core.ops.dispatch_emit.ask_gate import _handler, gate
from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW
from coordinator_core.ops.dispatch_emit.tests.test_ask_gate import REL, repo  # noqa: F401
from coordinator_core.ops.dispatch_emit.tests.test_ask_gate_resume import PLAN_REL
from coordinator_core.ops.dispatch_emit.tests.test_ledger_halt_resume_gate import ALREADY_RAN, _landed_repo, _put_s
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_RUN = "ask-20261018T000000-aaaaaa"
_TOKEN = "0123456789abcdef0123456789abcdef"
_BLITZ_FN = "  async function planBlitz(args) {\n    return { ready: [] };\n  }"
_REGISTER = {"rows": [{"id": "R1", "status": "open"}, {"id": "R2", "status": "met"}]}
_CODED = (("A1", "coded"), ("A2", "coded"))
_LANDED = {"committed": True, "sha": "abc1234", "error": None, "refused": None}
_MET = {"items": [{"id": "R1", "status": "met", "wired": True}]}
_UNMET = {"items": [{"id": "R1", "status": "not_met", "essential": True, "remaining": ["the criterion is red"]}]}
_NODE_PROGRAM = r"""
const src = process.env.SRC.replace(/^export const meta/m, 'const meta');
const cfg = JSON.parse(process.env.CFG);
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
const run = new AsyncFunction('args', 'agent', 'parallel', 'pipeline', 'phase', src);
run({ runToken: cfg.token }, agent, async (xs) => Promise.all(xs.map((f) => f())), async () => ({}), () => {})
  .then((result) => console.log(JSON.stringify({ calls, result })))
  .catch((e) => { console.error(String((e && e.stack) || e)); process.exit(1); });
"""


def _git(root, *a):
    subprocess.run(["git", *a], cwd=str(root), check=True, capture_output=True, **no_console_creationflags())


def _example_game_repo_shape(root):
    """Every spine row coded and landed, a register row open, no ledger pointer; the sizing committed."""
    _put_s(root, requirement_register=_REGISTER)
    _landed_repo(root, _CODED)
    _git(root, "add", REL)
    _git(root, "commit", "-q", "-m", "sizing")


def _run_composed(root, monkeypatch, verify) -> dict:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not on PATH")
    gate_reply = _handler({"sizing_path": REL, "run_id": _RUN}, repo_root=root)
    monkeypatch.setattr(ask_compose, "_known_arm", lambda *_: "s")
    script = compose_ask_script(
        repo_root="REPO", prompt=None, sizing_rel=REL, run_id=_RUN, session_id=None,
        wrap_stage=lambda _t: (_BLITZ_FN, ["Size", "Plan"]), plan_blitz_text="stub",
        script_path="scratch/warp/x.mjs", in_run_commit=True, **REVIEW_KW,
    )
    cfg = {
        "token": _TOKEN, "verify": verify, "commit": _LANDED,
        "replies": {"gate": gate_reply, "gate-stage": gate_reply},
    }
    proc = subprocess.run(
        [node, "-e", _NODE_PROGRAM], capture_output=True, text=True, encoding="utf-8", timeout=60,
        env={**os.environ, "SRC": script, "CFG": json.dumps(cfg)}, **no_console_creationflags(),
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_the_real_gate_resumes_the_example_game_repo_shape_with_a_whole_zero_row_manifest(repo):  # noqa: F811
    _example_game_repo_shape(repo)
    out = _handler({"sizing_path": REL, "run_id": _RUN}, repo_root=repo)
    assert out["halt"] is None, out["halt"]
    assert out["resume_ledger"][RESUME_LEDGER_KIND_FIELD] == ALL_LANDED_HALT_KIND
    assert out["manifest"].get("error") is None and out["manifest"]["rows"] == []


def test_an_all_done_refire_reaches_verify_then_commit_with_no_executor(repo, monkeypatch):  # noqa: F811
    _example_game_repo_shape(repo)
    out = _run_composed(repo, monkeypatch, _MET)
    labels = [c["label"] for c in out["calls"]]
    res = out["result"]
    assert res["halted_by"] is None, res["halted_by"]
    assert "no folded stage manifest" not in json.dumps(res)
    assert labels.index("verify") < labels.index("commit")
    assert not {"plan", "exec", "ledger-remaining", "ledger-replan"} & set(labels), labels
    assert res["commit"]["committed"] is True


def test_a_not_met_judge_lands_a_ledger_check_halt_and_the_next_gate_resumes_it_not_the_already_ran_refusal(
    repo, monkeypatch  # noqa: F811
):
    _example_game_repo_shape(repo)
    out = _run_composed(repo, monkeypatch, _UNMET)
    halted_by = out["result"]["halted_by"]
    labels = [c["label"] for c in out["calls"]]
    assert halted_by["halted"] == "ledger-check", halted_by
    assert "no folded stage manifest" not in json.dumps(halted_by)
    assert labels.index("verify") < labels.index("halt-commit") and "commit" not in labels

    rec = repo / RECORD_DIR_ROOT / _RUN
    rec.mkdir(parents=True, exist_ok=True)
    (rec / "run.json").write_text(json.dumps({"sizing_rel": REL}) + "\n", encoding="utf-8")
    landed = terminal_commit._handler(
        {"script_path": "run.mjs", "run_id": _RUN, HALTED_RECORD_PARAM: halted_by}, repo_root=repo
    )
    assert landed["committed"] is True, landed
    pointer = read_ledger_halt(ledger_halt_path(repo, PLAN_REL))
    assert pointer is not None and pointer.blocking == ("R1",)

    again = gate(repo, REL)
    assert again.halt is None, again.halt
    assert ALREADY_RAN not in json.dumps(again.to_json())
    resume = again.resume_ledger
    assert resume is not None and resume["run_id"] == _RUN and resume["blocking"] == ["R1"]
    assert resume["remaining"]["R1"]["remaining"] == ["the criterion is red"]

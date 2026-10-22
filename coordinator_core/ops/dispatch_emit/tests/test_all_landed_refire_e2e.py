"""The example-game-repo shape end to end, transcribed from the real run record: tshirt L, every plan row `open`, the
non-em rows landed by a checkpoint commit, the `performer: em` rows discharged in the evidence sidecar, an
earlier ledger-check halt commit whose `halted.json` now reads `refusal`, two rulable register rows, no ledger
pointer. The real gate resumes it as an all-landed ledger resume at arm m_plus; the composed script (agent
legs stubbed under node) reaches verify then commit with no plan or executor leg; a not_met judge lands a
ledger-check halt whose pointer makes the next gate call a ledger resume bound to that run and carrying its
remaining, not the already-ran refusal. Without the evidence sidecar the same plan is the row resume the
real run took."""

from __future__ import annotations

import json
import os
import shutil
import subprocess

import pytest

from coordinator_core.ops.dispatch_emit import land_run
from coordinator_core.ops.dispatch_emit.ask_compose import compose_ask_script
from coordinator_core.ops.dispatch_emit.ask_contract import (
    ALL_LANDED_HALT_KIND,
    HALTED_RECORD_PARAM,
    RECORD_DIR_ROOT,
    RESUME_LEDGER_KIND_FIELD,
    halted_record_message,
    ledger_halt_path,
    read_ledger_halt,
)
from coordinator_core.ops.dispatch_emit.ask_gate import _handler, gate
from coordinator_core.ops.dispatch_emit.reply_sidecar import read_reply
from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW
from coordinator_core.ops.dispatch_emit.tests.test_ask_gate import ACCEPTED, REL, _put, repo  # noqa: F401
from coordinator_core.ops.dispatch_emit.tests.test_ask_compose_stage_handle import (
    WORKFLOW_FAKE_JS,
    agent_gate_reply,
    readers_cfg,
)
from coordinator_core.ops.dispatch_emit.tests.test_ask_gate_resume import PLAN_REL
from coordinator_core.ops.dispatch_emit.tests.test_ledger_halt_resume_gate import ALREADY_RAN, _landed_repo
from coordinator_core.ops.dispatch_emit.tests._node_support import payload_file
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_RUN = "ask-20261018T000000-aaaaaa"
_PRIOR_RUN = "ask-20261017T231640-05b76d"
_TOKEN = "0123456789abcdef0123456789abcdef"
_BLITZ_FN = "  async function planBlitz(args) {\n    return { ready: [] };\n  }"
_RULABLE = ("T2", "R2")
_EM_ROWS = ("E1", "E2")
_LANDED = {"committed": True, "sha": "abc1234", "error": None, "refused": None}
_MET = {"items": [{"id": i, "status": "met", "wired": True} for i in _RULABLE]}
_UNMET = {"items": [{"id": "T2", "status": "not_met", "essential": True, "remaining": ["the criterion is red"]}]}
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


def _git(root, *a):
    subprocess.run(["git", *a], cwd=str(root), check=True, capture_output=True, **no_console_creationflags())


def _example_game_repo_shape(root):
    """The real record: tshirt L / route plan, two open rulable register rows, plan rows all `open` (A1/A2
    landed by one checkpoint commit, E1/E2 `performer: em` discharged in the evidence sidecar), an earlier
    ledger-check halt commit landing A1 whose `halted.json` reads `refusal` beside a stale manifest, and
    no ledger pointer."""
    _put(
        root, tshirt="L", route="plan", accepted=ACCEPTED, plan=PLAN_REL,
        requirement_register={
            "sources": [{"doc": REL, "ref": "ask"}],
            "rows": [
                {"id": i, "status": "open", "source_text": "t", "anchor": "relayed", "surface": "internal", "claimed_by": []}
                for i in _RULABLE
            ],
        },
    )
    ids = ("A1", "A2", *_EM_ROWS)
    _landed_repo(root, tuple((i, "open") for i in ids), landed=["A1", "A2"])
    body = "".join(
        f"- id: {i}\n  title: T{i}\n  disposition: open\n  writes:\n    - some/{i}.py\n"
        + ("  performer: em\n" if i in _EM_ROWS else "")
        for i in ids
    )
    (root / PLAN_REL).write_text(
        f"---\nrun_id: fixture\n---\n\n## Tasks\n\n```yaml plan-tasks\n{body}```\n", encoding="utf-8", newline="\n"
    )
    entries = "".join(f"  {i}:\n    recorded_at: '2026-10-17T00:00:00Z'\n    text: done by the EM\n" for i in _EM_ROWS)
    (root / PLAN_REL).with_suffix(".evidence.yaml").write_text(
        f"em_discharged:\n{entries}", encoding="utf-8", newline="\n"
    )
    _git(root, "add", REL)
    _git(root, "commit", "-q", "-m", "sizing")
    record = root / RECORD_DIR_ROOT / _PRIOR_RUN
    record.mkdir(parents=True)
    halted = {"halted": "refusal", "reason": "no folded stage manifest", "sizing_rel": REL}
    (record / "halted.json").write_text(json.dumps(halted), encoding="utf-8")
    manifest = {
        "rows": [{"id": "A1"}], "owed": [{"id": i} for i in _EM_ROWS], "gated": [], "rulable_ids": list(_RULABLE),
    }
    (record / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    _git(root, "add", "state")
    message = halted_record_message(_PRIOR_RUN, {"halted": "ledger-check", "plan": PLAN_REL})
    _git(root, "commit", "-q", "-m", message + "\nLands-Chunks: A1")


def _run_composed(root, verify) -> dict:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not on PATH")
    gate_reply = agent_gate_reply(root, _handler({"sizing_path": REL, "run_id": _RUN}, repo_root=root))
    script = compose_ask_script(
        repo_root="REPO", prompt=None, sizing_rel=REL, run_id=_RUN, session_id=None,
        wrap_stage=lambda _t: (_BLITZ_FN, ["Size", "Plan"]), plan_blitz_text="stub",
        script_path="scratch/warp/x.mjs", in_run_commit=True, **REVIEW_KW,
    )
    cfg = {
        "token": _TOKEN, "verify": verify, "commit": _LANDED, **readers_cfg(root),
        "replies": {"gate": gate_reply, "gate-stage": gate_reply},
    }
    proc = subprocess.run(
        [node, "-"], input=_NODE_PROGRAM, capture_output=True, text=True, encoding="utf-8", timeout=60,
        env={**os.environ, "SRC_FILE": payload_file(script), "CFG": json.dumps(cfg)}, **no_console_creationflags(),
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_the_real_gate_resumes_the_example_game_repo_shape_with_a_manifest_of_the_landed_rows(repo):  # noqa: F811
    _example_game_repo_shape(repo)
    out = _handler({"sizing_path": REL, "run_id": _RUN}, repo_root=repo)
    assert out["arm"] == "m_plus" and out["halt"] is None, out["halt"]
    assert out.get("resume_rows") is None
    assert out["resume_ledger"][RESUME_LEDGER_KIND_FIELD] == ALL_LANDED_HALT_KIND
    assert out["resume_ledger"]["blocking"] == list(_RULABLE)
    assert "manifest_handle" in out and "manifest" not in out, out
    assert [r["id"] for r in read_reply(repo, out["manifest_handle"])["rows"]] == ["A1", "A2"]


def test_without_the_evidence_sidecar_the_gate_returns_the_row_resume_the_real_run_took(repo):  # noqa: F811
    _example_game_repo_shape(repo)
    (repo / PLAN_REL).with_suffix(".evidence.yaml").unlink()
    out = _handler({"sizing_path": REL, "run_id": _RUN}, repo_root=repo)
    assert out["arm"] == "m_plus" and out.get("resume_ledger") is None
    assert out["resume_plan"] == PLAN_REL and out["resume_rows"] == list(_EM_ROWS)


def test_an_all_done_refire_reaches_verify_then_commit_with_no_executor(repo):  # noqa: F811
    _example_game_repo_shape(repo)
    out = _run_composed(repo, _MET)
    labels = [c["label"] for c in out["calls"]]
    res = out["result"]
    assert res["halted_by"] is None, res["halted_by"]
    assert "no folded stage manifest" not in json.dumps(res)
    assert labels.index("verify") < labels.index("commit")
    assert not {"plan", "prior-art", "exec", "ledger-remaining", "ledger-replan"} & set(labels), labels
    assert res["commit"]["committed"] is True


def test_a_not_met_judge_lands_a_ledger_check_halt_and_the_next_gate_resumes_it_not_the_already_ran_refusal(
    repo  # noqa: F811
):
    _example_game_repo_shape(repo)
    out = _run_composed(repo, _UNMET)
    halted_by = out["result"]["halted_by"]
    labels = [c["label"] for c in out["calls"]]
    assert halted_by["halted"] == "ledger-check", halted_by
    assert "no folded stage manifest" not in json.dumps(halted_by)
    assert labels.index("verify") < labels.index("halt-commit") and "commit" not in labels

    rec = repo / RECORD_DIR_ROOT / _RUN
    rec.mkdir(parents=True, exist_ok=True)
    (rec / "run.json").write_text(json.dumps({"sizing_rel": REL}) + "\n", encoding="utf-8")
    landed = land_run._handler(
        {"script_path": "run.mjs", "run_id": _RUN, HALTED_RECORD_PARAM: halted_by}, repo_root=repo
    )
    assert landed["committed"] is True, landed
    pointer = read_ledger_halt(ledger_halt_path(repo, PLAN_REL))
    assert pointer is not None and pointer.blocking == ("T2",)

    again = gate(repo, REL)
    assert again.halt is None, again.halt
    assert ALREADY_RAN not in json.dumps(again.to_json())
    resume = again.resume_ledger
    assert resume is not None and resume["run_id"] == _RUN and resume["blocking"] == list(_RULABLE)
    assert resume["remaining"]["T2"]["remaining"] == ["the criterion is red"]

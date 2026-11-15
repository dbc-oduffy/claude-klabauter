"""A refire of a refusal-halted run whose plan rows all landed and whose register rows are met composes no refusal.

Replays the f311e4 incident: the plan rows are `coded` (one em-discharged), both register rows already read
`met`, an earlier ledger-check halt carries a revise note naming the landed sha, and the newest halt is a
`refusal` with `next_action.resume.halted: refusal` and no phase. The refire's gate and composed script must
resume the landed plan at its ledger, not halt `stage staged no rows` with `next_action: refire` again.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess

import pytest

from coordinator_core.ops.dispatch_emit.ask_compose import compose_ask_script
from coordinator_core.ops.dispatch_emit.op import _gate_sizing_at_emit
from coordinator_core.ops.dispatch_emit.ask_contract import (
    ASK_SOURCE_PM,
    ALL_LANDED_HALT_KIND,
    RECORD_DIR_ROOT,
    RESUME_LEDGER_KIND_FIELD,
)
from coordinator_core.ops.dispatch_emit.ask_gate import _handler
from coordinator_core.ops.dispatch_emit.halt_refire import refire_next_action
from coordinator_core.ops.dispatch_emit.halted_run import write_halt_marker
from coordinator_core.ops.dispatch_emit.tests._node_support import payload_file
from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW, write_run_scripts
from coordinator_core.ops.dispatch_emit.tests.test_all_landed_refire_e2e import (
    _BLITZ_FN,
    _LANDED,
    _MET,
    _NODE_PROGRAM,
    _TOKEN,
    _git,
)
from coordinator_core.ops.dispatch_emit.tests.test_ask_compose_stage_handle import agent_gate_reply, readers_cfg
from coordinator_core.ops.dispatch_emit.tests.test_ask_gate import ACCEPTED, REL, _put, repo  # noqa: F401
from coordinator_core.ops.dispatch_emit.tests.test_ask_gate_resume import PLAN_REL
from coordinator_core.ops.dispatch_emit.tests.test_ledger_halt_resume_gate import _landed_repo
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_PROGRAM = _NODE_PROGRAM.replace("'workflow', src", "'workflow', 'log', src").replace(
    "() => {}, workflow)", "() => {}, workflow, () => {})"
)
_HALTED_RUN = "ask-20261113T135547-58220f"
_REFIRE_RUN = _HALTED_RUN
_SHA = "4f781d207da308af68f30d39b0615a5a1e9f1171"
_NOTE = f"R1 and R2 are met by commit {_SHA[:10]}. No remaining work: rule both met_by {_SHA[:10]} and close."


def _met_row(rid: str) -> dict:
    return {
        "id": rid, "status": "met", "source_text": "t", "anchor": "ask", "surface": "internal",
        "claimed_by": [PLAN_REL], "wired": True, "met_by": [f"{PLAN_REL}@{_SHA}"],
    }


def _incident_shape(root) -> None:
    """Sizing with both register rows met, plan rows C1-C3 coded and C4 em-discharged, the halted run's
    revise note, and a refusal `halted.json` shaped like the incident's (no phase: a full pass)."""
    _put(
        root, tshirt="M", route="plan", accepted=ACCEPTED, plan=PLAN_REL,
        requirement_register={"sources": [{"doc": REL, "ref": "ask"}], "rows": [_met_row("R1"), _met_row("R2")]},
    )
    ids = ("C1", "C2", "C3", "C4")
    _landed_repo(root, tuple((i, "coded" if i != "C4" else "open") for i in ids), landed=["C1", "C2", "C3"])
    body = "".join(
        f"- id: {i}\n  title: T{i}\n  disposition: {'coded' if i != 'C4' else 'open'}\n  writes:\n    - some/{i}.{'md' if i == 'C4' else 'py'}\n"
        + ("  performer: em\n" if i == "C4" else "")
        for i in ids
    )
    (root / PLAN_REL).write_text(
        f"---\nrun_id: fixture\n---\n\n## Tasks\n\n```yaml plan-tasks\n{body}```\n", encoding="utf-8", newline="\n"
    )
    (root / PLAN_REL).with_suffix(".evidence.yaml").write_text(
        "em_discharged:\n  C4:\n    recorded_at: '2026-11-13T13:56:00Z'\n    text: done by the EM\n",
        encoding="utf-8", newline="\n",
    )
    _git(root, "add", REL, PLAN_REL)
    _git(root, "commit", "-q", "-m", "sizing")
    record = root / RECORD_DIR_ROOT / _HALTED_RUN
    write_halt_marker(
        record, halted="refusal", sizing_rel=REL,
        next_action=refire_next_action(call="refire", run_id=_HALTED_RUN, sizing=REL, halted="refusal"),
    )
    (record / "run.json").write_text(json.dumps({"sizing_rel": REL}) + "\n", encoding="utf-8")
    (record / "manifest.json").write_text(
        json.dumps({"rows": [], "owed": [], "gated": [], "rulable_ids": [], "ledger_item_ids": ["R1", "R2"]}),
        encoding="utf-8",
    )
    (record / "notes.jsonl").write_text(
        json.dumps({"note": _NOTE, "at": "2026-11-13T13:59:15Z"}) + "\n", encoding="utf-8"
    )
    write_run_scripts(root, _REFIRE_RUN)


def _compose_and_run(root) -> dict:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not on PATH")
    gate_reply = agent_gate_reply(root, _handler({"sizing_path": REL, "run_id": _REFIRE_RUN}, repo_root=root))
    ask_sizing = _gate_sizing_at_emit(root, REL, [])
    script = compose_ask_script(
        staged_out={}, repo_root="REPO", prompt=None, sizing_rel=REL, run_id=_REFIRE_RUN, session_id=None,
        script_path=f"scratch/warp/{_REFIRE_RUN}.workflow.mjs", plan_blitz_args=ask_sizing.get("plan_blitz_args"),
        writes=ask_sizing.get("writes", ()), baton=ask_sizing.get("baton"),
        accept_pending=bool(ask_sizing.get("accept_pending")), in_run_commit=True, ask_source=ASK_SOURCE_PM,
        wrap_stage=lambda _t: (_BLITZ_FN, ["Size", "Plan"]), plan_blitz_text="stub", **REVIEW_KW,
    )
    cfg = {
        "token": _TOKEN, "verify": _MET, "commit": _LANDED, **readers_cfg(root),
        "replies": {"gate": gate_reply, "gate-stage": gate_reply},
    }
    proc = subprocess.run(
        [node, "-"], input=_PROGRAM, capture_output=True, text=True, encoding="utf-8", timeout=60,
        env={**os.environ, "SRC_FILE": payload_file(script), "CFG": json.dumps(cfg)}, **no_console_creationflags(),
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_the_gate_resumes_a_refusal_halted_all_landed_plan_at_its_ledger(repo):  # noqa: F811
    _incident_shape(repo)
    out = _handler({"sizing_path": REL, "run_id": _REFIRE_RUN}, repo_root=repo)
    assert out["halt"] is None, out["halt"]
    resume = out.get("resume_ledger")
    assert resume is not None and resume[RESUME_LEDGER_KIND_FIELD] == ALL_LANDED_HALT_KIND, out
    assert resume["blocking"] == [] and resume["revise_notes"] == [_NOTE]


def test_the_composed_refire_closes_without_the_no_rows_refusal(repo, m_plus_known_arm):  # noqa: F811
    _incident_shape(repo)
    out = _compose_and_run(repo)
    res = out["result"]
    labels = [c["label"] for c in out["calls"]]
    assert res["halted_by"] is None, res["halted_by"]
    assert "stage staged no rows" not in json.dumps(res)
    assert labels.index("verify") < labels.index("commit") and "exec" not in labels

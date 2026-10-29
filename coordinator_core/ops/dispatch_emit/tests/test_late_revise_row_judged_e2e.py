"""Incident replay: rows revised into a live run after its plan point are judged in that run, or land on refire.

Case 1 (verify not yet staged): the late row is read, unioned into verify's ledger ids, and judged.
Case 2 (verify staged): the revise reply says `lands_on_refire`, and a refire after the halt stages both rows."""

from __future__ import annotations

import json

import pytest

from coordinator_core.ops.dispatch_emit import ask_gate, ask_stage
from coordinator_core.ops.dispatch_emit import requirements_ledger as rl
from coordinator_core.ops.dispatch_emit import run_token
from coordinator_core.ops.dispatch_emit.ask_compose import compose_ask_script
from coordinator_core.ops.dispatch_emit.ask_contract import record_dir
from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW
from coordinator_core.ops.dispatch_emit.tests.test_ask_gate import ACCEPTED, REL, _put
from coordinator_core.ops.dispatch_emit.tests.test_ask_gate import repo as _bare_repo
from coordinator_core.ops.dispatch_emit.tests.test_plain_resume_refire_stages_current_rows import (
    PLAN_REL,
    _plan,
)

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

RUN = "ask-20261029T000000-aaa111"
_BLITZ_FN = "  async function planBlitz(args) {\n    return { ready: [] };\n  }"


@pytest.fixture
def repo(_bare_repo, monkeypatch):
    (_bare_repo / ".git").mkdir()
    monkeypatch.setattr(ask_gate, "_bind_run", lambda *a, **k: None)
    monkeypatch.setattr(run_token, "is_live", lambda *_a, **_k: True)
    _put(_bare_repo, tshirt="M", route="plan", accepted=ACCEPTED, plan=PLAN_REL)
    _plan(_bare_repo, {"C1": "open"})
    rl.start(_bare_repo, RUN)
    rl.bind(_bare_repo, RUN, REL, ask_text="the first ask")
    return _bare_repo


def _stage(root):
    params = {"run_id": RUN, "plan_path": PLAN_REL, "writes": ["pkg/C1.py"], "commit_sizing_path": REL}
    reply = ask_stage._handler(params, root)
    assert reply.get("manifest_handle"), reply
    return json.loads((record_dir(root, RUN) / "manifest.json").read_text(encoding="utf-8"))


def _revise(root, text):
    out = rl._handler({"run_id": RUN, "verbatim": text}, repo_root=root)
    assert out["ok"] is True and out["run_live"] is True, out
    return out


def _ledger_ids(root):
    return [i.id for i in rl.read_ledger(root, record_dir(root, RUN)).items]


def _verdict(ids):
    return {"items": [{"id": i, "status": "met", "wired": True, "met_by": "pkg/C1.py"} for i in ids]}


def _compose():
    return compose_ask_script(
        repo_root="REPO", prompt="add a thing", sizing_rel=None, run_id="run-1", session_id=None,
        wrap_stage=lambda text: (_BLITZ_FN, ["Size", "Plan"]), plan_blitz_text="stub",
        in_run_commit=True, script_path="scratch/warp/a.workflow.mjs", **REVIEW_KW,
    )


def test_a_row_revised_before_verify_is_late_unioned_into_verify_and_judged(repo):
    manifest = _stage(repo)
    staged = list(manifest["ledger_item_ids"])
    assert staged and not rl.verify_staged(record_dir(repo, RUN))

    out = _revise(repo, "also cover Windows")
    added = out["item"]
    assert "lands_on_refire" not in out and "pre-verify late-row leg" in out["note"]
    assert rl.late_rows(repo, RUN) == [added]

    script = _compose()
    line = next(ln for ln in script.splitlines() if "const _ledgerIds =" in ln)
    assert "_verifyLateIds" in line
    verify_ids = [*staged, *rl.late_rows(repo, RUN)]
    assert added in verify_ids and sorted(verify_ids) == sorted(_ledger_ids(repo))

    ledger = rl.read_ledger(repo, record_dir(repo, RUN))
    judged = rl.judge(ledger, _verdict(verify_ids))
    assert not any("unjudged" in f["reason"] for f in judged["failures"]), judged
    skipped = rl.judge(ledger, _verdict(staged))
    assert [f["id"] for f in skipped["failures"] if "unjudged" in f["reason"]] == [added]


def test_a_row_revised_after_verify_lands_on_refire_and_the_refire_stages_both_rows(repo):
    manifest = _stage(repo)
    r2 = _revise(repo, "also cover Windows")["item"]
    prompts = record_dir(repo, RUN) / "prompts"
    prompts.mkdir(parents=True, exist_ok=True)
    (prompts / "verify.md").write_text("verify", encoding="utf-8")

    out = _revise(repo, "and cover macOS")
    r3 = out["item"]
    assert out["lands_on_refire"] is True and out["next"]["kind"] == "refire"
    assert "next stage claims" not in json.dumps(out)
    assert r2 not in manifest["ledger_item_ids"] and r3 not in manifest["ledger_item_ids"]

    refire = ask_gate._handler({"sizing_path": REL, "run_id": "refire1", "writes": ["pkg/C1.py"]}, repo)
    assert refire["halt"] is None, refire
    params = {"run_id": "refire1", "plan_path": PLAN_REL, "writes": ["pkg/C1.py"], "commit_sizing_path": REL}
    reply = ask_stage._handler(params, repo)
    assert reply.get("manifest_handle"), reply
    restaged = json.loads((record_dir(repo, "refire1") / "manifest.json").read_text(encoding="utf-8"))
    assert {r2, r3} <= set(restaged["ledger_item_ids"]), restaged["ledger_item_ids"]

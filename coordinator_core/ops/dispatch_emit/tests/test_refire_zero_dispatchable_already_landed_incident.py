"""The two 2026-10-22 example-cockpit-repo L re-fires (57c72c all-landed, 219c06 stale ledger) rebuilt under tmp_path
from quoted facts: which gate verdict each takes at M+, what the gate's in-gate stage writes, and which composed
stage leg reaches the zero-dispatchable refusal. Fixtures carry literals; nothing reads cockpit."""

from __future__ import annotations

import json

import pytest

from coordinator_core.ops.dispatch_emit import ask_compose, ask_gate
from coordinator_core.ops.dispatch_emit.ask_compose import compose_ask_script
from coordinator_core.ops.dispatch_emit.ask_contract import (
    ALL_LANDED_HALT_KIND,
    RECORD_DIR_ROOT,
    RESUME_LEDGER_KIND_FIELD,
    halted_record_message,
)
from coordinator_core.ops.dispatch_emit.ask_stage import AskStageError, stage
from coordinator_core.ops.dispatch_emit.halted_run import write_halt_marker
from coordinator_core.ops.dispatch_emit.reply_sidecar import read_reply
from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW
from coordinator_core.ops.dispatch_emit.tests.test_ask_gate import ACCEPTED, REL, _put, repo  # noqa: F401
from coordinator_core.ops.dispatch_emit.tests.test_ask_gate_resume import PLAN_REL
from coordinator_core.ops.dispatch_emit.tests.test_refire_after_ledger_halt_resumes_incident import _git

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

CREDITED_FIELD = "credited_landed"
REFIRE_RUN = "ask-20261022T210944-57c72c"
HALTED_RUN = "warp-2026-10-22-new-project-describe-scaffold-launch-20261022T150000-219c06"
ZERO_ROWS = "the spine derives zero dispatchable rows"
C7_REMAINING = "Rail New project item opening the describe form (plan C7)"
_BLITZ_FN = "  async function planBlitz(args) {\n    return { ready: [] };\n  }"


def _register(ids):
    return {
        "sources": [{"doc": REL, "ref": "ask"}],
        "rows": [
            {"id": i, "status": "open", "source_text": f"text {i}", "anchor": "relayed", "surface": "internal", "claimed_by": []}
            for i in ids
        ],
    }


def _write_plan(repo, rows):
    """`rows` is `[(id, disposition, register_rows, performer)]`; written as a plan-tasks fence."""
    body = "".join(
        f"- id: {i}\n  title: T{i}\n  disposition: {d}\n  writes:\n    - some/{i}.py\n"
        + (f"  register_rows: [{', '.join(rr)}]\n" if rr else "")
        + (f"  performer: {p}\n" if p else "")
        for i, d, rr, p in rows
    )
    path = repo / PLAN_REL
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\nrun_id: fixture\n---\n\n## Tasks\n\n```yaml plan-tasks\n{body}```\n", encoding="utf-8", newline="\n"
    )


def _init(repo):
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t.example")
    _git(repo, "config", "user.name", "t")


def _checkpoint(repo, ids, tag):
    (repo / f"{tag}.txt").write_text(f"{tag}\n", encoding="utf-8")
    _git(repo, "add", f"{tag}.txt")
    _git(
        repo, "commit", "-q", "-m", f"checkpoint(wave 1): {len(ids)} rows — {', '.join(ids)}",
        "-m", f"Checkpoint-Plan: {PLAN_REL}\nCheckpoint-Base: abc1234",
    )


def _discharge(repo, ids):
    entries = "".join(f"  {i}:\n    recorded_at: '2026-10-22T00:00:00Z'\n    text: live-verified by the EM\n" for i in ids)
    (repo / PLAN_REL).with_suffix(".evidence.yaml").write_text(
        f"em_discharged:\n{entries}" if entries else "rows: {}\n", encoding="utf-8", newline="\n"
    )


def _all_landed_repo(repo, *, discharged):
    """57c72c: C11 and C15 landed by checkpoint, C20 `performer: em` live-verified; L sizing, plan edge."""
    _put(repo, tshirt="L", route="plan", accepted=ACCEPTED, plan=PLAN_REL, requirement_register=_register(["R1", "R2"]))
    _init(repo)
    _write_plan(repo, [("C11", "open", ["R1"], None), ("C15", "open", ["R1"], None), ("C20", "open", ["R2"], "em")])
    _checkpoint(repo, ["C11", "C15"], "wave1")
    _discharge(repo, ["C20"] if discharged else [])


def _stale_ledger_repo(repo):
    """219c06: ledger-check halt blocking T18, R2, R3 with R2 remaining "plan C7"; C7 lands afterwards; R3 is owed."""
    _put(repo, tshirt="L", route="plan", accepted=ACCEPTED, plan=PLAN_REL, requirement_register=_register(["T18", "R2", "R3"]))
    _init(repo)
    _write_plan(
        repo,
        [("C5", "open", ["T18"], None), ("C7", "open", ["R2"], None), ("C9", "open", ["R3"], None)],
    )
    _checkpoint(repo, ["C5"], "wave1")
    record = repo / RECORD_DIR_ROOT / HALTED_RUN
    write_halt_marker(
        record, halted="ledger-check", sizing_rel=REL, plan_rel=PLAN_REL, blocking=("T18", "R2", "R3"),
        blocking_items=[
            {"id": "T18", "remaining": ["T18 evidence stale"], "evidence": "ledger verdict"},
            {"id": "R2", "remaining": [C7_REMAINING], "evidence": "ledger verdict"},
            {"id": "R3", "remaining": ["Mount dialog from the menu (lazy), then senior-front-end review file"], "evidence": "ledger verdict"},
        ],
    )
    (record / "manifest.json").write_text(json.dumps({"rows": [{"id": "C5"}, {"id": "C7"}, {"id": "C9"}]}), encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", halted_record_message(HALTED_RUN, {"halted": "ledger-check", "plan": PLAN_REL}))
    _checkpoint(repo, ["C7"], "wave2")


def _compose(monkeypatch, arm="m_plus"):
    monkeypatch.setattr(ask_compose, "_known_arm", lambda *_: arm)
    return compose_ask_script(
        repo_root="REPO", prompt=None, sizing_rel=REL, run_id="run-1", session_id=None,
        wrap_stage=lambda _t: (_BLITZ_FN, ["Size", "Plan"]), plan_blitz_text="stub",
        script_path="x.mjs", in_run_commit=True, **REVIEW_KW,
    )


def _staged_ids(repo, out):
    return [r["id"] for r in read_reply(repo, out["manifest_handle"])["rows"]]


def test_a_all_landed_with_the_em_row_discharged_closes_with_a_zero_row_manifest(repo):  # noqa: F811
    _all_landed_repo(repo, discharged=True)
    out = ask_gate._handler({"sizing_path": REL, "run_id": REFIRE_RUN}, repo)
    assert out.get("halt") is None and out["arm"] == "m_plus", out
    assert out["resume_ledger"][RESUME_LEDGER_KIND_FIELD] == ALL_LANDED_HALT_KIND
    assert "manifest_handle" in out and _staged_ids(repo, out) == [], out


def test_a2_undischarged_em_row_keeps_the_row_resume(repo, monkeypatch):  # noqa: F811
    """Leg (4): resume_plan/resume_rows with no resume_ledger, staged later by the composed `_ONLY_ROWS_JS` call."""
    _all_landed_repo(repo, discharged=False)
    out = ask_gate._handler({"sizing_path": REL, "run_id": REFIRE_RUN}, repo)
    assert out.get("halt") is None and out["arm"] == "m_plus", out
    assert not out.get("resume_ledger") and out["resume_plan"] == PLAN_REL and out["resume_rows"] == ["C20"], out
    assert "manifest_handle" not in out and "manifest" not in out, out
    script = _compose(monkeypatch)
    assert "only_rows: _gate.resume_rows" in script


def test_a2_em_row_beside_an_unlanded_executor_row_stays_open_for_the_row_resume(repo, monkeypatch):  # noqa: F811
    """An em-performed row is dropped only when nothing executor-dispatchable is left: with C15 unlanded the
    gate still takes the non-ledger row resume, staged later by the composed `_ONLY_ROWS_JS` call."""
    _put(repo, tshirt="L", route="plan", accepted=ACCEPTED, plan=PLAN_REL, requirement_register=_register(["R1", "R2"]))
    _init(repo)
    _write_plan(repo, [("C11", "open", ["R1"], None), ("C15", "open", ["R1"], None), ("C20", "open", ["R2"], "em")])
    _checkpoint(repo, ["C11"], "wave1")
    out = ask_gate._handler({"sizing_path": REL, "run_id": REFIRE_RUN}, repo)
    assert out.get("halt") is None and out["arm"] == "m_plus", out
    assert not out.get("resume_ledger") and out["resume_plan"] == PLAN_REL and out["resume_rows"] == ["C15", "C20"], out
    script = _compose(monkeypatch)
    assert "only_rows: _gate.resume_rows" in script


def test_b_stale_ledger_credits_the_row_landed_after_the_halt_and_dispatches_only_r3(repo):  # noqa: F811
    _stale_ledger_repo(repo)
    out = ask_gate._handler({"sizing_path": REL, "run_id": REFIRE_RUN}, repo)
    assert out.get("halt") is None and out["arm"] == "m_plus" and out.get("resume_ledger"), out
    resume = out["resume_ledger"]
    assert "R2" in resume[CREDITED_FIELD]
    assert "R2" not in resume["blocking"] and "R2" not in resume["remaining"]
    assert "R3" in resume["blocking"] and "R3" in resume["remaining"]


def test_b_credited_row_is_not_owed_and_the_replan_stage_call_carries_ledger_resume(repo, monkeypatch):  # noqa: F811
    """Gate verdict credits R2 so only R3 stays blocking; the ledger-replan stage call passes ledger_resume."""
    _stale_ledger_repo(repo)
    out = ask_gate._handler({"sizing_path": REL, "run_id": REFIRE_RUN}, repo)
    resume = out["resume_ledger"]
    assert "R2" in resume[CREDITED_FIELD] and "R2" not in resume["blocking"], resume
    script = _compose(monkeypatch)
    replan = script[script.index("label: 'ledger-replan'") - 6000: script.index("label: 'ledger-replan'")]
    assert "ledger_resume" in replan


def test_c_unlanded_zero_dispatchable_spine_still_refuses(repo):  # noqa: F811
    _put(repo, tshirt="L", route="plan", accepted=ACCEPTED, plan=PLAN_REL, requirement_register=_register(["R1"]))
    _init(repo)
    _write_plan(repo, [("E1", "open", ["R1"], "em")])
    out = ask_gate._handler({"sizing_path": REL, "run_id": REFIRE_RUN}, repo)
    assert out.get("halt") is None and not out.get("resume_ledger"), out
    assert out["resume_plan"] == PLAN_REL and out["resume_rows"] == ["E1"]
    with pytest.raises(AskStageError, match=ZERO_ROWS):
        stage(repo, run_id=REFIRE_RUN, plan_rel=PLAN_REL, writes=[], cross_repo_approved=False, session_id=None)

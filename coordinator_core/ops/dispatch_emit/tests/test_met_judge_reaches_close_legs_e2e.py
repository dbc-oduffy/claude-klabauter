"""A met judge whose recorded falsifier could not run reaches every close leg of the terminal commit.

The judge result goes through the engine's verdict rules, lands in `inline_review.criterion` the way
the run record carries it, and drives `dispatch.land_run` over the full-path fixture and then its close job: the
review stamp mints, the plan is stamped implemented, the completion entry is written and the baton
ships, with no re-judge record. The legs are read from the close-job record, not the landing reply. The same run with a falsifier `fail` closes none of them.
"""

from __future__ import annotations

import copy
import json
import shutil
import subprocess

import pytest

from coordinator_core.benchmarks import terminal_commit_full_path as driver
from coordinator_core.ops.ceremony import close_background
from coordinator_core.ops.dispatch_emit import land_run
from coordinator_core.ops.dispatch_emit.emit import _JUDGE_VERDICT_RULES_JS
from coordinator_core.ops.dispatch_emit.run_close import close_job_path
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def _judged(falsifier_verdict: str) -> dict:
    """The d50b82 judge shape, run through the engine's post-hoc verdict rules."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not on PATH")
    verdict = {
        "status": "met",
        "differs_from_baseline": True,
        "observed": [
            {"kind": "command", "ref": "py -m pytest", "result": "3 passed"},
            {"kind": "file", "ref": "a.py", "result": "exists"},
        ],
        "clauses": [{"clause": "a.py exists", "status": "met", "observed_ref": "a.py"}],
        "falsifier": {
            "present": True, "ran": True, "verdict": falsifier_verdict,
            "output_anchor": "AttributeError: ROADMAP_NEXT_ACTION_JS",
        },
    }
    script = f"const log = () => {{}}; console.log(JSON.stringify({_JUDGE_VERDICT_RULES_JS}({json.dumps(verdict)})));"
    out = subprocess.run(
        [node, "-e", script], capture_output=True, text=True, check=True, **no_console_creationflags()
    )
    return json.loads(out.stdout)


def _review(judged: dict) -> dict:
    review = copy.deepcopy(driver.INLINE_REVIEW)
    review["criterion"] = {
        **review["criterion"],
        "status": judged["status"],
        "reason": judged.get("reason"),
        "register_rows": [],
    }
    return review


def _run(tmp_path, monkeypatch, judged: dict):
    from coordinator_core.session import claims

    repo, script = driver.build_fixture(tmp_path / "work")
    outbox = tmp_path / "lessons-outbox"
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
         "inline_review": _review(judged)},
        repo_root=repo / ".git",
    )
    assert out.get("committed") is True, out
    return repo, out, _run_close_job(repo, out)


def _run_close_job(repo, out) -> dict:
    """Run the landing's close job in-process and return its recorded leg statuses."""
    run_id = out["close"]["run_id"]
    record = json.loads((repo / close_job_path(run_id)).read_text(encoding="utf-8"))
    close_background._handler(
        {"run_id": run_id, "claimed_batons": record["claimed_batons"], "source_memo": record.get("source_memo")},
        repo_root=repo / ".git",
    )
    settled = json.loads((repo / close_job_path(run_id)).read_text(encoding="utf-8"))
    return {leg: (r["status"], r["detail"]) for leg, r in settled["legs"].items()}


def _rejudge_files(repo) -> list:
    return [p for p in repo.rglob("*") if p.is_file() and ".git" not in p.parts and "rejudge" in p.name]


@pytest.fixture(autouse=True)
def _work_dir(tmp_path):
    (tmp_path / "work").mkdir()


def test_a_met_over_an_unrun_falsifier_closes_every_leg_in_run(tmp_path, monkeypatch):
    judged = _judged("error")
    assert judged["status"] == "met" and judged["reason"].startswith("met; recorded falsifier did not run")

    repo, out, legs = _run(tmp_path, monkeypatch, judged)

    assert out["criterion_status"] == "met", out
    assert legs["review-stamp"][0] == "ran", legs
    assert legs["plan-reconciliation"][0] != "failed", legs
    assert legs["completion-entry"][0] == "ran", legs
    assert legs["baton-ship"][0] == "ran", legs
    assert legs["close-commit"][0] == "ran", legs
    text = (repo / driver.PLAN_REL).read_text(encoding="utf-8")
    assert "status: implemented" in text and "review_stamp:" in text
    assert "status: met" in text.split("review_stamp:", 1)[1]
    assert legs["baton-ship"][1].startswith("shipped at "), legs
    assert _rejudge_files(repo) == []


def test_the_same_run_with_a_falsifier_fail_closes_none_of_them(tmp_path, monkeypatch):
    judged = _judged("fail")
    assert judged["status"] == "indeterminate"

    repo, out, _legs = _run(tmp_path, monkeypatch, judged)

    assert out.get("criterion_status") != "met", out
    stamp = (repo / driver.PLAN_REL).read_text(encoding="utf-8").partition("review_stamp:")[2]
    assert "status: met" not in stamp
    assert "status: implemented" not in (repo / driver.PLAN_REL).read_text(encoding="utf-8")
    assert _rejudge_files(repo) == []

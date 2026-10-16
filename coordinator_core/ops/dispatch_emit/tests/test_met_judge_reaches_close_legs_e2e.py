"""A met judge whose recorded falsifier could not run reaches every close leg of the terminal commit.

The judge result goes through the engine's verdict rules, lands in `inline_review.criterion` the way
the run record carries it, and drives `dispatch.terminal_commit` over the full-path fixture: the
review stamp mints, the plan is stamped implemented, the completion entry is written and the baton
ships, with no re-judge record. The same run with a falsifier `fail` closes none of them.
"""

from __future__ import annotations

import copy
import json
import shutil
import subprocess

import pytest

from coordinator_core.benchmarks import terminal_commit_full_path as driver
from coordinator_core.ops.ceremony import close_background
from coordinator_core.ops.dispatch_emit import terminal_commit
from coordinator_core.ops.dispatch_emit.emit import _JUDGE_VERDICT_RULES_JS
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
    noop = tmp_path / "noop.py"
    noop.write_text("pass\n", encoding="utf-8")
    monkeypatch.setenv("LESSON_PROMOTE_OUTBOX_ROOT", str(outbox))
    monkeypatch.setenv("COORDINATOR_SESSION_ID", driver.SESSION_ID)
    monkeypatch.setattr(terminal_commit, "CLOSE_JOB_SCRIPT", str(noop))
    monkeypatch.setattr(
        claims, "list_claims_by_session_checked", lambda sid, cwd=None: ([("handoff", "b.md")], [])
    )
    monkeypatch.setattr(close_background, "_launch_close_review", lambda ctx: True)
    out = terminal_commit._handler(
        {"script_path": script, "incomplete_chunks": [], "session_id": driver.SESSION_ID,
         "inline_review": _review(judged)},
        repo_root=repo / ".git",
    )
    return repo, out


def _rejudge_files(repo) -> list:
    return [p for p in repo.rglob("*") if p.is_file() and ".git" not in p.parts and "rejudge" in p.name]


@pytest.fixture(autouse=True)
def _work_dir(tmp_path):
    (tmp_path / "work").mkdir()


def test_a_met_over_an_unrun_falsifier_closes_every_leg_in_run(tmp_path, monkeypatch):
    judged = _judged("error")
    assert judged["status"] == "met" and judged["reason"].startswith("met; recorded falsifier did not run")

    repo, out = _run(tmp_path, monkeypatch, judged)

    assert out["review_stamp"] == "minted", out
    assert out["criterion_status"] == "met", out
    assert out["plan_status"] == "implemented", out
    text = (repo / driver.PLAN_REL).read_text(encoding="utf-8")
    assert "status: implemented" in text and "review_stamp:" in text
    assert "status: met" in text.split("review_stamp:", 1)[1]
    legs = (out.get("close") or {}).get("legs") or {}
    assert (legs.get("plan-reconciliation") or {}).get("status") != "failed", legs
    assert (legs.get("completion-entry") or {}).get("status") == "ran", legs
    assert (legs.get("baton-ship") or {}).get("status") == "ran", legs
    assert "shipped" in (repo / driver.BATON_REL).read_text(encoding="utf-8")
    assert _rejudge_files(repo) == []


def test_the_same_run_with_a_falsifier_fail_closes_none_of_them(tmp_path, monkeypatch):
    judged = _judged("fail")
    assert judged["status"] == "indeterminate"

    repo, out = _run(tmp_path, monkeypatch, judged)

    assert out.get("criterion_status") != "met", out
    assert out.get("plan_status") != "implemented", out
    stamp = (repo / driver.PLAN_REL).read_text(encoding="utf-8").partition("review_stamp:")[2]
    assert "status: met" not in stamp
    assert "status: implemented" not in (repo / driver.PLAN_REL).read_text(encoding="utf-8")
    assert _rejudge_files(repo) == []

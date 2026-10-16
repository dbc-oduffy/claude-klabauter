"""dispatch.terminal_commit returns the plan-run close block and pays the inline legs.

Fixture: the budget driver's one-row plan with a met criterion, one claimed baton and a
lessons outbox redirected into tmp. The close job is launched at a no-op script.
"""

from __future__ import annotations

import copy
import json
import subprocess
import sys
from pathlib import Path

import pytest

from coordinator_core.benchmarks import terminal_commit_full_path as driver
from coordinator_core.ops.dispatch_emit import run_close, terminal_commit
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

CONTRACT = set(run_close.STATUSES)


@pytest.fixture
def env(tmp_path, monkeypatch):
    from coordinator_core.session import claims

    parent = tmp_path / "work"
    parent.mkdir()
    repo, script = driver.build_fixture(parent)
    outbox = parent / "lessons-outbox"
    outbox.mkdir()
    noop = parent / "noop.py"
    noop.write_text("pass\n", encoding="utf-8")
    monkeypatch.setenv("LESSON_PROMOTE_OUTBOX_ROOT", str(outbox))
    monkeypatch.setenv("COORDINATOR_SESSION_ID", driver.SESSION_ID)
    monkeypatch.setattr(terminal_commit, "CLOSE_JOB_SCRIPT", str(noop))
    monkeypatch.setattr(
        claims, "list_claims_by_session_checked", lambda sid, cwd=None: ([("handoff", "b.md")], [])
    )
    return {"repo": repo, "script": script, "outbox": outbox}


def _fire(env, review=None, script=None, **extra) -> dict:
    return terminal_commit._handler(
        {
            "script_path": script or env["script"],
            "incomplete_chunks": [],
            "session_id": driver.SESSION_ID,
            "inline_review": review or copy.deepcopy(driver.INLINE_REVIEW),
            **extra,
        },
        repo_root=env["repo"] / ".git",
    )


def _entries(repo: Path) -> list:
    root = repo / "archive" / "completed"
    return sorted(root.rglob("*.md")) if root.exists() else []


def _judge(**criterion) -> dict:
    review = copy.deepcopy(driver.INLINE_REVIEW)
    for key in criterion.pop("drop", ()):
        review["criterion"].pop(key, None)
    review["criterion"].update(criterion)
    return review


def test_plan_run_reply_carries_every_leg(env):
    out = _fire(env)

    close = out["close"]
    assert close["applies"] is True and close["run_id"] == "run" and close["kind"] == "plan-run"
    assert set(close["legs"]) == set(run_close.RUN_BOUND_LEGS) | set(run_close.SESSION_BOUND_LEGS)
    assert len(close["legs"]) == 11
    assert {leg["status"] for leg in close["legs"].values()} <= CONTRACT
    assert close["legs"]["review"]["status"] == "queued"
    for leg in ("plan-reconciliation", "lessons", "completion-entry", "baton-ship"):
        assert close["legs"][leg]["status"] == "ran", (leg, close["legs"][leg])
    assert close["outstanding"] == []
    assert close["completion_entry"] and close["completion_entry"].startswith("archive/completed/")
    assert (env["repo"] / close["completion_entry"]).is_file()
    assert len(list(env["outbox"].glob("*.yaml"))) == 1
    assert "shipped" in (env["repo"] / driver.BATON_REL).read_text(encoding="utf-8")


def test_queued_legs_name_a_job_record_that_exists(env):
    close = _fire(env)["close"]

    queued = [leg for leg, r in close["legs"].items() if r["status"] == "queued"]
    assert sorted(queued) == ["baton-drain", "orientation-refresh", "post-close", "review"]
    record = env["repo"] / run_close.close_job_path("run")
    assert json.loads(record.read_text(encoding="utf-8"))["legs"].keys() == set(queued)
    assert all(run_close.close_job_path("run") in close["legs"][leg]["detail"] for leg in queued)
    assert close["legs"]["memo-lifecycle"]["status"] == "not-applicable"


def test_record_carries_attribution_and_settled_inline_legs(env):
    out = _fire(env)

    record = json.loads((env["repo"] / run_close.close_job_path("run")).read_text(encoding="utf-8"))
    assert record["session_id"] == driver.SESSION_ID
    assert record["plan_rel"] == driver.PLAN_REL
    assert "source_memo" in record
    assert record["claimed_batons"] == [driver.BATON_REL]
    assert record["review_receipt"] == driver.PLAN_REL
    assert record["run_base_sha"] == "a" * 40
    assert record["terminal_sha"] == out["sha"]
    assert record["completion_entry"] == out["close"]["completion_entry"]
    assert set(record["settled"]) == {"completion-entry", "baton-ship"}
    assert {v["status"] for v in record["settled"].values()} == {"ran"}


def test_inline_legs_ride_the_coded_stamp_commit(env):
    out = _fire(env)

    entry = out["close"]["completion_entry"]
    shown = subprocess.run(
        ["git", "show", "--name-only", "--format=", out["coded_sha"]],
        cwd=env["repo"], capture_output=True, text=True, check=True, **no_console_creationflags(),
    ).stdout.split()
    process_half = subprocess.run(
        ["git", "show", "--name-only", "--format=", "HEAD"],
        cwd=env["repo"], capture_output=True, text=True, check=True, **no_console_creationflags(),
    ).stdout.split()
    assert {entry, driver.BATON_REL} <= set(shown + process_half)
    dirty = subprocess.run(
        ["git", "status", "--porcelain", "--", entry, driver.BATON_REL],
        cwd=env["repo"], capture_output=True, text=True, check=True, **no_console_creationflags(),
    ).stdout
    assert dirty == ""


def test_judge_without_summary_fails_completion_entry_and_writes_none(env):
    out = _fire(env, _judge(drop=("completion_summary", "observation")))

    close = out["close"]
    leg = close["legs"]["completion-entry"]
    assert leg["status"] == "failed"
    assert leg["detail"].startswith("no judge summary")
    assert "coordinator-complete-entry.py" in leg["detail"]
    assert "completion-entry" in close["outstanding"]
    assert _entries(env["repo"]) == [] and close["completion_entry"] is None


def test_not_met_run_fails_completion_entry_and_writes_none(env):
    out = _fire(env, _judge(status="not_met", reason="r"))

    close = out["close"]
    assert close["legs"]["completion-entry"]["status"] == "failed"
    assert "completion-entry" in close["outstanding"]
    assert _entries(env["repo"]) == []
    assert close["legs"]["baton-ship"]["status"] == "failed"
    assert "shipped" not in (env["repo"] / driver.BATON_REL).read_text(encoding="utf-8")


def test_commit_that_does_not_land_leaves_no_inline_file(env, monkeypatch):
    baton = env["repo"] / driver.BATON_REL
    before = baton.read_text(encoding="utf-8")
    real = terminal_commit.reentrant_dispatch
    calls: list = []

    def flaky(op, params, repo_root=None):
        calls.append(op)
        if len(calls) == 2:
            return {"committed": False, "error": "boom"}
        return real(op, params, repo_root=repo_root)

    monkeypatch.setattr(terminal_commit, "reentrant_dispatch", flaky)

    out = _fire(env)

    close = out["close"]
    assert "coded_stamp_error" in out
    assert _entries(env["repo"]) == [] and baton.read_text(encoding="utf-8") == before
    assert list(env["outbox"].glob("*.yaml")) == []
    assert not (env["repo"] / run_close.close_job_path("run")).exists()
    for leg in ("completion-entry", "baton-ship", "lessons", "baton-drain", "orientation-refresh", "post-close", "review"):
        assert close["legs"][leg] == {"status": "failed", "detail": "coded-stamp commit did not land"}, leg
    assert set(close["outstanding"]) >= {"completion-entry", "baton-ship", "lessons"}


def test_job_that_does_not_start_reports_its_legs_failed(env, monkeypatch):
    monkeypatch.setattr(terminal_commit, "_launch_close_job", lambda *a, **k: False)

    close = _fire(env)["close"]

    for leg in ("baton-drain", "orientation-refresh", "post-close", "review"):
        assert close["legs"][leg] == {"status": "failed", "detail": "close job did not start"}
    assert "post-close" in close["outstanding"]


def test_anchor_only_run_does_not_apply(env):
    (env["repo"] / "anchor.mjs").write_text("// emitted script, no marker\n", encoding="utf-8")
    review = copy.deepcopy(driver.INLINE_REVIEW)
    review["delivery"]["product_files"] = 0

    out = terminal_commit._handler(
        {
            "script_path": "anchor.mjs",
            "incomplete_chunks": [],
            "plan_path": driver.PLAN_REL,
            "inline_review": review,
        },
        repo_root=env["repo"] / ".git",
    )

    close = out["close"]
    assert close["applies"] is False and close["outstanding"] == []
    assert {close["legs"][leg]["status"] for leg in run_close.RUN_BOUND_LEGS} == {"not-applicable"}


def test_refusal_before_a_request_carries_no_close(env):
    out = terminal_commit._handler(
        {"script_path": "nope.mjs", "incomplete_chunks": []}, repo_root=env["repo"] / ".git"
    )

    assert "close" not in out


def test_background_legs_match_the_job(env):
    from coordinator_core.ops.ceremony import close_background

    assert terminal_commit._BACKGROUND_LEGS == ("review", *close_background.BACKGROUND_LEGS)


def _record(env) -> dict:
    return json.loads((env["repo"] / run_close.close_job_path("run")).read_text(encoding="utf-8"))


def test_record_persists_the_judged_criterion(env):
    _fire(env)

    record = _record(env)
    assert record["criterion"]["status"] == driver.INLINE_REVIEW["criterion"]["status"]
    assert record["falsifier_broken"] is None


def test_record_persists_a_demoted_verdict(env):
    _fire(env, _judge(status="not_met", reason="r"))

    assert _record(env)["criterion"]["status"] == "not_met"


def test_record_without_a_digest_criterion_writes_null(env):
    review = copy.deepcopy(driver.INLINE_REVIEW)
    review.pop("criterion")

    _fire(env, review)

    assert _record(env)["criterion"] is None


def test_record_persists_the_falsifier_broken_tells(env):
    _fire(env, plan_path=driver.PLAN_REL,
          falsifier_broken=[{"plan": driver.PLAN_REL, "tells": ["t1", "t2"]}])

    assert _record(env)["falsifier_broken"] == ["t1", "t2"]


def test_no_leg_detail_names_a_machine_path(env):
    close = _fire(env)["close"]

    text = json.dumps(close)
    assert str(env["repo"]) not in text and "\\\\" not in text
    assert sys.executable not in text

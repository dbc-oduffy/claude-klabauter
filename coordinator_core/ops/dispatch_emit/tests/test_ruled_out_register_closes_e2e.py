"""An ask run whose every register row a recorded revise ruling took out of scope commits through
`dispatch.land_run` and its close job pays every close leg, whichever run recorded the ruling."""

from __future__ import annotations

import copy

import pytest
import yaml

from coordinator_core.benchmarks import terminal_commit_full_path as driver
from coordinator_core.ops import requirement_register as rr
from coordinator_core.ops.ceremony import close_background
from coordinator_core.ops.dispatch_emit import land_run
from coordinator_core.ops.dispatch_emit.tests.test_met_judge_reaches_close_legs_e2e import _run_close_job

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

OTHER_RUN = "ask-20990101T000000-aaaaaa"
OPEN_TRAILER = "Requirement-Open: R1 withheld by out-of-scope ruling"


def _fixture(tmp_path, monkeypatch, ruling_run):
    from coordinator_core.session import claims

    parent = tmp_path / "work"
    parent.mkdir()
    repo, script = driver.build_ask_fixture(parent)
    sizing = repo / driver.ASK_SIZING_REL
    data = yaml.safe_load(sizing.read_text(encoding="utf-8"))
    row = data[rr.REGISTER_KEY][rr.ROWS_KEY][0]
    assert row[rr.ROW_ID] == "R1", row
    row[rr.ROW_STATUS] = rr.STATUS_DEFERRED
    row[rr.ROW_RULING] = {
        rr.RULING_SOURCE: "pm",
        rr.RULING_QUOTE: "R1 is out of scope",
        rr.RULING_REF: f"workflow.revise:{ruling_run}:out_of_scope",
    }
    sizing.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    plan = repo / driver.ASK_PLAN_REL
    text = plan.read_text(encoding="utf-8")
    claim = f'register_claims:\n  sizing: "{driver.ASK_SIZING_REL}"\n  rows: ["R1"]\nprime_exit_criterion:'
    plan.write_text(text.replace("prime_exit_criterion:", claim, 1), encoding="utf-8")

    outbox = tmp_path / "lessons-outbox"
    outbox.mkdir()
    monkeypatch.setenv("LESSON_PROMOTE_OUTBOX_ROOT", str(outbox))
    monkeypatch.setenv("COORDINATOR_SESSION_ID", driver.SESSION_ID)
    monkeypatch.setattr(land_run, "_launch_close_job", lambda *a, **k: True)
    monkeypatch.setattr(
        claims, "list_claims_by_session_checked", lambda sid, cwd=None: ([("handoff", "b.md")], [])
    )
    monkeypatch.setattr(close_background, "_launch_close_review", lambda ctx: True)
    return repo, script


def _drive(repo, script, verdict):
    return land_run._handler(
        {"script_path": script, "incomplete_chunks": [], "session_id": driver.SESSION_ID,
         "inline_review": copy.deepcopy(driver.INLINE_REVIEW), "ledger_verdict": verdict},
        repo_root=repo / ".git",
    )


def _assert_closed(repo, out):
    import subprocess

    from coordinator_core.win_portability import no_console_creationflags

    assert out.get("committed") is True, out
    message = subprocess.run(
        ["git", "log", "-1", "--format=%B", out["sha"]], cwd=str(repo), capture_output=True, text=True,
        check=True, **no_console_creationflags(),
    ).stdout
    assert OPEN_TRAILER in message, message
    legs = {leg: status for leg, (status, _detail) in _run_close_job(repo, out).items()}
    owed = ("review-stamp", "plan-reconciliation", "completion-entry", "baton-ship", "lessons", "close-commit")
    assert {leg: legs.get(leg) for leg in owed if legs.get(leg) != "ran"} == {}, legs


@pytest.mark.parametrize("ruling_run", [OTHER_RUN, driver.ASK_RUN], ids=["re-fire-other-run", "same-run"])
def test_all_ruled_out_register_with_empty_verdict_closes(tmp_path, monkeypatch, ruling_run):
    repo, script = _fixture(tmp_path, monkeypatch, ruling_run)
    _assert_closed(repo, _drive(repo, script, {"items": []}))


def test_verdict_judging_the_ruled_out_row_met_still_closes(tmp_path, monkeypatch):
    repo, script = _fixture(tmp_path, monkeypatch, OTHER_RUN)
    _assert_closed(repo, _drive(repo, script, {"items": [{"id": "R1", "status": "met"}]}))

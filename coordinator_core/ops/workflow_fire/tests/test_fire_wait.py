"""workflow.fire_wait: one non-blocking refreshed read plus the landing commit sha at exit."""
import json
import time

import pytest

from coordinator_core.git import run as git_run
from coordinator_core.ops.workflow_fire import fire, wait


@pytest.fixture
def registry(tmp_path, monkeypatch):
    reg = tmp_path / "fires"
    reg.mkdir()
    monkeypatch.setattr(fire, "_registry_dir", lambda *a: reg)
    monkeypatch.setattr(fire, "_refresh_record_state", lambda record, *a: record)
    return reg


@pytest.fixture
def git_calls(monkeypatch):
    calls = []

    def fake(args, **kw):
        calls.append(args)
        return git_run.GitResult(0, "abc123\n", "", False)

    monkeypatch.setattr(wait, "run_git", fake)
    return calls


def put(registry, **extra):
    record = {"fire_id": "f1", "state": "running", "outcome": None, "run_id": "r1",
              "started_at": time.time(), "timeout_s": 600, "exit_code": None, **extra}
    (registry / "f1.json").write_text(json.dumps(record), encoding="utf-8")


def call():
    return wait._workflow_fire_wait({"fire_id": "f1"})


def test_running_fire_replies_running_without_a_git_spawn(registry, git_calls):
    put(registry, driver_session_id="sid-1")
    reply = call()
    assert reply["state"] == "running" and reply["found"] and not reply["timed_out"]
    assert reply["commit_sha"] is None and git_calls == []


def test_exited_fire_with_landing_sha_needs_no_git(registry, git_calls):
    put(registry, state="exited", outcome="clean", landing={"committed": True, "sha": "deadbeef"})
    reply = call()
    assert (reply["state"], reply["outcome"], reply["run_id"], reply["commit_sha"]) == (
        "exited", "clean", "r1", "deadbeef")
    assert reply["commit_sha_reason"] is None and git_calls == []


def test_exited_fire_without_landing_asks_git_for_the_session_trailer(registry, git_calls):
    put(registry, state="exited", outcome="clean", driver_session_id="sid-9")
    reply = call()
    assert reply["commit_sha"] == "abc123"
    assert git_calls == [["log", "--all", "--fixed-strings", "--grep", "Session-Id: sid-9", "-n1", "--format=%H"]]


def test_uncommitted_landing_falls_through_to_git_lookup(registry, git_calls):
    put(registry, state="exited", landing={"committed": False, "sha": "x"}, driver_session_id="sid-9")
    assert call()["commit_sha"] == "abc123" and len(git_calls) == 1


def test_exited_fire_with_nothing_to_look_up_names_a_reason(registry, git_calls):
    put(registry, state="exited")
    reply = call()
    assert reply["commit_sha"] is None and reply["commit_sha_reason"] and git_calls == []


def test_empty_git_log_reports_nothing_landed(registry, monkeypatch):
    monkeypatch.setattr(wait, "run_git", lambda *a, **k: git_run.GitResult(0, "", "", False))
    put(registry, state="exited", driver_session_id="sid-9")
    reply = call()
    assert reply["commit_sha"] is None and reply["commit_sha_reason"] == "nothing landed"


def test_running_fire_past_its_deadline_is_timed_out(registry, git_calls):
    put(registry, started_at=time.time() - 700, timeout_s=600)
    assert call()["timed_out"] is True


def test_exited_fire_past_its_deadline_is_not_timed_out(registry, git_calls):
    put(registry, state="exited", started_at=time.time() - 700, timeout_s=600)
    assert call()["timed_out"] is False


def test_record_without_timeout_gets_the_14400_default_deadline(registry, git_calls):
    started = time.time()
    put(registry, started_at=started, timeout_s=None)
    reply = call()
    assert reply["timeout_s"] == 14400 and reply["deadline_at"] == started + 14400


def test_a_recorded_timeout_still_overrides_the_default(registry, git_calls):
    started = time.time()
    put(registry, started_at=started, timeout_s=600)
    assert call()["deadline_at"] == started + 600


def test_headless_start_default_is_the_shared_14400():
    import inspect
    from coordinator_core.ops.workflow_fire import headless

    assert wait.DEFAULT_TIMEOUT_S == 14400
    assert inspect.signature(headless.start).parameters["timeout_s"].default == 14400


def test_unknown_fire_id_is_found_false_with_the_full_key_set(registry, git_calls):
    reply = call()
    assert reply["found"] is False and reply["timed_out"] is False
    assert set(reply) == {"fire_id", "found", "state", "outcome", "run_id", "commit_sha",
                          "commit_sha_reason", "timed_out", "exit_code", "timeout_s", "deadline_at",
                          "reason"}


def test_failed_exit_carries_the_reason_in_the_result(registry, git_calls):
    put(registry, state="exited", exit_code=1, outcome="failed", failure_subtype="spawn_denied",
        startup_log_tail="a\nsandbox denied lock\n")
    reply = call()
    assert reply["exit_code"] == 1
    assert "workflow exit_code=1" in reply["reason"] and "spawn_denied" in reply["reason"]
    assert "sandbox denied lock" in reply["reason"]


def test_clean_exit_and_running_fire_have_no_reason(registry, git_calls):
    put(registry, state="exited", exit_code=0, outcome="clean", landing={"committed": True, "sha": "d"})
    assert call()["reason"] is None
    put(registry, exit_code=None)
    assert call()["reason"] is None


def test_missing_fire_id_is_refused():
    with pytest.raises(ValueError):
        wait._workflow_fire_wait({})

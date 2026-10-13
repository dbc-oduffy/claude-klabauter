"""dispatch.terminal_commit routes plan_path + run_id with no script_path to the no-script close.

Fixture: the scripted call on test_terminal_commit_run_close's fixture lands the commit with its
review stamp refused once, so the rows are coded, the close-job record is written and the plan is
not stamped; the run's scratch/warp directory is then deleted.
"""

from __future__ import annotations

import copy
import json
import shutil
from pathlib import Path

import pytest

from coordinator_core.benchmarks import terminal_commit_full_path as driver
from coordinator_core.ops.ceremony import close_background
from coordinator_core.ops.dispatch_emit import run_close, terminal_commit
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

RUN_ID = "run"
STEM = driver.INLINE_REVIEW["integration_stem"]


@pytest.fixture
def landed(tmp_path, monkeypatch):
    from coordinator_core.session import claims

    parent = tmp_path / "work"
    parent.mkdir()
    repo, script = driver.build_fixture(parent)
    warp = repo / "scratch" / "warp"
    warp.mkdir(parents=True)
    (warp / "run.mjs").write_text((repo / script).read_text(encoding="utf-8"), encoding="utf-8")
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
    launches: list = []
    monkeypatch.setattr(close_background, "_launch_close_review", lambda ctx: launches.append(ctx) or True)

    real_mint = terminal_commit._mint_review_stamp
    state = {"refuse": True}

    def mint_once_refused(*args, **kwargs):
        if state["refuse"]:
            state["refuse"] = False
            return {"review_stamp": "refused", "review_stamp_refusal": "withheld for the fixture"}
        return real_mint(*args, **kwargs)

    monkeypatch.setattr(terminal_commit, "_mint_review_stamp", mint_once_refused)
    first = terminal_commit._handler(
        {
            "script_path": script,
            "incomplete_chunks": [],
            "session_id": driver.SESSION_ID,
            "inline_review": copy.deepcopy(driver.INLINE_REVIEW),
        },
        repo_root=repo / ".git",
    )
    assert first["review_stamp"] == "refused" and first["sha"], first
    shutil.rmtree(repo / "scratch")
    launches.clear()
    return {"repo": repo, "script": script, "launches": launches, "first": first}


def _call(env, **extra) -> dict:
    return terminal_commit._handler(
        {"plan_path": driver.PLAN_REL, "run_id": RUN_ID, "session_id": driver.SESSION_ID, **extra},
        repo_root=env["repo"] / ".git",
    )


def _record_path(repo: Path) -> Path:
    return repo / run_close.close_job_path(RUN_ID)


def _record(repo: Path) -> dict:
    return json.loads(_record_path(repo).read_text(encoding="utf-8"))


def _set_record(repo: Path, **fields) -> None:
    data = _record(repo)
    data.update(fields)
    _record_path(repo).write_text(json.dumps(data), encoding="utf-8")


def _review_records(repo: Path) -> list:
    return sorted((repo / ".coordinator-local").rglob(f"{STEM}.md")) if (repo / ".coordinator-local").exists() else []


def _plan_text(repo: Path) -> str:
    return (repo / driver.PLAN_REL).read_text(encoding="utf-8")


def _head(repo: Path) -> str:
    import subprocess

    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    ).stdout.strip()


def test_closes_a_run_whose_scratch_warp_directory_is_absent(landed):
    repo = landed["repo"]
    assert not (repo / "scratch" / "warp").exists()
    assert "status: executing" in _plan_text(repo)

    out = _call(landed)

    assert out["review_stamp"] == "minted", out
    assert out["plan_status"] == "implemented"
    assert out["criterion_status"] == "met" and out["verdict_source"] == "close-job"
    text = _plan_text(repo)
    assert "status: implemented" in text and "review_stamp:" in text
    for key in ("stranded", "undeclared_dirty", "incomplete_reasons", "next"):
        assert key not in out
    record = _record(repo)
    assert record["review_receipt"] == driver.PLAN_REL
    assert record["legs"]["review"]["status"] == "queued"
    assert len(landed["launches"]) == 1
    outstanding = out["close"]["outstanding"]
    assert not {"review", "plan-reconciliation", "completion-entry"} & set(outstanding)


def test_refuses_without_a_judge_verdict_and_writes_nothing(landed):
    repo = landed["repo"]
    _set_record(repo, criterion=None)
    for path in _review_records(repo):
        path.unlink()
    plan_before, record_before = _plan_text(repo), _record_path(repo).read_bytes()

    out = _call(landed)

    assert out["refused"] == "no-judge-verdict" and out["committed"] is False
    assert _plan_text(repo) == plan_before
    assert _record_path(repo).read_bytes() == record_before
    assert landed["launches"] == []


def test_refuses_when_the_review_record_holds_no_criterion(landed):
    repo = landed["repo"]
    _set_record(repo, criterion=None)
    records = _review_records(repo)
    assert records, "the scripted call leaves the run's review record on disk"
    for path in records:
        text = path.read_text(encoding="utf-8")
        assert "criterion:" in text
        path.write_text(text.replace("criterion:", "criterion_removed:"), encoding="utf-8")
    plan_before, record_before = _plan_text(repo), _record_path(repo).read_bytes()

    out = _call(landed)

    assert out["refused"] == "no-judge-verdict"
    assert _plan_text(repo) == plan_before
    assert _record_path(repo).read_bytes() == record_before


def test_task_output_naming_the_deleted_script_still_takes_the_no_script_path(landed, tmp_path):
    digest = {
        "next_action": {
            "params": {
                "script_path": landed["script"],
                "incomplete_chunks": [],
                "inline_review": copy.deepcopy(driver.INLINE_REVIEW),
            }
        }
    }
    path = tmp_path / "task-output.json"
    path.write_text(json.dumps(digest), encoding="utf-8")

    out = _call(landed, task_output_path=str(path))

    assert "cannot read params.script_path" not in str(out.get("error", ""))
    assert out["verdict_source"] == "close-job" and out["review_stamp"] == "minted", out
    assert "stranded" not in out


def test_second_identical_call_lands_nothing(landed):
    repo = landed["repo"]
    assert _call(landed)["plan_status"] == "implemented"
    head = _head(repo)
    plan_after, record_after = _plan_text(repo), _record_path(repo).read_bytes()
    landed["launches"].clear()

    out = _call(landed)

    assert out["nothing_to_commit"] is True and out["committed"] is False
    assert _head(repo) == head
    assert landed["launches"] == []
    assert _plan_text(repo) == plan_after
    assert _record_path(repo).read_bytes() == record_after


def test_a_scripted_call_without_a_script_still_refuses_as_before(landed):
    out = terminal_commit._handler(
        {"incomplete_chunks": [], "inline_review": copy.deepcopy(driver.INLINE_REVIEW)},
        repo_root=landed["repo"] / ".git",
    )

    assert out["committed"] is False
    assert out["error"] == "dispatch.terminal_commit params.script_path is required"
    assert "stranded" in out

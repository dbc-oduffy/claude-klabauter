"""A land_run repeat call on a landed run takes a recorded met criterion-rejudge verdict over an unmet close-job criterion."""

from __future__ import annotations

import subprocess
from datetime import datetime, timedelta, timezone

import pytest
import yaml

from coordinator_core.benchmarks import terminal_commit_full_path as driver
from coordinator_core.ops.dispatch_emit.grind_commit_contract import ALREADY_LANDED_KEY
from coordinator_core.ops.dispatch_emit.tests.test_land_run_reclose import KEY, Env, env  # noqa: F401
from coordinator_core.ops.dispatch_emit.verdict_supersession import REJUDGE_KIND, VERDICT_DIR

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_UNMET = {"status": "not_met", "observation": "stranded file absent", "sidecar": "j1.md"}


def _write_rejudge(env: Env, status: str, *, at: datetime) -> None:
    target = env.repo / VERDICT_DIR / "2026-11" / f"rejudge-{status}-{at.strftime('%H%M%S%f')}.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    fm = {
        "kind": REJUDGE_KIND, "plan_id": driver.PLAN_ID,
        "recorded_at": at.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        "criterion": {"status": status, "observation": "a.py exists at HEAD", "sidecar": None},
    }
    target.write_text("---\n" + yaml.safe_dump(fm, sort_keys=False) + "---\n", encoding="utf-8")


def _review_instant(env: Env) -> datetime:
    """The terminal commit's committer time, the floor a re-judge must be recorded after."""
    out = subprocess.run(
        ["git", "-C", str(env.repo), "log", "-1", "--format=%cI", env.sha],
        capture_output=True, text=True, check=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return datetime.fromisoformat(out.stdout.strip()).astimezone(timezone.utc)


def test_a_default_repeat_call_takes_a_newer_met_rejudge_record(env):
    env.set_record(criterion=_UNMET)
    _write_rejudge(env, "met", at=_review_instant(env) + timedelta(minutes=5))

    out = env.reland()

    assert out[ALREADY_LANDED_KEY] is True
    assert out["criterion_status"] == "met"
    assert env.record()["criterion"]["status"] == "met"
    assert env.launches == [KEY]


def test_without_a_rejudge_record_the_unmet_criterion_stands(env):
    env.set_record(criterion=_UNMET)

    out = env.reland()

    assert out["criterion_status"] == "not_met"
    assert env.record()["criterion"] == _UNMET


def test_a_not_met_rejudge_record_does_not_flip_the_criterion(env):
    env.set_record(criterion=_UNMET)
    _write_rejudge(env, "not_met", at=_review_instant(env) + timedelta(minutes=5))

    out = env.reland()

    assert out["criterion_status"] == "not_met"


def test_a_met_rejudge_older_than_the_review_does_not_flip_the_criterion(env):
    env.set_record(criterion=_UNMET)
    _write_rejudge(env, "met", at=_review_instant(env) - timedelta(days=1))

    out = env.reland()

    assert out["criterion_status"] == "not_met"

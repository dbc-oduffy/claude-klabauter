"""Re-close settles a leg the record still lists failed: the settlement supersedes it, never raises."""

from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest

from coordinator_core.ops.dispatch_emit import run_close
from coordinator_core.ops.dispatch_emit.run_close import LegResult

RUN = "run-r.workflow"


def _record(root):
    return json.loads((root / run_close.close_job_path(RUN)).read_text(encoding="utf-8"))


def _deferred_record(root):
    run_close.write_close_job(
        root, RUN, ["completion-entry", "baton-ship", "review"], datetime.now().astimezone() + timedelta(minutes=10)
    )
    run_close.record_leg_outcome(root, RUN, "completion-entry", "failed", "not started: job process time 469ms reached 400ms")
    run_close.record_leg_outcome(root, RUN, "baton-ship", "failed", "not started: job process time 469ms reached 400ms")


def test_settling_a_failed_listed_leg_supersedes_it(tmp_path):
    _deferred_record(tmp_path)
    run_close.amend_close_job(
        tmp_path, RUN,
        settled={"completion-entry": LegResult("ran", "entry written")},
        queue=["review"], deadline=datetime.now().astimezone() + timedelta(minutes=10),
    )
    rec = _record(tmp_path)
    assert "completion-entry" not in rec["legs"]
    assert rec["settled"]["completion-entry"]["status"] == "ran"
    assert rec["legs"]["review"]["status"] == "queued"
    closed = run_close._parse_close(rec, RUN, datetime.now().astimezone())
    assert closed.legs["completion-entry"].status == "ran"


def test_the_same_leg_in_queue_and_settled_is_still_refused(tmp_path):
    _deferred_record(tmp_path)
    with pytest.raises(ValueError, match="both queued and settled"):
        run_close.amend_close_job(
            tmp_path, RUN, settled={"review": LegResult("ran")}, queue=["review"],
            deadline=datetime.now().astimezone() + timedelta(minutes=10),
        )

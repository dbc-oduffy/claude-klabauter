"""close.background on an already-closed run: an absent sizing ship is re-paid, a shipped sizing is
left alone, and an existing record with nothing owed is an error naming the cause, never an empty
``legs`` map."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest
import yaml

from coordinator_core.ops.ceremony import close_background as cb
from coordinator_core.ops.ceremony.tests.test_close_background_land_run_legs import (
    PLAN, RUN, SIZING, _build, _git, _job, _write,
)
from coordinator_core.ops.dispatch_emit import run_close
from coordinator_core.ops.tests.test_deliverable_cascade_kinds import _sizing_body

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_CLOSED = ("sizing-ship", "plan-reconciliation", "close-commit")


def _closed_record(root: Path, sha: str, sizing_ship: tuple) -> None:
    """The record of a run whose close job already finished: every leg recorded, none queued."""
    _job(root, sha, _CLOSED)
    for leg in _CLOSED:
        status, detail = sizing_ship if leg == "sizing-ship" else ("ran", "paid")
        run_close.record_leg_outcome(root, RUN, leg, status, detail)


def _sizing_status(root: Path) -> str:
    return yaml.safe_load((root / SIZING).read_text(encoding="utf-8"))["status"]


def _reclose(root: Path) -> dict:
    return cb._handler({"run_id": RUN, "claimed_batons": [], "source_memo": None}, root / ".git")


def test_sizing_ship_recorded_not_applicable_by_old_code_is_re_paid(tmp_path):
    sha = _build(tmp_path)
    _closed_record(tmp_path, sha, ("not-applicable", "plan cites no sizing"))
    assert _sizing_status(tmp_path) == "routed"

    reply = _reclose(tmp_path)

    assert "error" not in reply, reply
    assert reply["legs"]["sizing-ship"]["status"] == "ran", reply
    assert "close-commit" in reply["legs"]
    assert _sizing_status(tmp_path) == "shipped"
    assert _git(tmp_path, "show", f"HEAD:{SIZING}").count("status: shipped") == 1


def test_failed_sizing_ship_with_the_sizing_unshipped_is_re_paid(tmp_path):
    sha = _build(tmp_path)
    _closed_record(tmp_path, sha, ("failed", "ship skipped: earlier refusal"))

    reply = _reclose(tmp_path)

    assert reply["legs"]["sizing-ship"]["status"] == "ran", reply
    assert _sizing_status(tmp_path) == "shipped"


def test_a_shipped_sizing_is_not_shipped_again(tmp_path):
    sha = _build(tmp_path)
    _write(tmp_path, SIZING, _sizing_body(status="shipped", deliverable_id="dlv-landrun-legs-000001", plan=PLAN))
    _git(tmp_path, "add", SIZING)
    _git(tmp_path, "commit", "-q", "-m", "ship sizing")
    head = _git(tmp_path, "rev-parse", "HEAD")
    _closed_record(tmp_path, sha, ("not-applicable", "plan cites no sizing"))

    reply = _reclose(tmp_path)

    assert "sizing-ship" not in reply.get("legs", {}), reply
    assert _git(tmp_path, "rev-parse", "HEAD") == head
    assert _sizing_status(tmp_path) == "shipped"


def test_existing_record_with_nothing_owed_is_an_error_naming_the_cause(tmp_path):
    (tmp_path / ".git").mkdir()
    run_close.write_close_job(
        tmp_path, RUN, ("baton-drain",), datetime.now().astimezone() + timedelta(minutes=10)
    )
    run_close.record_leg_outcome(tmp_path, RUN, "baton-drain", "ran", "paid")

    reply = cb._handler({"run_id": RUN, "claimed_batons": [], "source_memo": None}, tmp_path / ".git")

    assert "legs" not in reply, reply
    assert reply["error"] and RUN in reply["error"]

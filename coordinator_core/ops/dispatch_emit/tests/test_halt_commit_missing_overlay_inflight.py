"""A halt commit never carries the run's `overlay-inflight` process marker: a detached overlay child releases
it between the commit's listing and its read, and a tracked marker is never staged as a deletion."""

from __future__ import annotations

import time

import pytest

from coordinator_core.ops.dispatch_emit import run_artifacts
from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT
from coordinator_core.ops.dispatch_emit.clean_overlay_verify import INFLIGHT_FILE


RUN = f"ask-{time.strftime('%Y%m%dT%H%M%S', time.gmtime())}-cccccc"
SIZING = f"{run_artifacts._SIZINGS_REL}/2026-11-18-s.yaml"
RECORD = f"{RECORD_DIR_ROOT}/{RUN}"
MARKER = f"{RECORD}/{INFLIGHT_FILE}"
LEDGER = {"halted": "ledger-check", "reason": "row C2 open"}
PASSING = {"targeted_tests": {"status": "pass", "tests_failed": 0, "baseline": None}}


def test_the_record_commit_split_keeps_an_unignored_marker_out_of_the_landable_paths():
    from coordinator_core.ops.dispatch_emit import halted_record_commit

    landable, state = halted_record_commit._split_run_state([f"{RECORD}/run.json", MARKER], RECORD, False)

    assert landable == [f"{RECORD}/run.json"]
    assert state == [MARKER]

"""A refire that resumes a halted ask run records it as run.json resumed_from."""

from __future__ import annotations

import pytest

from coordinator_core.ops.dispatch_emit import op
from coordinator_core.ops.dispatch_emit import requirements_ledger as rl
from coordinator_core.ops.dispatch_emit.ask_contract import record_dir
from coordinator_core.ops.dispatch_emit.tests.test_refire_inherits_halted_base import (  # noqa: F401
    ASK,
    REL,
    WARP,
    _fire,
    _halt,
    _status,
    composed,
    repo,
)


def _resumed_from(repo, run_id):
    return (rl.read_run(rl.run_dir(repo, run_id)) or {}).get(rl.RESUMED_FROM_FIELD)


def test_a_halted_ask_run_predecessor_is_recorded_as_resumed_from(repo, composed):
    _halt(repo, ASK, "a" * 40)
    reply = _fire(repo)
    assert reply["run_id"] != ASK
    assert _resumed_from(repo, reply["run_id"]) == ASK
    assert _status(repo, ASK) == "resumed"


def test_a_rebound_run_records_no_resumed_from(repo, composed):
    _halt(repo, WARP, "a" * 40)
    assert _fire(repo)["run_id"] == WARP
    assert _resumed_from(repo, WARP) is None


def test_a_fresh_fire_records_no_resumed_from(repo, composed):
    reply = _fire(repo)
    assert _resumed_from(repo, reply["run_id"]) is None

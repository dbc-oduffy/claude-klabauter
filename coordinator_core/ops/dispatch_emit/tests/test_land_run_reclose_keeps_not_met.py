"""A reclose or requeue of a run whose close record lists unlanded rows keeps the criterion not_met."""

from __future__ import annotations

from coordinator_core.ops.dispatch_emit import land_run, run_close
from coordinator_core.ops.dispatch_emit.grind_commit_contract import ALREADY_LANDED_KEY
from coordinator_core.ops.dispatch_emit.tests.test_land_run_reclose import KEY, _FRESH, Env, env  # noqa: F401

import pytest

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_DEMOTED = {**_FRESH, "status": "not_met", "reason": "row C1 did not land"}


def _mark_unlanded(env: Env) -> None:
    record = env.record()
    env.set_record(criterion=_DEMOTED, inputs={**record.get("inputs", {}), "unlanded": ["C1"]})


def test_unlanded_is_a_close_input_key():
    assert "unlanded" in run_close.CLOSE_INPUT_KEYS


def test_a_reclose_with_a_met_criterion_replies_not_met_while_rows_are_unlanded(env):
    _mark_unlanded(env)

    out = env.reland(task_output_path=env.digest(criterion=_FRESH))

    assert out[ALREADY_LANDED_KEY] is True
    assert out["criterion_status"] == "not_met"
    assert env.record()["criterion"]["status"] == "not_met"


def test_a_requeue_with_a_met_criterion_does_not_replace_not_met(env):
    _mark_unlanded(env)

    land_run._requeue_close(env.repo, {}, key=KEY, criterion=_FRESH)

    assert env.record()["criterion"]["status"] == "not_met"


def _mark_unlanded_paths(env: Env, path: str) -> None:
    record = env.record()
    env.set_record(
        criterion=_DEMOTED,
        inputs={**record.get("inputs", {}), "unlanded": ["C1"], "unlanded_paths": {"C1": [path]}},
    )


def test_a_reclose_keeps_not_met_while_the_recorded_paths_are_still_uncommitted(env):
    (env.repo / "pending.py").write_text("x = 1\n", encoding="utf-8")
    _mark_unlanded_paths(env, "pending.py")

    out = env.reland(task_output_path=env.digest(criterion=_FRESH))

    assert out["criterion_status"] == "not_met"
    assert env.record()["inputs"]["unlanded"] == ["C1"]


def test_a_reclose_clears_rows_whose_recorded_paths_have_landed_and_takes_the_met_criterion(env):
    _mark_unlanded_paths(env, "a.py")

    out = env.reland(task_output_path=env.digest(criterion=_FRESH))

    assert out["criterion_status"] == "met"
    record = env.record()
    assert record["criterion"] == _FRESH
    assert "unlanded" not in record["inputs"]
    assert "unlanded_paths" not in record["inputs"]


def test_a_requeue_clears_only_the_landed_rows(env):
    (env.repo / "pending.py").write_text("x = 1\n", encoding="utf-8")
    record = env.record()
    env.set_record(
        criterion=_DEMOTED,
        inputs={
            **record.get("inputs", {}), "unlanded": ["C1", "C2"],
            "unlanded_paths": {"C1": ["a.py"], "C2": ["pending.py"]},
        },
    )

    land_run._requeue_close(env.repo, {}, key=KEY, criterion=_FRESH)

    inputs = env.record()["inputs"]
    assert inputs["unlanded"] == ["C2"]
    assert inputs["unlanded_paths"] == {"C2": ["pending.py"]}
    assert env.record()["criterion"]["status"] == "not_met"


def test_a_reclose_without_unlanded_rows_still_takes_the_met_criterion(env):
    out = env.reland(task_output_path=env.digest(criterion=_FRESH))

    assert out["criterion_status"] == "met"
    assert env.record()["criterion"] == _FRESH

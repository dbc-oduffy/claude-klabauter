"""identical_rehalt_reason: the dd822d second-refire shape is true; each release condition is false."""

from __future__ import annotations

import pytest

from coordinator_core.ops.dispatch_emit.ask_contract import (
    LEG_DISPATCH,
    LEG_SKIP,
    RESUME_LEDGER_CREDITED_FIELD,
    RESUME_LEDGER_HALT_SHA_FIELD,
    RESUME_LEDGER_KIND_FIELD,
    RESUME_LEDGER_NOTES_FIELD,
    RESUME_LEDGER_SINCE_HALT_SHAS_FIELD,
    RESUME_LEDGER_UNSERVED_FIELD,
)
from coordinator_core.ops.dispatch_emit.refire_noop import identical_rehalt_reason, unserved_blocking_reason
from coordinator_core.ops.dispatch_emit.tests.test_ask_gate import repo  # noqa: F401

SHA = "a" * 40


def _ledger(**over):
    base = {
        "blocking": ["R1"],
        RESUME_LEDGER_HALT_SHA_FIELD: SHA,
        RESUME_LEDGER_NOTES_FIELD: [],
        RESUME_LEDGER_SINCE_HALT_SHAS_FIELD: [],
        RESUME_LEDGER_CREDITED_FIELD: {},
    }
    base.update(over)
    return base


def _call(ledger=None, rows=(), owed=(), gated=(), derived=(), leg=None):
    return identical_rehalt_reason(_ledger() if ledger is None else ledger, rows, owed, gated, derived, leg)


def test_dd822d_second_refire_shape_is_identical_rehalt():
    reason = _call(leg={"action": LEG_SKIP})
    assert reason is not None
    assert "R1" in reason and SHA[:10] in reason and "workflow.revise" in reason


def test_all_landed_kind_also_true():
    assert _call(_ledger(**{RESUME_LEDGER_KIND_FIELD: "all-landed"})) is not None


def test_no_leg_is_true():
    assert _call(leg=None) is not None


@pytest.mark.parametrize(
    "kwargs",
    [
        {"rows": [{"id": "C5"}]},
        {"owed": ["C5"]},
        {"gated": ["C6"]},
        {"derived": ["LR1"]},
        {"leg": {"action": LEG_DISPATCH}},
        {"ledger": _ledger(**{RESUME_LEDGER_NOTES_FIELD: ["rule: add gem_holder"]})},
        {"ledger": _ledger(**{RESUME_LEDGER_SINCE_HALT_SHAS_FIELD: [SHA]})},
        {"ledger": _ledger(**{RESUME_LEDGER_CREDITED_FIELD: {"R1": SHA}})},
        {"ledger": _ledger(**{RESUME_LEDGER_UNSERVED_FIELD: ["R1"]})},
        {"ledger": _ledger(blocking=[])},
        {"ledger": _ledger(**{RESUME_LEDGER_KIND_FIELD: "commit"})},
    ],
)
def test_each_release_condition_is_none(kwargs):
    assert _call(**kwargs) is None


def test_a_blocking_id_the_register_has_since_ruled_releases_the_noop():
    assert _call(_ledger(rulable_ids=["R2"])) is None


def test_a_blocking_id_still_rulable_keeps_the_noop():
    assert _call(_ledger(rulable_ids=["R1", "R2"])) is not None


def test_no_resume_ledger_is_none():
    assert identical_rehalt_reason(None, (), (), (), (), None) is None


def _unserved(ledger=None, serving=()):
    return unserved_blocking_reason(_ledger() if ledger is None else ledger, serving)


def test_no_serving_row_refuses_naming_the_id_and_the_add_task_step():
    reason = _unserved()
    assert reason is not None
    assert "R1" in reason and "plan.tasks.mutate" in reason and "workflow.revise" in reason


@pytest.mark.parametrize("disposition", ["coded", "done", "wont_do", "spun_off", "backlogged"])
def test_only_closed_serving_rows_still_refuse(disposition):
    assert _unserved(serving=[{"id": "C1", "disposition": disposition}]) is not None


def test_an_em_discharged_open_row_still_refuses():
    ledger = _ledger(em_discharged={"C1": "done by hand"})
    assert _unserved(ledger, serving=[{"id": "C1", "disposition": "open"}]) is not None


@pytest.mark.parametrize(
    "kwargs",
    [
        {"serving": [{"id": "C1", "disposition": "open"}]},
        {"serving": [{"id": "C1", "disposition": "open"}], "ledger": _ledger(em_discharged={"C2": "x"})},
        {"ledger": _ledger(**{RESUME_LEDGER_NOTES_FIELD: ["rule: add row"]})},
        {"ledger": _ledger(blocking=[])},
        {"ledger": _ledger(rulable_ids=["R2"])},
        {"ledger": _ledger(**{RESUME_LEDGER_KIND_FIELD: "all-landed"})},
        {"ledger": None, "serving": []},
    ],
)
def test_unserved_release_conditions_are_none(kwargs):
    ledger = kwargs.get("ledger", _ledger())
    assert unserved_blocking_reason(ledger, kwargs.get("serving", ())) is None


def _gate_repo(repo, row_disposition, register_rows):
    from coordinator_core.ops.dispatch_emit.tests.test_ask_gate import REL
    from coordinator_core.ops.dispatch_emit.tests.test_ask_gate_resume import PLAN_REL
    from coordinator_core.ops.dispatch_emit.tests.test_ask_gate_resume_ledger_remaining_leg import _halted

    _halted(repo, [f"update {REL}"])
    plan = repo / PLAN_REL
    text = plan.read_text(encoding="utf-8").replace(
        "  disposition: open\n", f"  disposition: {row_disposition}\n  register_rows: {register_rows}\n"
    )
    plan.write_text(text, encoding="utf-8", newline="\n")


def test_gate_refuses_before_a_manifest_handle_when_no_open_row_serves(repo, monkeypatch):  # noqa: F811
    from coordinator_core.ops.dispatch_emit import ask_gate
    from coordinator_core.ops.dispatch_emit.ask_contract import HALT_REFUSAL, LEG_DISPATCH, RESUME_LEDGER_LEG_FIELD
    from coordinator_core.ops.dispatch_emit.tests.test_refire_after_ledger_halt_executes_unmet_rows_incident import (
        REFIRE_RUN,
    )
    from coordinator_core.ops.dispatch_emit.tests.test_ask_gate import REL

    monkeypatch.setattr(ask_gate, "_derive_resume_rows", lambda *_a, **_k: [])
    _gate_repo(repo, "open", "[R9]")

    out = ask_gate._handler({"sizing_path": REL, "run_id": REFIRE_RUN}, repo)

    assert out["resume_ledger"][RESUME_LEDGER_LEG_FIELD]["action"] == LEG_DISPATCH
    assert (out.get("halt") or {}).get("kind") == HALT_REFUSAL, out
    assert "R1" in out["halt"]["reason"] and "plan.tasks.mutate" in out["halt"]["reason"]
    assert "manifest_handle" not in out


def test_gate_stages_when_an_open_row_serves_the_blocking_id(repo, monkeypatch):  # noqa: F811
    from coordinator_core.ops.dispatch_emit import ask_gate
    from coordinator_core.ops.dispatch_emit.tests.test_refire_after_ledger_halt_executes_unmet_rows_incident import (
        REFIRE_RUN,
    )
    from coordinator_core.ops.dispatch_emit.tests.test_ask_gate import REL

    monkeypatch.setattr(ask_gate, "_derive_resume_rows", lambda *_a, **_k: [])
    _gate_repo(repo, "open", "[R1]")

    out = ask_gate._handler({"sizing_path": REL, "run_id": REFIRE_RUN}, repo)

    assert out.get("halt") is None and "manifest_handle" in out, out

"""Pins the shared run contract's additions and the spec registry."""
from __future__ import annotations

import pytest

from coordinator_core.ceremony_common.run import contract as c
from coordinator_core.ceremony_common.run import spec


def _digest(**kw) -> dict:
    return c.build_digest(
        ceremony="workday-complete", run_id="20261012T093000Z-ab12", repo_root="/repo",
        state="ran", finish_argv=["finish"], groups=[], **kw)


def test_digest_with_chained_validates_and_without_too():
    assert "chained" not in _digest()
    assert _digest(chained={"ceremony": "workday-start"})["chained"] == {"ceremony": "workday-start"}


def test_chained_must_be_object():
    bad = _digest()
    bad["chained"] = "x"
    assert c.validate_digest(bad)


def _results(decision: dict) -> dict:
    return {"groups": {"judgment": [{"key": "k", "outcome": "ok", "decision": decision}]}}


def test_decision_payload_is_optional_object():
    assert c.validate_agent_results(_results({"disposition": "accept", "rationale": "r"})) == []
    assert c.validate_agent_results(
        _results({"disposition": "accept", "rationale": "r", "payload": {"a": 1}})) == []
    assert c.validate_agent_results(
        _results({"disposition": "accept", "rationale": "r", "payload": "x"}))


def test_decision_row_carries_payload_only_when_set():
    d = c.Decision("p", "q", "d", "rule", "r", "why", "e")
    assert "payload" not in d.to_row()
    assert c.Decision("p", "q", "d", "rule", "r", "why", "e", payload={"a": 1}).to_row()["payload"] == {"a": 1}


def test_registry_keys_exact():
    assert set(spec.CEREMONY_SPECS) == {"workday_start", "workday_complete", "workweek_start"}


@pytest.mark.parametrize("name", sorted(spec.CEREMONY_SPECS))
def test_every_registered_spec_loads(name):
    loaded = spec.load_spec(name)
    assert isinstance(loaded, spec.CeremonySpec)
    assert loaded.name.replace("-", "_") == name


def test_absent_spec_module_is_a_named_error(monkeypatch):
    monkeypatch.setattr(spec, "CEREMONY_SPECS", {"ghost": "coordinator_core.no_such_ceremony.spec:SPEC"})
    with pytest.raises(spec.SpecUnavailable, match="ghost"):
        spec.load_spec("ghost")
    with pytest.raises(spec.SpecUnavailable):
        spec.load_spec("nope")


def test_no_parked_status_and_every_class_has_a_deadline():
    assert "parked" not in c.STEP_STATUSES
    assert set(c.DEADLINE_S) == set(c.COST_CLASSES)
    assert {"suite", "launch"} <= set(c.COST_CLASSES)
    assert c.NEVER_OFFERED_DISPOSITIONS == {"discard"}


def test_step_from_directive_op_record_and_new_keys():
    s = c.step_from_directive(
        {"id": "e1", "op": "git.maintenance", "stdin_from": "d0", "best_effort": True},
        stage="x", heading="H", cost_class="engine")
    assert (s.kind, s.target, s.stdin_from, s.best_effort) == ("op", "git.maintenance", "d0", True)
    plain = c.step_from_directive({"id": "d", "cli": "x"}, stage="x", heading="H", cost_class="engine")
    assert plain.kind == "cli" and plain.stdin_from is None and plain.best_effort is False


def test_schema_path_exists():
    assert c.SCHEMA_PATH.is_file()

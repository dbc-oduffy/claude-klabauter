from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from coordinator_core.contract.cockpit_schema import ENTITY_SCHEMAS
from coordinator_core.contract.cockpit_schema.emit_schema import emit_schemas
from coordinator_core.contract.cockpit_schema.entities import (
    DeliveryVerdictSummary,
    RulingSummary,
    SizingSummary,
    SnapshotEnvelope,
    WorkflowRun,
)

SIZING = {
    "path": "state/sizings/2026-10-24-x.yaml",
    "intent": "do x",
    "route": "plan",
    "tshirt": "M",
    "status": "accepted",
    "xl_exit": None,
    "created": "2026-10-24",
    "exit_criterion_accepted": True,
    "linked_plan": None,
    "resolution": "unresolved",
}
RUN = {
    "run_id": "ask-20261024T005815-667dbd",
    "stage": "staged",
    "status": "live",
    "sizing_rel": "state/sizings/2026-10-24-x.yaml",
    "session_id": None,
    "started_at": "2026-10-24T01:00:00Z",
    "last_activity_at": "2026-10-24T01:05:00Z",
    "steps_planned": 8,
    "outcome": None,
}
VERDICT = {
    "path": "state/delivery-verdicts/2026-10/a.md",
    "observed_at": "2026-10-24T01:00:00Z",
    "plan_id": "pln-x-6b5214",
    "head_sha": "4ba0507e609b3b251bba89c61a54e2902ffc9a46",
    "recorded_at": "2026-10-03T21:13:23.358699Z",
    "session_id": "",
    "delivery": {"verdict": "PASS", "unbacked": [{"claim": "c", "anchor": "a"}]},
    "criterion": {"status": "indeterminate", "observation": "o", "sidecar": None},
    "foreign_claims": [],
}
RULING_FENCED = {
    "path": "state/apm-rulings/a.md",
    "observed_at": "2026-10-24T01:00:00Z",
    "title": "t",
    "from": "apm",
    "created": "2026-10-23",
    "source": "plan",
    "decision": "hold",
    "pm_must_see": False,
}
RULING_FENCELESS = {
    **{k: None for k in ("title", "from", "created", "source", "decision", "pm_must_see")},
    "path": "state/apm-rulings/b.md",
    "observed_at": "2026-10-24T01:00:00Z",
}

PINNED_REQUIRED = {
    "sizing-summary": set(SIZING),
    "workflow-run": set(RUN),
    "delivery-verdict-summary": set(VERDICT),
    "ruling-summary": set(RULING_FENCED),
}
MODELS = {
    "sizing-summary": (SizingSummary, SIZING),
    "workflow-run": (WorkflowRun, RUN),
    "delivery-verdict-summary": (DeliveryVerdictSummary, VERDICT),
    "ruling-summary": (RulingSummary, RULING_FENCED),
}


@pytest.mark.parametrize("name", list(MODELS))
def test_registered_and_round_trips(name):
    model, fixture = MODELS[name]
    assert ENTITY_SCHEMAS[name] is model
    parsed = model.model_validate(fixture)
    assert model.model_validate(parsed.model_dump(by_alias=True)) == parsed


def test_fenceless_ruling_all_optional_keys_null():
    r = RulingSummary.model_validate(RULING_FENCELESS)
    assert r.title is None and r.from_ is None and r.pm_must_see is None


@pytest.mark.parametrize("name", list(MODELS))
def test_extra_key_refused(name):
    model, fixture = MODELS[name]
    with pytest.raises(ValidationError):
        model.model_validate({**fixture, "appetite": "x"})


@pytest.mark.parametrize(
    "model,fixture,patch",
    [
        (SizingSummary, SIZING, {"resolution": "bogus"}),
        (WorkflowRun, RUN, {"stage": "bogus"}),
        (WorkflowRun, RUN, {"started_at": "yesterday"}),
    ],
)
def test_invalid_refused(model, fixture, patch):
    with pytest.raises(ValidationError):
        model.model_validate({**fixture, **patch})


def test_emitted_required_is_exactly_the_pinned_set(tmp_path):
    emit_schemas({n: m for n, (m, _) in MODELS.items()}, out_dir=tmp_path)
    for name, pinned in PINNED_REQUIRED.items():
        doc = json.loads((tmp_path / f"{name}.schema.json").read_text(encoding="utf-8"))
        assert set(doc["required"]) == pinned
        assert doc["additionalProperties"] is False


def test_envelope_arrays_optional_and_verdicts_rulings_absent(tmp_path):
    emit_schemas({"snapshot-envelope": SnapshotEnvelope}, out_dir=tmp_path)
    doc = json.loads((tmp_path / "snapshot-envelope.schema.json").read_text(encoding="utf-8"))
    for key in ("sizings", "workflow_runs"):
        assert doc["properties"][key]["type"] == "array"
        assert key not in doc["required"]
    assert not {"delivery_verdicts", "verdicts", "rulings"} & set(doc["properties"])

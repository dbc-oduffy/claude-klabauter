"""The landed halted.json carries the halt's refire envelope, and kept work stamps `resume.stage`."""

from __future__ import annotations

import json

from coordinator_core.ops.dispatch_emit import halt_refire, halted_run, kept_work
from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT
from coordinator_core.ops.dispatch_emit.tests.test_halt_records_kept_work import (  # noqa: F401
    PLAN,
    RUN,
    WRITERS,
    _chunks,
    repo,
)

HALTED_REL = f"{RECORD_DIR_ROOT}/{RUN}/{halted_run.HALTED_FILE}"
KEPT_POINTER = f"{RECORD_DIR_ROOT}/{RUN}/{kept_work.KEPT_FILE}"


def _envelope() -> dict:
    return halt_refire.refire_next_action(
        call='Workflow({script: "x.mjs"})', run_id=RUN, sizing="state/sizings/s.yaml", halted="refusal"
    )


def _halted(envelope, *, with_plan: bool = False) -> dict:
    halted: dict = {"halted": "refusal", "reason": "readiness refused"}
    if envelope is not None:
        halted["next_action"] = envelope
    if with_plan:
        halted["plan"] = PLAN
        halted["blitz"] = {"pulled": [{"planPath": PLAN, "reviews": "r.json", "coverage": "c"}]}
    return halted


def test_valid_envelope_is_written_and_invalid_or_legacy_is_omitted(tmp_path):
    base = dict(halted="refusal", sizing_rel="s.yaml")
    plain = halted_run.write_halt_marker(tmp_path / "a", **base).read_bytes()

    halted_run.write_halt_marker(tmp_path / "b", next_action=_envelope(), **base)
    assert json.loads((tmp_path / "b" / "halted.json").read_text())["next_action"] == _envelope()

    bad = {**_envelope(), "call": "fix the file by hand"}
    halted_run.write_halt_marker(tmp_path / "c", next_action=bad, **base)
    halted_run.write_halt_marker(tmp_path / "d", next_action={"kind": "legacy"}, **base)
    assert (tmp_path / "c" / "halted.json").read_bytes() == plain
    assert (tmp_path / "d" / "halted.json").read_bytes() == plain


def test_record_resume_stage_sets_stage_and_refuses_otherwise(tmp_path):
    stage = {"kept": "k.json", "chunks": ["C1"], "plan_gate": False}
    with_env = tmp_path / "e"
    halted_run.write_halt_marker(with_env, halted="refusal", sizing_rel="s.yaml", next_action=_envelope())
    assert halted_run.record_resume_stage(with_env, stage) is True
    got = json.loads((with_env / "halted.json").read_text())["next_action"]
    assert got["resume"]["stage"] == stage
    assert {k: v for k, v in got.items() if k != "resume"} == {k: v for k, v in _envelope().items() if k != "resume"}
    assert halt_refire.is_refire_next_action(got)

    without = tmp_path / "f"
    halted_run.write_halt_marker(without, halted="refusal", sizing_rel="s.yaml")
    before = (without / "halted.json").read_bytes()
    assert halted_run.record_resume_stage(without, stage) is False
    assert (without / "halted.json").read_bytes() == before

    resumed = tmp_path / "g"
    halted_run.write_halt_marker(resumed, halted="refusal", sizing_rel="s.yaml", next_action=_envelope())
    halted_run.mark_resumed(resumed)
    before = (resumed / "halted.json").read_bytes()
    assert halted_run.record_resume_stage(resumed, stage) is False
    assert (resumed / "halted.json").read_bytes() == before


@WRITERS
def test_callers_forward_next_action_and_stamp_after_record_kept(repo, call, monkeypatch):
    from coordinator_core.ops.dispatch_emit import halted_record_commit

    order: list = []
    real_marker, real_kept, real_stage = halted_run.write_halt_marker, halted_record_commit._record_kept, halted_run.record_resume_stage

    def marker(*a, **k):
        order.append(("marker", k.get("next_action")))
        return real_marker(*a, **k)

    def kept(*a, **k):
        order.append(("kept",))
        return real_kept(*a, **k)

    def stage(*a, **k):
        order.append(("stage", a[1]))
        return real_stage(*a, **k)

    monkeypatch.setattr(halted_run, "write_halt_marker", marker)
    monkeypatch.setattr(halted_record_commit, "write_halt_marker", marker)
    monkeypatch.setattr(halted_record_commit, "_record_kept", kept)
    monkeypatch.setattr(halted_run, "record_resume_stage", stage)
    _chunks(repo, {"C1": ["a.py"]}, {"C1": "DONE: C1\n"})

    call(repo, _halted(_envelope()))

    assert [o[0] for o in order] == ["marker", "kept", "stage"]
    assert order[0][1] == _envelope()
    assert order[2][1]["kept"] == KEPT_POINTER

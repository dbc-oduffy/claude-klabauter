"""pipeline_contract: closed-token regex, refusal type, frozen dataclasses."""

from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import pipeline_contract as pc


@pytest.mark.parametrize(
    "token",
    ["brief", "subject", "scratch_dir", "flags.depth", "stage.scout.output"],
)
def test_placeholder_re_yields_inner_token(token):
    assert pc.PLACEHOLDER_RE.findall("x {{" + token + "}} y") == [token]


def test_placeholder_re_strips_inner_space_and_flags_unknown():
    assert pc.PLACEHOLDER_RE.findall("{{ brief }} {{topic}}") == ["brief", "topic"]


def test_refused_str_and_reasons():
    err = pc.PipelineEmitRefused(["a", "b"])
    assert str(err) == "a\nb"
    assert err.reasons == ["a", "b"]
    assert isinstance(err, ValueError)


def test_dataclasses_are_frozen():
    fan = pc.FanOut(pc.FAN_OUT_NONE)
    objs = [
        pc.FlagSpec(("a",), None),
        fan,
        pc.Stage("s", "t", "m", "s.md", None, False, (), fan, "o"),
        pc.Manifest("p", Path("."), {}, (), {}, {}, "sha"),
        pc.PipelineInputs("b", (), "d", {}),
        pc.Schedule({}, {}),
    ]
    for obj in objs:
        field = next(iter(obj.__dataclass_fields__))
        with pytest.raises(FrozenInstanceError):
            setattr(obj, field, "x")


def test_halt_fields_default_none_and_halt_count_frozen():
    fan = pc.FanOut(pc.FAN_OUT_NONE)
    stage = pc.Stage("s", "t", "m", "s.md", None, False, (), fan, "o")
    assert stage.halts_unless_nonempty is None
    assert stage.halts_unless_count is None
    hc = pc.HaltCount(("a", "b"), "notebooks")
    with pytest.raises(FrozenInstanceError):
        hc.equals = "x"


def test_relay_clause_and_script_cap():
    assert pc.WORKFLOW_SCRIPT_CAP_BYTES == 524288
    clause = pc.PIPELINE_RELAY_CLAUSE.lower()
    assert "relayed chat" in clause and "quote" in clause
    assert "genuine instruction to stop" not in clause
    assert "never a reason to skip a step" in clause
    for name in ("PIPELINE_RELAY_CLAUSE", "WORKFLOW_SCRIPT_CAP_BYTES", "HaltCount"):
        assert name in pc.__all__


def test_pinned_constants():
    assert pc.SCHEMA_VERSION == 1
    assert pc.MANIFEST_SUFFIX == ".manifest.yaml"
    assert pc.RUN_ID_PREFIX == "pipeline-"
    assert len({pc.SCOPE_PRE, pc.SCOPE_SUBJECT, pc.SCOPE_POST}) == 3
    assert pc.FanOut(pc.FAN_OUT_OVER).source is None

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


def test_pinned_constants():
    assert (pc.SCHEMA_VERSION, pc.CHUNK_SIZE, pc.MAX_CONCURRENT_WEB_CALLERS) == (1, 5, 5)
    assert pc.MANIFEST_SUFFIX == ".manifest.yaml"
    assert pc.RUN_ID_PREFIX == "pipeline-"
    assert len({pc.SCOPE_PRE, pc.SCOPE_SUBJECT, pc.SCOPE_POST}) == 3
    assert pc.FanOut(pc.FAN_OUT_OVER).source is None

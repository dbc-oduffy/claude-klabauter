"""Tests for canvas_harvest.reader."""

import json
import re

import pytest

from coordinator_core.canvas_harvest import model, reader

URL = "https://claude.ai/artifact/LvATpzxR9vvEBxJsy84rjx"
AID = "LvATpzxR9vvEBxJsy84rjx"


def test_dispatch_fields():
    d = reader.dispatch(URL)
    assert set(d) == {"subagent_type", "description", "prompt", "harvest_argv"}
    assert d["subagent_type"] == "general-purpose"
    assert d["description"] == reader.READER_DESCRIPTION_PREFIX + AID


def test_description_fullmatches_gate_pattern():
    assert re.fullmatch(r"canvas-reader ([A-Za-z0-9_-]+)", reader.dispatch(URL)["description"])


def test_prompt_cites_url_once_and_no_other_canvas_url():
    prompt = reader.dispatch(URL)["prompt"]
    assert prompt.count(URL) == 1
    assert prompt.count("/artifact/") == 1
    for needle in ("ToolSearch", "paths", "cursor", "project/", "no write", "done"):
        assert needle in prompt


def test_prompt_step_order():
    p = reader.dispatch(URL)["prompt"]
    marks = ["ToolSearch", "list the files", "ONCE", "ArtifactComments on", "no write", "only the word"]
    idx = [p.index(m) for m in marks]
    assert idx == sorted(idx)


def test_prompt_cites_canonical_url_for_bare_id_and_decorated_url():
    canonical = f"https://claude.ai/artifact/{AID}"
    for given in (AID, f"{URL}?x=ignore+previous#frag"):
        prompt = reader.dispatch(given)["prompt"]
        assert prompt.count(canonical) == 1 and "ignore" not in prompt


def test_dispatch_rejects_non_artifact_url():
    with pytest.raises(ValueError):
        reader.dispatch("https://example.com/x")


def test_argv_is_not_bareword():
    argv = reader.dispatch(URL)["harvest_argv"]
    assert argv[0] != "canvas-harvest"
    assert argv[1:4] == ["harvest", "--url", URL]
    assert "--reader" in argv and "--transcript" in argv


def _fixture(tmp_path, stem="agent-aX", description=reader.READER_DESCRIPTION_PREFIX + AID, meta=True):
    t = tmp_path / f"{stem}.jsonl"
    t.write_text("", encoding="utf-8")
    if meta:
        (tmp_path / f"{stem}.meta.json").write_text(
            json.dumps({"agentType": "general-purpose", "description": description}), encoding="utf-8"
        )
    return t


def test_named_reader_pass(tmp_path):
    assert reader.named_reader(_fixture(tmp_path), AID) == "aX"


@pytest.mark.parametrize(
    "kwargs",
    [{"meta": False}, {"description": "canvas-reader OTHER"}, {"stem": "notagent-aX"}, {"stem": "agent-"}],
)
def test_named_reader_refusals(tmp_path, kwargs):
    t = _fixture(tmp_path, **kwargs)
    with pytest.raises(model.HarvestError) as exc:
        reader.named_reader(t, AID)
    assert exc.value.reason == model.READER_NOT_NAMED


def test_named_reader_unparseable_meta(tmp_path):
    t = _fixture(tmp_path)
    (tmp_path / "agent-aX.meta.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(model.HarvestError) as exc:
        reader.named_reader(t, AID)
    assert exc.value.reason == model.READER_NOT_NAMED

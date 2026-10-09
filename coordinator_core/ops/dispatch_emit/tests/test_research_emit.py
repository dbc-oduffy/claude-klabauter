"""research_emit: segments from a shape result, the ask file, slugged scout questions, the deep roster."""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.ops import research_shape
from coordinator_core.ops.dispatch_emit import research_emit as re_
from coordinator_core.ops.dispatch_emit.pipeline_contract import PipelineEmitRefused
from coordinator_core.ops.dispatch_emit.pipeline_manifest import load_manifest, validate

CONTENT = Path(__file__).parent / "fixtures" / "pipeline_research"
SCRATCH = "scratch/run-1"
BRIEF = f"{SCRATCH}/ask.md"


def _segments(research, **kw):
    shape = research_shape.shape(research)
    return shape, re_.segments_for(shape, brief_rel=BRIEF, scratch_rel=SCRATCH, **kw)


def _validate_all(segments):
    for pipeline, inputs in segments:
        validate(load_manifest(CONTENT, pipeline), inputs)


def test_bare_ask_writes_verbatim_and_yields_one_scouts_segment(tmp_path):
    ask = "What is X?\nWith detail."
    rel = re_.write_ask(tmp_path, SCRATCH, ask)
    assert rel == BRIEF
    assert (tmp_path / rel).read_bytes() == ask.encode()
    _, segs = _segments({"value_class": "scouts"})
    assert [p for p, _ in segs] == ["scouts"]
    inputs = segs[0][1]
    assert inputs.brief == rel
    assert len(inputs.lists["questions"]) == 1
    _validate_all(segs)


def test_two_questions_give_two_entries_and_headed_sections(tmp_path):
    qs = ["Why is the sky blue?", "How/why do tides work?"]
    rel = re_.write_ask(tmp_path, SCRATCH, "Topic: nature", qs)
    text = (tmp_path / rel).read_text(encoding="utf-8")
    assert "## why-is-the-sky-blue\n\nWhy is the sky blue?" in text
    assert "## how-why-do-tides-work\n\nHow/why do tides work?" in text
    _, segs = _segments({"value_class": "scouts"}, questions=qs)
    assert segs[0][1].lists["questions"] == ("why-is-the-sky-blue", "how-why-do-tides-work")
    _validate_all(segs)


def test_three_questions_are_refused(tmp_path):
    with pytest.raises(PipelineEmitRefused):
        re_.write_ask(tmp_path, SCRATCH, "t", ["a", "b", "c"])
    with pytest.raises(PipelineEmitRefused):
        _segments({"value_class": "scouts"}, questions=["a", "b", "c"])


def test_empty_duplicate_and_unsluggable_questions_are_refused(tmp_path):
    for bad in (["?!"], ["Same?", "same"], [""]):
        with pytest.raises(PipelineEmitRefused):
            re_.scout_slugs(bad)
    with pytest.raises(PipelineEmitRefused):
        re_.write_ask(tmp_path, SCRATCH, "  ")


def test_slug_is_filename_safe_and_capped():
    (slug,) = re_.scout_slugs(["What/why? " + "x" * 100])
    assert len(slug) <= re_.MAX_SLUG_LEN
    assert set(slug) <= set("abcdefghijklmnopqrstuvwxyz0123456789-")
    assert not slug.endswith("-") and not slug.startswith("-")


def test_notebooklm_and_web_corpus_yields_preflight_web_notebooklm_all_valid():
    shape, segs = _segments({"value_class": "corpus", "sources": ["web", "notebooklm"]})
    assert shape["pipelines"] == ["nlm-preflight", "web", "notebooklm"]
    assert [p for p, _ in segs] == ["nlm-preflight", "web", "notebooklm"]
    assert all(i.brief == BRIEF and i.scratch_dir == SCRATCH for _, i in segs)
    _validate_all(segs)
    assert load_manifest(CONTENT, "web").stages[0].produces_brief
    assert load_manifest(CONTENT, "nlm-preflight").stages[0].halts_unless == "ready"


def test_deep_yields_the_roster_roles_plus_a_specialist_per_source():
    _, segs = _segments({"value_class": "deep"}, sources=["web", "repo", "structured"])
    assert [p for p, _ in segs] == ["unblock"]
    roster = segs[0][1].lists["roster"]
    assert roster == (
        {"slug": "diagnose", "agent_type": "general-purpose"},
        {"slug": "decompose", "agent_type": "general-purpose"},
        {"slug": "challenge", "agent_type": "general-purpose"},
        {"slug": "web", "agent_type": "coordinator:research-specialist"},
        {"slug": "repo", "agent_type": "coordinator:repo-specialist"},
    )
    _validate_all(segs)


def test_deep_with_no_sources_defaults_to_the_web_specialist():
    _, segs = _segments({"value_class": "deep"})
    assert [m["slug"] for m in segs[0][1].lists["roster"]][-1] == "web"


def test_deepest_depth_sets_the_repo_flag_only():
    shape, segs = _segments({"value_class": "corpus", "sources": ["web", "repo"], "depth": "deepest"})
    assert shape["pipelines"] == ["web", "repo"]
    assert dict(segs[0][1].flags) == {}
    assert dict(segs[1][1].flags) == {"deepest": "true"}

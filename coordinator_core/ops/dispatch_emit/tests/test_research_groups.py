"""research_groups: group order, subset priorities, refusals, slug cut, run line."""

from __future__ import annotations

from coordinator_core.ops import _research_contract as rc
from coordinator_core.ops.dispatch_emit import research_emit
from coordinator_core.ops.dispatch_emit import research_groups as rg

SHAPE = {"pipelines": [rc.PREFLIGHT_PIPELINE, "notebooklm"]}


def _research(*refs, extra=()):
    return {"targets": [{"source": "notebooklm", "ref": r} for r in refs] + list(extra)}


def test_names_never_bare_groups():
    names = (rg.PER_GROUP_PARAM, rg.GROUPS_PARAM, rg.GROUPS_REPLY_KEY, rg.GROUP_RUNS_CLOSE_PARAM)
    assert "groups" not in names
    assert rg.GROUP_RUNS_CLOSE_PARAM == "group_runs"


def test_order_and_priority_skip_non_notebooklm():
    research = _research("nb-A", "nb-B", extra=[{"source": "web", "ref": "http://x"}])
    research["targets"].insert(1, {"source": "web", "ref": "http://y"})
    specs = rg.groups_for(research, "topic")
    assert [s.ref for s in specs] == ["nb-A", "nb-B"]
    assert [s.priority for s in specs] == [1, 2]
    assert specs[0].group == "nb-a"
    assert specs[0].topic_slug == "topic-nb-a"


def test_subset_keeps_full_list_priority():
    specs = rg.groups_for(_research("a", "b", "c"), "t", selected=["c", "b"])
    assert [(s.ref, s.priority) for s in specs] == [("b", 2), ("c", 3)]


def test_topic_slug_cut_keeps_group():
    uuid = "7f3c9a10-1b2d-4e5f-8a9b-0c1d2e3f4a5b"
    long_topic = "metahuman-video-value-extraction-and-more-words-here"
    specs = rg.groups_for(_research(uuid, "other"), long_topic)
    for s in specs:
        assert len(s.topic_slug) <= research_emit.MAX_SLUG_LEN
        assert s.topic_slug.endswith("-" + s.group)
    assert specs[0].topic_slug != specs[1].topic_slug


def test_clean_shape_has_no_refusals():
    assert rg.per_group_refusals(SHAPE, _research("a", "b")) == []


def test_refuses_fewer_than_two():
    assert any("at least two" in r for r in rg.per_group_refusals(SHAPE, _research("a")))


def test_refuses_other_pipelines():
    shape = {"pipelines": [rc.PREFLIGHT_PIPELINE, "notebooklm", rc.SCOUTS_PIPELINE]}
    out = rg.per_group_refusals(shape, _research("a", "b"))
    assert any(rc.SCOUTS_PIPELINE in r for r in out)


def test_refuses_unknown_selected_ref():
    out = rg.per_group_refusals(SHAPE, _research("a", "b"), ["zzz"])
    assert any("zzz" in r for r in out)


def test_refuses_alike_slugs_and_reports_together():
    out = rg.per_group_refusals(SHAPE, _research("Nb A", "nb-a"), ["q"])
    assert any("slug alike" in r for r in out)
    assert any("q" in r for r in out)


def test_run_line_shape():
    line = rg.run_line({"priority": 2, "group": "nb-b", "run_id": "rid-1", "path": "x/rid-1.workflow.mjs"})
    assert line == "[2] nb-b run_id=rid-1 script=x/rid-1.workflow.mjs"

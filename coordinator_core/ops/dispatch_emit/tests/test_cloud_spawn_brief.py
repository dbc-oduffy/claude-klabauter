"""
Tests for the cloud-spawn brief emitter (`cloud_spawn_brief`, `dispatch.emit` cloud_spawn route).

The gate: a template or question naming a trigger, artifact, publish, push, branch, commit,
scheduling or repo-attach verb is refused, so a brief can never drift into the shape the
auto-mode classifier denies whole.
"""

from __future__ import annotations

import pytest

from coordinator_core.ops.dispatch_emit import cloud_spawn_brief as csb
from coordinator_core.ops.dispatch_emit.op import _dispatch_emit

_ARGS = dict(
    source_repo="acme/widgets",
    parent_session_id="session_01ABC",
    channel_pr=7,
    question="Which MCP tools does a child session see?",
)


@pytest.mark.parametrize("kind", csb.KINDS)
def test_template_carries_reads_and_exactly_one_report_write(kind):
    block = csb.build_cloud_spawn(kind, **_ARGS)
    prompt = block["prompt"]
    assert csb.count_report_writes(prompt) == 1
    assert "pull request #7 of acme/widgets" in prompt
    assert "session_01ABC" in prompt
    assert "UNTESTED" in prompt


@pytest.mark.parametrize("kind", csb.KINDS)
def test_template_names_no_side_channel_verb(kind):
    template = csb._TEMPLATES[kind]
    assert csb._FORBIDDEN_RE.search(template) is None
    csb.refuse_side_channel_words(csb.build_cloud_spawn(kind, **_ARGS)["prompt"])


@pytest.mark.parametrize("word", csb.FORBIDDEN_STEMS)
def test_gate_refuses_every_forbidden_stem(word):
    with pytest.raises(csb.CloudSpawnBriefError):
        csb.refuse_side_channel_words(f"Also {word} something.")


@pytest.mark.parametrize(
    "question",
    [
        "Create triggers aimed at the parent",
        "Publish an artifact with the result",
        "Push a speculative branch",
        "Call add_repo for example-retrieval-repo",
    ],
)
def test_question_asking_for_a_side_channel_is_refused(question):
    with pytest.raises(csb.CloudSpawnBriefError):
        csb.build_cloud_spawn("probe", **{**_ARGS, "question": question})


def test_a_template_edit_that_adds_a_side_channel_is_refused(monkeypatch):
    monkeypatch.setitem(csb._TEMPLATES, "probe", csb._PROBE_TEMPLATE + "Create a trigger.\n")
    with pytest.raises(csb.CloudSpawnBriefError):
        csb.build_cloud_spawn("probe", **_ARGS)


def test_a_template_with_a_second_write_is_refused(monkeypatch):
    monkeypatch.setitem(
        csb._TEMPLATES, "worker", csb._WORKER_TEMPLATE + "{marker} a second comment.\n"
    )
    with pytest.raises(csb.CloudSpawnBriefError, match="exactly one report write"):
        csb.build_cloud_spawn("worker", **_ARGS)


def test_block_shape_and_no_environment_id():
    block = csb.build_cloud_spawn("probe", **_ARGS)
    assert set(block) == {"source_url", "prompt", "title"}
    assert block["source_url"] == "https://github.com/acme/widgets"


@pytest.mark.parametrize(
    "repo", ["acme/widgets", "https://github.com/acme/widgets", "https://github.com/acme/widgets.git"]
)
def test_source_repo_spellings_normalise(repo):
    block = csb.build_cloud_spawn("probe", **{**_ARGS, "source_repo": repo})
    assert block["source_url"] == "https://github.com/acme/widgets"


@pytest.mark.parametrize(
    "pr", [7, "7", "#7", "https://github.com/acme/widgets/pull/7"]
)
def test_channel_pr_spellings_normalise(pr):
    assert "#7 of" in csb.build_cloud_spawn("probe", **{**_ARGS, "channel_pr": pr})["prompt"]


@pytest.mark.parametrize(
    "override",
    [
        {"kind": "auditor"},
        {"source_repo": "not a repo"},
        {"parent_session_id": "session x; rm"},
        {"channel_pr": "abc"},
        {"channel_pr": True},
        {"channel_pr": 0},
        {"question": "   "},
        {"question": "x" * 401},
    ],
)
def test_malformed_input_is_refused(override):
    args = {"kind": "probe", **_ARGS, **override}
    with pytest.raises(csb.CloudSpawnBriefError):
        csb.build_cloud_spawn(**args)


def test_op_route_returns_the_create_session_block():
    reply = _dispatch_emit({"cloud_spawn": {"kind": "worker", **_ARGS}})
    assert reply["ok"] is True
    assert reply["create_session"] == csb.build_cloud_spawn("worker", **_ARGS)


@pytest.mark.parametrize("other", [{"plan_path": "p.md"}, {"queue": ["q"]}, {"output_path": "o.mjs"}])
def test_op_route_refuses_combination_with_another_route(other):
    with pytest.raises(ValueError, match="cloud_spawn alone"):
        _dispatch_emit({"cloud_spawn": {"kind": "probe", **_ARGS}, **other})


def test_op_route_refuses_a_non_object():
    with pytest.raises(ValueError, match="must be an object"):
        _dispatch_emit({"cloud_spawn": "probe"})

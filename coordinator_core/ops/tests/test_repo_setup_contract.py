"""Tests for the repo.setup answers contract and verdict schema."""

import json

from coordinator_core.ops.repo_setup_contract import (
    VERDICT_SCHEMA,
    Verdict,
    make_item,
    validate_answers,
)


def test_good_answers_pass_through_dict_and_json_string():
    payload = {
        "project_name": "demo",
        "project_type": "general",
        "project_subtypes": [],
        "cross_platform": True,
        "workstreams": ["core"],
        "register_repo": False,
    }
    assert validate_answers(payload) == (payload, [])
    assert validate_answers(json.dumps(payload)) == (payload, [])


def test_none_means_no_answers():
    assert validate_answers(None) == ({}, [])


def test_aliases_fold_onto_canonical_keys():
    answers, items = validate_answers(
        {"name": "demo", "p2.ask-project-type": "web-dev", "description": "a goal"}
    )
    assert items == []
    assert answers == {"project_name": "demo", "project_type": "web-dev", "description": "a goal"}


def test_unknown_key_refused_naming_it_and_writes_no_answers():
    answers, items = validate_answers({"project_name": "demo", "bogus": 1})
    assert answers == {}
    assert items == [{"step": "answers", "key": "bogus", "detail": "unknown answers key"}]


def test_mistyped_keys_each_get_an_item():
    answers, items = validate_answers(
        {"project_name": 3, "project_type": "unreal", "cross_platform": "yes", "workstreams": "x"}
    )
    assert answers == {}
    assert sorted(i["key"] for i in items) == [
        "cross_platform",
        "project_name",
        "project_type",
        "workstreams",
    ]
    assert all(i["step"] == "answers" for i in items)


def test_blank_name_refused():
    assert validate_answers({"project_name": "  "})[1][0]["key"] == "project_name"


def test_non_json_string_and_non_object_name_the_payload():
    for bad in ("{not json", "[1, 2]", "42", 7):
        answers, items = validate_answers(bad)
        assert answers == {}
        assert [i["key"] for i in items] == ["$"]


def test_conflicting_spellings_refused():
    _, items = validate_answers({"name": "a", "project_name": "b"})
    assert len(items) == 1 and items[0]["step"] == "answers"


def test_passthrough_step_id_accepted():
    answers, items = validate_answers({"p3f.untrack-scratch-offer": "skip"})
    assert items == [] and answers == {"p3f.untrack-scratch-offer": "skip"}


def test_verdict_serialises_four_lists_and_matches_schema():
    v = Verdict(
        created=[make_item("claude-md", "written", path="CLAUDE.md")],
        needs_attention=[make_item("answers", "unknown answers key", key="bogus")],
    )
    d = v.to_dict()
    assert set(d) == set(VERDICT_SCHEMA["required"])
    assert d["already_existed"] == [] and d["open"] == []
    assert d["created"][0] == {"step": "claude-md", "detail": "written", "path": "CLAUDE.md"}
    json.dumps(d)


def test_schema_is_json_serialisable_and_closed():
    json.dumps(VERDICT_SCHEMA)
    assert VERDICT_SCHEMA["additionalProperties"] is False
    for name in VERDICT_SCHEMA["required"]:
        assert VERDICT_SCHEMA["properties"][name]["items"]["required"] == ["step", "detail"]

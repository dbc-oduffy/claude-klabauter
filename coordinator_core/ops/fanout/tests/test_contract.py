"""Pins the fanout-manifest.v1 contract: schema, roster, tag builders, transport check-in line."""

from __future__ import annotations

import copy
from pathlib import Path

import pytest

from coordinator_core.ops.fanout import contract, transport

FIXTURE = Path(__file__).parent / "fixtures" / "manifest-minimal.yaml"


def _manifest() -> dict:
    return contract.parse_manifest_yaml(FIXTURE.read_text(encoding="utf-8"))


def test_minimal_fixture_validates_from_object_and_yaml_string():
    assert contract.validate_manifest(_manifest())["job_id"] == "demo-job"
    assert contract.validate_manifest(FIXTURE.read_text(encoding="utf-8"))["job_id"] == "demo-job"


def test_plan_permission_mode_refused():
    m = _manifest()
    m["permission_mode"] = "plan"
    with pytest.raises(ValueError, match=r"\$\.permission_mode"):
        contract.validate_manifest(m)


def test_duplicate_worker_id_refused():
    m = _manifest()
    m["workers"].append(copy.deepcopy(m["workers"][0]))
    with pytest.raises(ValueError, match=r"workers\[1\]\.id"):
        contract.validate_manifest(m)


def test_missing_parent_channel_pr_url_refused():
    m = _manifest()
    del m["parent"]["channel_pr_url"]
    with pytest.raises(ValueError, match="channel_pr_url"):
        contract.validate_manifest(m)


def test_comms_repo_required_for_pr_channel():
    m = _manifest()
    del m["comms_repo"]
    with pytest.raises(ValueError, match="comms_repo"):
        contract.validate_manifest(m)


def test_focus_required_and_repos_shaped():
    m = _manifest()
    m["workers"][0]["focus"] = "not-a-key"
    with pytest.raises(ValueError, match="focus"):
        contract.validate_manifest(m)
    del m["workers"][0]["focus"]
    with pytest.raises(ValueError, match="focus"):
        contract.validate_manifest(m)


def test_worker_id_slug_pattern():
    m = _manifest()
    m["workers"][0]["id"] = "Bad:Id"
    with pytest.raises(ValueError, match=r"workers\[0\]\.id"):
        contract.validate_manifest(m)


def test_invalid_yaml_and_non_object_refused():
    with pytest.raises(ValueError):
        contract.validate_manifest("a: [unclosed")
    with pytest.raises(ValueError):
        contract.validate_manifest("- just\n- a list\n")


def test_roster_extends_and_never_shrinks():
    m = _manifest()
    worker = m["workers"][0]
    assert tuple(contract.effective_roster(m, worker)) == contract.STANDARD_CHILD_ROSTER
    m["repos"] = ["extra-a", "project-rag"]
    worker["repos"] = ["extra-b", "extra-a"]
    roster = contract.effective_roster(m, worker)
    assert roster == [*contract.STANDARD_CHILD_ROSTER, "extra-a", "extra-b"]
    props = contract.load_schema()["properties"]
    assert not [k for k in props if "exclude" in k or "remove" in k or "drop" in k]
    assert "additionalProperties" in contract.load_schema()
    assert contract.load_schema()["additionalProperties"] is False


def test_tag_builders_deterministic_and_ordered():
    m = _manifest()
    m["workers"][0]["tags"] = ["custom", "fanout:demo-job"]
    tags = contract.worker_tags(m, m["workers"][0])
    assert tags == contract.worker_tags(m, m["workers"][0])
    assert tags == [
        "fanout:demo-job",
        "fanout-worker:worker-a",
        "fanout-parent:parent-session-1",
        "fanout-focus:repos.repo_a",
        "custom",
    ]
    assert contract.session_title("j", "w") == contract.idempotency_key("j", "w") == "j/w"


def test_create_session_arg_keys_match_typeddict():
    assert set(contract.CREATE_SESSION_ARG_KEYS) == set(contract.CreateSessionArgs.__annotations__)


def test_parse_checkin_accepts_exact_line_and_rejects_near_misses():
    t = transport.for_channel("pr")
    assert t.parse_checkin("[session abc-1] channel: https://example.invalid/c/pull/2") == (
        "abc-1",
        "https://example.invalid/c/pull/2",
    )
    assert t.parse_checkin("ok\n[session abc-1] channel: https://example.invalid/c/pull/2\n") is not None
    assert t.parse_checkin(t.checkin_line("s9", "https://example.invalid/x")) == ("s9", "https://example.invalid/x")
    for bad in (
        "[session abc-1] channel:https://example.invalid/c",
        "[session ] channel: https://example.invalid/c",
        "session abc-1 channel: https://example.invalid/c",
        "[session abc-1] channel: not-a-url",
        "[session abc-1] channel: https://example.invalid/c trailing",
        "",
    ):
        assert t.parse_checkin(bad) is None


def test_broadcast_target_requires_checkin():
    t = transport.for_channel("pr")
    assert t.broadcast_target({"checked_in": True, "channel_url": "https://example.invalid/u"}) == "https://example.invalid/u"
    assert t.broadcast_target({"checked_in": False, "channel_url": "https://example.invalid/u"}) is None


def test_child_prompt_block_names_parent_and_checkin_shape():
    m = _manifest()
    block = transport.for_channel("pr").child_prompt_block(m, m["workers"][0])
    assert m["parent"]["channel_pr_url"] in block
    assert m["comms_repo"] in block
    assert "[session <your-session-id>] channel: <your-channel-pr-url>" in block


def test_unknown_channel_refused():
    with pytest.raises(ValueError):
        transport.for_channel("slack")

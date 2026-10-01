"""Pins fanout.compose: determinism, worker order, tags, roster, and the prompt's channel block."""

from __future__ import annotations

import copy
import json
from pathlib import Path

from coordinator_core.ops.fanout import compose, contract, transport

FIXTURE = Path(__file__).parent / "fixtures" / "manifest-minimal.yaml"


def _manifest() -> dict:
    return contract.parse_manifest_yaml(FIXTURE.read_text(encoding="utf-8"))


def _two_workers() -> dict:
    m = _manifest()
    second = copy.deepcopy(m["workers"][0])
    second["id"] = "worker-b"
    second["focus"] = "repos.repo_b"
    m["workers"].append(second)
    return m


def test_byte_identical_across_calls():
    m = _two_workers()
    a = json.dumps(compose.compose(copy.deepcopy(m)), sort_keys=False)
    b = json.dumps(compose.compose(copy.deepcopy(m)), sort_keys=False)
    assert a == b


def test_worker_order_and_arg_keys():
    result = compose.compose(_two_workers())
    assert result["job_id"] == "demo-job"
    assert [a["worker_id"] for a in result["actions"]] == ["worker-a", "worker-b"]
    for action in result["actions"]:
        assert tuple(action["create_session"]) == contract.CREATE_SESSION_ARG_KEYS


def test_accepts_yaml_string():
    result = compose.compose(FIXTURE.read_text(encoding="utf-8"))
    assert len(result["actions"]) == 1


def test_every_tag_present_and_deduped():
    m = _manifest()
    m["workers"][0]["tags"] = ["extra", "fanout:demo-job", "extra"]
    args = compose.compose(m)["actions"][0]["create_session"]
    assert args["tags"] == [
        "fanout:demo-job",
        "fanout-worker:worker-a",
        "fanout-parent:parent-session-1",
        "fanout-focus:repos.repo_a",
        "extra",
    ]


def test_roster_has_standard_repos_and_extensions():
    m = _manifest()
    prompt = compose.compose(m)["actions"][0]["create_session"]["prompt"]
    for name in contract.STANDARD_CHILD_ROSTER:
        assert name in prompt
    m["repos"] = ["job-extra"]
    m["workers"][0]["repos"] = ["worker-extra"]
    prompt = compose.compose(m)["actions"][0]["create_session"]["prompt"]
    roster_line = next(ln for ln in prompt.splitlines() if ln.startswith("- repos:"))
    assert roster_line == "- repos: " + ", ".join(
        [*contract.STANDARD_CHILD_ROSTER, "job-extra", "worker-extra"]
    )


def test_prompt_carries_parent_channel_and_checkin_round_trip():
    m = _manifest()
    prompt = compose.compose(m)["actions"][0]["create_session"]["prompt"]
    assert m["parent"]["channel_pr_url"] in prompt
    assert "demo-job" in prompt and "worker-a" in prompt and "repos.repo_a" in prompt
    assert prompt.rstrip().endswith("Do the thing.")
    pr = transport.for_channel("pr")
    sample = pr.checkin_line("sess-1", "https://example.invalid/comms/pull/2")
    template_line = next(ln for ln in prompt.splitlines() if "<your-session-id>" in ln).strip()
    assert template_line == pr.checkin_line("<your-session-id>", "<your-channel-pr-url>")
    assert pr.parse_checkin(sample) == ("sess-1", "https://example.invalid/comms/pull/2")


def test_action_identity_and_shared_params():
    action = compose.compose(_manifest())["actions"][0]
    assert action["idempotency_key"] == contract.idempotency_key("demo-job", "worker-a")
    cs = action["create_session"]
    assert cs["title"] == contract.session_title("demo-job", "worker-a")
    assert cs["source_url"] == "https://example.invalid/example-owner/repo-a"
    assert (cs["environment_id"], cs["model"], cs["permission_mode"]) == (
        "env-example",
        "example-model",
        "acceptEdits",
    )

"""session.poll_share classifier, windows and handler over the committed synthetic fixture."""

import asyncio
from pathlib import Path

import pytest

from coordinator_core.ops.session import poll_share

FIXTURE = Path(__file__).parent / "fixtures" / "poll_share_synthetic.jsonl"


@pytest.fixture(scope="module")
def polling_transcript(tmp_path_factory):
    # The committed fixture spells its polls as ListAgents, which POLL_TOOLS no longer counts.
    text = FIXTURE.read_text(encoding="utf-8").replace('"name":"ListAgents"', '"name":"TaskList"')
    assert '"name":"TaskList"' in text, "fixture spelling changed; the ListAgents rewrite no longer applies"
    path = tmp_path_factory.mktemp("poll_share") / "poll_share_synthetic.jsonl"
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture(scope="module")
def result(polling_transcript):
    return poll_share.compute(str(polling_transcript), "S1")


def test_listagents_is_not_a_poll():
    assert not poll_share._is_poll_call({"name": "ListAgents", "input": {}})


def test_window_with_three_poll_turns(result):
    w = result["windows"][0]
    assert (w["em_turns"], w["poll_turns"], w["keepalive_ticks"], w["closed"]) == (4, 3, 0, True)
    assert w["poll_share"] == 0.75


def test_keepalive_counted_separately_not_excluded(result):
    w = result["windows"][1]
    assert (w["em_turns"], w["poll_turns"], w["keepalive_ticks"]) == (3, 1, 1)


def test_window_with_zero_polls(result):
    w = result["windows"][2]
    assert (w["em_turns"], w["poll_turns"], w["poll_share"]) == (1, 0, 0.0)


def test_totals_and_shape(result, polling_transcript):
    assert (result["em_turns"], result["poll_turns"], result["keepalive_ticks"]) == (8, 4, 1)
    assert result["poll_share"] == 0.5
    assert result["classifier_version"] == poll_share.CLASSIFIER_VERSION
    assert result["session_id"] == "S1"
    assert str(polling_transcript) not in repr(result)


def test_poll_tools_is_imported_not_retyped():
    from coordinator_core.hooks.flag_em_poll_in_flight import POLL_TOOLS

    assert poll_share.POLL_TOOLS is POLL_TOOLS


def test_monitor_with_action_and_non_sleep_bash_are_not_polls():
    assert not poll_share._is_poll_call({"name": "Monitor", "input": {"action": "stop"}})
    assert not poll_share._is_poll_call({"name": "Bash", "input": {"command": "sleep 5 && rm x"}})
    assert poll_share._is_poll_call({"name": "Bash", "input": {"command": "sleep 5"}})


def test_handler_roundtrip_and_missing_file(tmp_path):
    out = asyncio.run(poll_share._handler({"transcript_path": str(FIXTURE), "session_id": "S1"}))
    assert out["em_turns"] == 8
    with pytest.raises(ValueError):
        asyncio.run(poll_share._handler({"transcript_path": str(tmp_path / "nope.jsonl"), "session_id": "S1"}))

import pytest
from coordinator_core.codex_status.model import empty, ingest, summary, observe_usage, thread


def record(model="gpt-6.1-sol", usage=None):
    return {"type": "coordinator_usage", "payload": {"session_id": "child", "request_id": "request",
        "parent_session_id": "root", "model": model, "usage_format": "coordinator-canonical-v1",
        "usage": usage if usage is not None else {"input_tokens": 10, "output_tokens": 20, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}}}


def test_missing_or_invalid_usage_can_be_corrected_without_consuming_identity():
    state = empty("root")
    ingest(state, record(usage={}), {})
    assert summary(state)["priced_events"] == 0
    with pytest.raises(ValueError):
        ingest(state, record(usage={"input_tokens": -1, "output_tokens": 20, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}), {})
    ingest(state, record(), {})
    assert summary(state)["priced_events"] == 1
    assert summary(state)["usd"] > 0


def test_unknown_rate_replay_repairs_once_without_double_unpriced_count():
    state = empty("root")
    for _ in range(3):
        ingest(state, record(model="unknown"), {})
    assert summary(state)["unpriced_events"] == 1
    for _ in range(3):
        ingest(state, record(), {})
    assert summary(state)["unpriced_events"] == 0
    assert summary(state)["priced_events"] == 1
    assert state["threads"]["child"]["canonical_events"] == 1


def test_output_only_historical_gap_and_missing_native_counters():
    state = empty("root")
    thread(state, "root")["model"] = "gpt-6.1-sol"
    observe_usage(state, "root", "turn", {"total": {"input_tokens": 10, "output_tokens": 200, "cached_input_tokens": 0},
        "last": {"input_tokens": 10, "output_tokens": 20, "cached_input_tokens": 0}})
    assert any("predates" in warning for warning in summary(state)["warnings"])
    with pytest.raises(ValueError):
        observe_usage(state, "root", "missing", {"total": {}, "last": {}})


def test_unknown_parent_spend_is_excluded_with_explicit_warning():
    state = empty("root")
    event = record()
    event["payload"].pop("parent_session_id")
    ingest(state, event, {})
    value = summary(state)
    assert value["usd"] == 0
    assert any("parent ownership" in warning for warning in value["warnings"])


def test_missing_cache_counters_are_unknown_not_zero():
    state = empty("root")
    event = record()
    event["payload"]["usage"].pop("cache_read_input_tokens")
    ingest(state, event, {})
    assert summary(state)["priced_events"] == 0
    assert summary(state)["warnings"]
    with pytest.raises(ValueError):
        observe_usage(state, "root", "native", {"total": {"input_tokens": 10, "output_tokens": 20},
            "last": {"input_tokens": 10, "output_tokens": 20}})
    ingest(state, record(), {})
    assert summary(state)["priced_events"] == 1

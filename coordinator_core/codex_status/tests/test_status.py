from __future__ import annotations

import json
from pathlib import Path

import pytest

from coordinator_core.codex_status.model import empty, ingest, summary
from coordinator_core.codex_status.collect import advance, discover
from coordinator_core.codex_status.render import render, registered_alias
from coordinator_core.usage.price_table import estimate_usd, RATE_PROVENANCE

ROOT = "01a1b63c-040a-76f2-a9b9-929d9a0d0098"
CHILD = "01a1b63c-040a-76f2-a9b9-929d9a0d0099"


def metadata(sid, parent=None):
    return {"type": "session_meta", "payload": {"id": sid, "parent_thread_id": parent}}


def context(turn, model="gpt-6.1-sol"):
    return {"type": "turn_context", "payload": {"turn_id": turn, "model": model, "effort": "medium"}}


def usage(inp, cached, output, turn_total=None):
    count = {"input_tokens": inp, "cached_input_tokens": cached, "output_tokens": output, "total_tokens": inp + output}
    return {"type": "event_msg", "payload": {"type": "token_count", "info": {
        "last_token_usage": count, "total_token_usage": turn_total or count, "model_context_window": 475000}}}


def write(path, rows):
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def test_copied_fork_history_is_charged_once_and_explicit_children_count(tmp_path):
    parent = tmp_path / "root.jsonl"
    child = tmp_path / "child.jsonl"
    prefix = [metadata(ROOT), context("root-turn"), usage(1000, 500, 100)]
    write(parent, prefix)
    write(child, [metadata(CHILD, ROOT), *prefix, context("child-turn"), usage(200, 100, 20)])
    state = empty(ROOT)
    advance(state, [parent, child])
    value = summary(state)
    assert value["usd"] == pytest.approx(.00246)
    assert value["threads"] == 2
    assert value["priced_events"] == 2
    advance(state, [parent, child])
    assert summary(state)["usd"] == pytest.approx(.00246)


def test_native_and_rollout_notifications_deduplicate_without_double_reasoning():
    state, source = empty(ROOT), {}
    for event in [metadata(ROOT), context("turn"), usage(1000, 500, 100)]:
        ingest(state, event, source)
    ingest(state, {"method": "thread/tokenUsage/updated", "params": {"threadId": ROOT, "turnId": "turn", "tokenUsage": {
        "last": {"inputTokens": 1000, "cachedInputTokens": 500, "outputTokens": 100, "reasoningOutputTokens": 80, "totalTokens": 1100},
        "total": {"inputTokens": 1000, "cachedInputTokens": 500, "outputTokens": 100, "totalTokens": 1100}, "modelContextWindow": 475000}}}, {})
    assert summary(state)["priced_events"] == 1
    assert summary(state)["usd"] == pytest.approx(.00205)


def test_compaction_preserves_lifetime_spend_and_clears_stale_context():
    state, source = empty(ROOT), {}
    for event in [metadata(ROOT), context("turn"), usage(300000, 100000, 10000)]:
        ingest(state, event, source)
    value = summary(state)
    assert value["usd"] == pytest.approx(.97)
    assert value["remaining_percent"] == pytest.approx(100 * (1 - 310000 / 475000))
    ingest(state, {"type": "compacted", "payload": {}}, source)
    assert summary(state)["remaining_percent"] is None
    assert summary(state)["usd"] == pytest.approx(.97)


def test_workflow_canonical_usage_and_unpriced_models_remain_visible():
    state = empty(ROOT)
    record = {"type": "coordinator_usage", "payload": {"session_id": CHILD, "parent_session_id": ROOT,
        "request_id": "actual-request", "provider": "codex", "model": "gpt-6.1-sol",
        "usage_format": "coordinator-canonical-v1", "usage": {"input_tokens": 500, "cache_read_input_tokens": 500, "cache_creation_input_tokens": 0, "output_tokens": 100}}}
    ingest(state, record, {})
    ingest(state, record, {})
    assert summary(state)["usd"] == pytest.approx(.00205)
    record["payload"].update(request_id="unpriced", model="unknown-provider-model")
    ingest(state, record, {})
    value = summary(state)
    assert value["unpriced_events"] == 1
    assert "coverage incomplete" in render(value)


def test_partial_json_lines_and_bounded_progress(tmp_path):
    path = tmp_path / "root.jsonl"
    write(path, [metadata(ROOT), context("turn")])
    partial = json.dumps(usage(1000, 500, 100))
    with path.open("a") as handle:
        handle.write(partial[:20])
    state = empty(ROOT)
    advance(state, [path], byte_budget=2048)
    assert not state["backlog_complete"]
    assert summary(state)["priced_events"] == 0
    with path.open("a") as handle:
        handle.write(partial[20:] + "\n")
    advance(state, [path], byte_budget=2048)
    assert summary(state)["priced_events"] == 1
    assert state["backlog_complete"]


def test_unknown_route_and_prices_never_render_confident_zero(tmp_path, monkeypatch):
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path))
    peers = tmp_path / "unimatrix" / "peers.json"
    peers.parent.mkdir()
    peers.write_text(json.dumps({"peers": [{"name": "actual-route", "vendor": "codex", "target": ROOT}]}))
    assert registered_alias(ROOT) == "actual-route"
    assert registered_alias(CHILD) is None
    text = render(summary(empty(ROOT)))
    assert text.split(" · ")[0] == "unknown" and text.endswith("unknown") and "0.00" not in text
    assert "route?" not in text and "context?" not in text and "~$?" not in text
    assert render({"remaining_percent": 80, "alias": "a"}).count("unknown") >= 2 and "ctx" not in render({"remaining_percent": 80})
    assert "ctx" in render({"before_compact_percent": 40})


def test_discovery_joins_parent_identity_not_same_repository(tmp_path):
    write(tmp_path / "rollout-root.jsonl", [metadata(ROOT)])
    write(tmp_path / "rollout-child.jsonl", [metadata(CHILD, ROOT)])
    write(tmp_path / "rollout-neighbor.jsonl", [metadata("different-thread")])
    paths, complete = discover(tmp_path, ROOT)
    assert complete
    assert {p.name for p in paths} == {"rollout-root.jsonl", "rollout-child.jsonl"}


def test_current_first_party_prices_include_cache_long_context_and_speed():
    assert estimate_usd("gpt-6.1-sol", {"input_tokens": 1000, "cache_read_input_tokens": 1000}) == pytest.approx(.0021)
    assert estimate_usd("gpt-6.1-sol", {"input_tokens": 300000, "output_tokens": 10000}, "fast") == pytest.approx(2.7)
    assert estimate_usd("gpt-6-astra-law", {"input_tokens": 1000}) is None
    assert estimate_usd("gpt-6.1-sol", {"input_tokens": 1000}, "unknown-tier") is None
    assert RATE_PROVENANCE["gpt-6.1-sol"]["verified_at"] == "2026-11-07"
    assert RATE_PROVENANCE["gpt-6.1-sol"]["effective_from"] is None


def test_installed_snapshot_door_has_schema_and_retains_observation_time(tmp_path, monkeypatch):
    from coordinator_core.ops.codex_status import snapshot as operation
    from coordinator_core.contract.op_contract import build_contract
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path))
    path = tmp_path / "root.jsonl"
    observed = usage(1000, 500, 100)
    observed["timestamp"] = "2026-11-07T13:39:09Z"
    write(path, [metadata(ROOT), context("turn"), observed])
    result = operation({"session_id": ROOT, "rollouts": [str(path)]})
    assert result["usd"] == pytest.approx(.00205)
    assert result["backlog_complete"]
    assert result["captured_at"] == 1794058749
    assert build_contract("codex_status.snapshot").params_schema["properties"]["byte_budget"]["maximum"] == 4194304
    again = operation({"session_id": ROOT, "rollouts": [str(path)]})
    assert again["usd"] == result["usd"] and again["priced_events"] == 1


def test_rotating_source_preserves_old_spend_and_consumes_new_usage(tmp_path):
    path = tmp_path / "root.jsonl"
    write(path, [metadata(ROOT), context("first"), usage(1000, 500, 100), {"type": "response_item", "payload": "x" * 512}])
    state = empty(ROOT)
    advance(state, [path])
    write(path, [metadata(ROOT), context("second"), usage(200, 100, 20)])
    advance(state, [path])
    assert summary(state)["usd"] == pytest.approx(.00246)
    assert state["backlog_complete"]
    assert any("rotated or truncated" in message for message in state["warnings"])


def test_unknown_model_only_never_renders_zero_priced_usage():
    state, source = empty(ROOT), {}
    for event in [metadata(ROOT), context("turn", "unknown-model"), usage(1000, 500, 100)]:
        ingest(state, event, source)
    value = summary(state)
    assert value["unpriced_events"] == 1 and value["priced_events"] == 0
    assert render(value).endswith("unknown") and "~$?" not in render(value)


def test_canonical_native_overlap_needs_an_actual_request_join():
    state, source = empty(ROOT), {}
    for event in [metadata(ROOT), context("turn"), usage(1000, 500, 100)]:
        ingest(state, event, source)
    record = {"type": "coordinator_usage", "payload": {"session_id": ROOT, "request_id": "execution-id",
        "provider": "codex", "model": "gpt-6.1-sol", "usage_format": "coordinator-canonical-v1",
        "usage": {"input_tokens": 500, "cache_read_input_tokens": 500, "cache_creation_input_tokens": 0, "output_tokens": 100}}}
    ingest(state, record, {})
    assert summary(state)["usd"] == pytest.approx(.00205)  # Never add potentially overlapping recorders.
    assert any("overlap" in warning for warning in summary(state)["warnings"])
    joined = empty(ROOT)
    owner = {}
    for event in [metadata(ROOT), context("turn"), usage(1000, 500, 100)]:
        ingest(joined, event, owner)
    record["payload"].update(native_turn_id="turn", native_total_usage={"input_tokens": 1000,
        "cached_input_tokens": 500, "output_tokens": 100, "total_tokens": 1100})
    ingest(joined, record, {})
    assert summary(joined)["priced_events"] == 1
    assert not any("overlap" in warning for warning in summary(joined)["warnings"])


def test_historical_rollout_prices_its_model_without_overwriting_fresh_native_display():
    state = empty(ROOT)
    ingest(state, {"timestamp": "2026-11-07T22:00:00Z", "method": "thread/started", "params": {"thread": {
        "id": ROOT, "model": "gpt-6-astra", "reasoningEffort": "high"}}}, {})
    raw = usage(1000, 500, 100)["payload"]["info"]
    ingest(state, {"timestamp": "2026-11-07T22:01:00Z", "method": "thread/tokenUsage/updated", "params": {
        "threadId": ROOT, "turnId": "new", "tokenUsage": {**raw, "model_context_window": 100000}}}, {})
    source = {}
    old_context = context("old")
    old_context["timestamp"] = "2026-11-07T13:00:00Z"
    old_usage = usage(1000, 500, 100)
    old_usage["timestamp"] = "2026-11-07T13:01:00Z"
    for event in (metadata(ROOT), old_context, old_usage):
        ingest(state, event, source)
    value = summary(state)
    assert value["usd"] == pytest.approx(.0105 + .00205)
    assert value["model"] == "gpt-6-astra" and value["effort"] == "high"
    assert value["remaining_percent"] == pytest.approx(98.9)
    assert set(value["rate_provenance"]) == {"gpt-6-astra", "gpt-6.1-sol"}


def test_canonical_alias_preference_still_requires_actual_transport_projection(tmp_path, monkeypatch):
    from coordinator_core.session import external_registry
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path))
    peers = tmp_path / "unimatrix" / "peers.json"
    peers.parent.mkdir()
    peers.write_text(json.dumps({"peers": [{"name": name, "vendor": "codex", "target": ROOT}
                                           for name in ("legacy-route", "canonical-route")]}))
    monkeypatch.setattr(external_registry, "lookup", lambda sid: {"session_id": sid, "address_version": 1, "name": "canonical-route"})
    assert registered_alias(ROOT) == "canonical-route"
    monkeypatch.setattr(external_registry, "lookup", lambda sid: {"session_id": sid, "address_version": 1, "name": "not-projected"})
    assert registered_alias(ROOT) is None

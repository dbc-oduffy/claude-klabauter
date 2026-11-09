"""Incremental native-event ledger; copied fork history is charged once."""
from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime

from coordinator_core.usage.price_table import estimate_usd, RATE_PROVENANCE, model_family
_UNSET = object()


def epoch(value):
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError, AttributeError):
        return None


def configure(row, model, effort, speed, observed_at):
    stamp = epoch(observed_at)
    prior = row.get("configuration_at")
    if prior is not None and (stamp is None or stamp < prior):
        return
    if model and model != row.get("model"):
        row.pop("window", None)
        row.pop("used", None)
    row.update(model=model, effort=effort, speed=speed, configuration_at=stamp)


def empty(root: str) -> dict:
    return {"version": 2, "root": root, "threads": {}, "seen": {}, "files": {},
            "coverage": {"subagents": False, "workflows": False}, "warnings": []}


def thread(state: dict, sid: str) -> dict:
    return state["threads"].setdefault(sid, {"parent": None, "model": None, "effort": None,
                                             "usd": 0.0, "native_usd": 0.0, "canonical_usd": 0.0, "joined_canonical_usd": 0.0,
                                             "native_events": 0, "canonical_events": 0, "joined_canonical_events": 0,
                                             "unpriced_events": 0, "observations": 0, "priced_events": 0})


def warn(state, message):
    if message not in state["warnings"]:
        state["warnings"].append(message)


def attach_parent(state, row, parent):
    if not isinstance(parent, str) or not parent or row.get("parent_conflict"):
        return
    if row.get("parent") and row["parent"] != parent:
        warn(state, "conflicting parent identities; ambiguous child spend is excluded")
        row.update(parent=None, parent_conflict=True)
    else:
        row["parent"] = parent


def counts(raw: dict) -> dict:
    if not isinstance(raw, dict) or any(snake not in raw and camel not in raw for snake, camel in
                                      (("input_tokens", "inputTokens"), ("output_tokens", "outputTokens"),
                                       ("cached_input_tokens", "cachedInputTokens"))):
        raise ValueError("native usage is missing required input/output/cache-read counters")
    fields = {"input_tokens": "inputTokens", "output_tokens": "outputTokens",
              "cached_input_tokens": "cachedInputTokens", "cache_write_input_tokens": "cacheWriteInputTokens",
              "total_tokens": "totalTokens"}
    out = {}
    for snake, camel in fields.items():
        value = raw.get(snake, raw.get(camel, 0))
        if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or value < 0:
            raise ValueError("invalid native token count")
        out[snake] = int(value)
    return out


def observe_usage(state: dict, sid: str, turn_id: str | None, usage: dict, observed_at=None,
                  pricing_model=_UNSET, pricing_speed=_UNSET):
    owner = thread(state, sid)
    total = counts(usage.get("total", usage.get("total_token_usage", {})))
    last = counts(usage.get("last", usage.get("last_token_usage", {})))
    if not last["input_tokens"] and not last["output_tokens"]:
        return  # Empty initial/rate-limit snapshots are not a settled request.
    window = usage.get("modelContextWindow", usage.get("model_context_window"))
    observed = epoch(observed_at)
    prior = max(owner.get("usage_at") or 0, owner.get("configuration_at") or 0)
    if isinstance(window, int) and not isinstance(window, bool) and window > 0 and (not prior or observed is not None and observed >= prior):
        owner["window"] = window
        owner["used"] = last["total_tokens"]
        owner["usage_at"] = observed
    if not turn_id:
        warn(state, "usage without a stable turn identity was not priced")
        return
    key = hashlib.sha256(json.dumps([turn_id, total], sort_keys=True).encode()).hexdigest()
    previous = owner.get("cumulative")
    owner["cumulative"] = total
    if key in state["seen"]:
        return
    cached, written = last["cached_input_tokens"], last["cache_write_input_tokens"]
    uncached = last["input_tokens"] - cached - written
    if uncached < 0:
        warn(state, "cache counters exceed observed input; usage is unpriced")
        owner["unpriced_events"] += 1
        return
    normalized = {"input_tokens": uncached, "output_tokens": last["output_tokens"],
                  "cache_read_input_tokens": cached, "cache_creation_input_tokens": written}
    priced_model = owner.get("model") if pricing_model is _UNSET else pricing_model
    speed = owner.get("speed") if pricing_speed is _UNSET else pricing_speed
    price = estimate_usd(priced_model, normalized, speed)
    pending = state.setdefault("pending_unpriced", {})
    if key in pending:
        if price is None:
            return
        old = thread(state, pending.pop(key))
        old["unpriced_events"] -= 1
        old["observations"] -= 1
        old[state.setdefault("pending_counters", {}).pop(key, "native_events")] -= 1
    if price is None:
        pending[key] = sid
        state.setdefault("pending_counters", {})[key] = "native_events"
    else:
        state["seen"][key] = True
    owner["observations"] += 1
    owner["native_events"] += 1
    if price is None:
        owner["unpriced_events"] += 1
    else:
        owner["usd"] += price
        owner["native_usd"] += price
        owner["priced_events"] += 1
    if priced_model:
        models = owner.setdefault("observed_models", [])
        if priced_model not in models:
            models.append(priced_model)
    # Last usage is one request; lifetime totals cannot safely be priced at
    # the latest model/tier. An initial gap remains explicitly incomplete.
    if previous is None and any(total[field] > last[field] for field in total):
        warn(state, "some usage predates the first priced observation")


def ingest(state: dict, event: dict, source: dict):
    kind, payload = event.get("type"), event.get("payload") or {}
    method, params = event.get("method"), event.get("params") or {}
    if kind == "coordinator_usage":
        sid, request = payload.get("session_id"), payload.get("request_id")
        if not isinstance(sid, str) or not isinstance(request, str) or payload.get("usage_format") != "coordinator-canonical-v1":
            warn(state, "workflow usage lacks actual identity or canonical counter format")
            return
        row = thread(state, sid)
        attach_parent(state, row, payload.get("parent_session_id"))
        configure(row, payload.get("model"), payload.get("effort"), payload.get("service_tier"), event.get("timestamp"))
        native_turn, native_total = payload.get("native_turn_id"), payload.get("native_total_usage")
        joined = isinstance(native_turn, str) and isinstance(native_total, dict)
        if joined:
            key = hashlib.sha256(json.dumps([native_turn, counts(native_total)], sort_keys=True).encode()).hexdigest()
        else:
            key = hashlib.sha256(json.dumps([sid, request, payload.get("provider")]).encode()).hexdigest()
        if key in state["seen"]:
            return
        usage = payload.get("usage")
        if not isinstance(usage, dict) or any(field not in usage for field in ("input_tokens", "output_tokens",
                                                                             "cache_read_input_tokens", "cache_creation_input_tokens")):
            warn(state, "workflow usage is missing required canonical counters")
            return
        for field in ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"):
            value = usage.get(field, 0)
            if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or value < 0:
                raise ValueError("invalid canonical workflow usage")
        price = estimate_usd(payload.get("model"), usage, payload.get("service_tier"))
        pending = state.setdefault("pending_unpriced", {})
        counter = "joined_canonical_events" if joined else "canonical_events"
        if key in pending:
            if price is None:
                return
            old = thread(state, pending.pop(key))
            old["unpriced_events"] -= 1
            old["observations"] -= 1
            # A native observation can be repaired by canonical data and vice versa.
            prior_counter = state.setdefault("pending_counters", {}).pop(key, counter)
            old[prior_counter] -= 1
        if price is None:
            pending[key] = sid
            state.setdefault("pending_counters", {})[key] = counter
        else:
            state["seen"][key] = True
        row["observations"] += 1
        row[counter] += 1
        if payload.get("model"):
            models = row.setdefault("observed_models", [])
            if payload["model"] not in models:
                models.append(payload["model"])
        if price is None:
            row["unpriced_events"] += 1
        else:
            row["usd"] += price
            row["joined_canonical_usd" if joined else "canonical_usd"] += price
            row["priced_events"] += 1
        return
    if kind == "session_meta" and not source.get("thread_id"):
        sid = payload.get("id")
        if not isinstance(sid, str) or not sid:
            return
        source["thread_id"] = sid
        owner = thread(state, sid)
        parent = payload.get("parent_thread_id")
        if parent is None and isinstance(payload.get("source"), dict):
            parent = (payload["source"].get("subagent", {}).get("thread_spawn", {}).get("parent_thread_id"))
        attach_parent(state, owner, parent or source.get("parent_override"))
        owner["cwd"] = payload.get("cwd")
        return
    if method == "thread/started":
        metadata = params.get("thread", {})
        sid = metadata.get("id")
        if sid:
            owner = thread(state, sid)
            attach_parent(state, owner, metadata.get("parentThreadId"))
            owner["cwd"] = metadata.get("cwd")
            configure(owner, metadata.get("model"), metadata.get("reasoningEffort"), None, event.get("timestamp"))
        return
    if method == "thread/tokenUsage/updated":
        sid = params.get("threadId")
        if sid:
            observe_usage(state, sid, params.get("turnId"), params.get("tokenUsage") or {}, event.get("timestamp"))
        return
    if method == "model/rerouted" and params.get("threadId"):
        row = thread(state, params["threadId"])
        configure(row, params.get("toModel"), row.get("effort"), row.get("speed"), event.get("timestamp"))
        return
    if method == "thread/compacted" or (method == "item/completed" and (params.get("item") or {}).get("type") == "contextCompaction"):
        if params.get("threadId"):
            thread(state, params["threadId"]).pop("used", None)
        return
    sid = source.get("thread_id")
    if not sid:
        return
    owner = thread(state, sid)
    if kind == "turn_context":
        model = payload.get("model")
        source.update(model=model, effort=payload.get("effort"), speed=payload.get("service_tier"))
        configure(owner, model, payload.get("effort"), payload.get("service_tier"), event.get("timestamp"))
        source["turn_id"] = payload.get("turn_id")
    elif kind == "event_msg" and payload.get("type") == "token_count" and isinstance(payload.get("info"), dict):
        observe_usage(state, sid, source.get("turn_id"), payload["info"], event.get("timestamp"), source.get("model"), source.get("speed"))
    elif kind == "compacted":
        owner.pop("used", None)  # Lifetime spend survives; old context doesn't.


def descendants(state: dict) -> set[str]:
    selected = {state["root"]}
    while True:
        added = {sid for sid, row in state["threads"].items() if row.get("parent") in selected}
        if added <= selected:
            return selected
        selected |= added


def summary(state: dict, alias: str | None = None) -> dict:
    root = thread(state, state["root"])
    included = descendants(state)
    rows = [row for sid, row in state["threads"].items() if sid in included]
    overlap = any((row["native_events"] or row["joined_canonical_events"]) and row["canonical_events"] for row in rows)
    warnings = list(state["warnings"])
    if any(sid not in included and (row.get("observations") or row.get("unpriced_events"))
           for sid, row in state["threads"].items()):
        warnings.append("observed usage has unknown or unrelated parent ownership; excluded from this root estimate")
    if overlap:
        warnings.append("native and workflow recorders overlap without a request join; per-thread maximum is a lower-bound estimate")
    remaining = None
    if root.get("window") and root.get("used") is not None:
        remaining = max(0, min(100, 100 * (1 - root["used"] / root["window"])))
    return {"session_id": state["root"], "alias": alias, "model": root.get("model"),
            "effort": root.get("effort"), "remaining_percent": remaining,
            "context_window": root.get("window"), "captured_at": root.get("usage_at"),
            "context_basis": "observed model context window; not an independently observed compaction threshold",
            "usd": sum(max(r["native_usd"] + r["joined_canonical_usd"], r["canonical_usd"]) for r in rows), "priced_events": sum(r.get("priced_events", 0) for r in rows),
            "unpriced_events": sum(r["unpriced_events"] for r in rows), "threads": len(rows),
            "coverage": state["coverage"], "warnings": warnings,
            "backlog_complete": state.get("backlog_complete", False),
            "rate_provenance": {m: RATE_PROVENANCE.get(model_family(m), {"basis": "legacy engine price table" if model_family(m) else "unpriced; no known model rate"})
                                for m in {model for r in rows for model in [r.get("model"), *r.get("observed_models", [])]} if m}}

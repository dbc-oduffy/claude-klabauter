from __future__ import annotations

import contextlib
import json
from pathlib import Path

from coordinator_core.codex_status import hook
from coordinator_core.tui_status.fields import PARTIAL_MARK
import pytest

SID = "01a1b63c-040a-76f2-a9b9-929d9a0d0098"


def value():
    return {"session_id": SID, "alias": "actual-route", "usd": 1.23, "priced_events": 3,
            "backlog_complete": True, "coverage": {"subagents": True, "workflows": True},
            "warnings": [], "unpriced_events": 0}


def test_metadata_title_write_requires_loaded_namespace_and_preserved_task(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []
    class Connection:
        def request(self, method, params, deadline):
            calls.append((method, params))
            if method == "thread/loaded/list":
                return {"data": [SID]}
            if method == "thread/read":
                return {"thread": {"id": SID, "cwd": str(tmp_path), "name": "Original assigned work"}}
            return {}
    monkeypatch.setattr(hook.codex_app_server, "_connect", lambda *a, **kw: contextlib.nullcontext(Connection()))
    monkeypatch.setattr(hook.CodexAdapter, "proxy_argv", lambda self: ["native", "app-server", "proxy"])
    def preserve(sid, title):
        calls.append(("preserve", title))
        return {"session_id": sid, "original_task_title": title}
    monkeypatch.setattr(hook.external_registry, "preserve_task_title", preserve, raising=False)
    result = hook.apply_title(value(), str(tmp_path))
    title = "actual-route · model not yet observed · unknown · unknown · ~$1.23"
    assert result == {"updated": True, "title": title}
    assert [call[0] for call in calls] == ["thread/loaded/list", "thread/read", "preserve", "thread/name/set"]
    assert calls[-1][1] == {"threadId": SID, "name": title}


def test_other_namespace_never_renames_or_starts_a_thread(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []
    class Connection:
        def request(self, method, params, deadline) -> dict[str, list[str]]:
            calls.append(method)
            return {"data": ["some-other-thread"]}
    monkeypatch.setattr(hook.codex_app_server, "_connect", lambda *a, **kw: contextlib.nullcontext(Connection()))
    monkeypatch.setattr(hook.CodexAdapter, "proxy_argv", lambda self: ["native"])
    monkeypatch.setattr(hook.external_registry, "preserve_task_title", lambda *a: {}, raising=False)
    assert hook.apply_title(value(), str(tmp_path))["updated"] is False
    assert calls == ["thread/loaded/list"]


def test_unregistered_alias_still_attempts_the_title_write_with_the_label(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "settings"))
    cache = tmp_path / "cache.json"
    snap = {**value(), "alias": None, "cache_path": str(cache)}
    monkeypatch.setattr(hook, "snapshot", lambda p: snap)
    seen = []
    def attempt(v, cwd):
        seen.append(hook.status_title(v))
        return {"updated": False, "reason": "thread is not in this loaded app-server namespace"}
    monkeypatch.setattr(hook, "apply_title", attempt)
    result = hook.handle({"session_id": SID, "cwd": str(tmp_path)}, "Stop")
    assert result["reason"] != "no exact registered route alias"
    assert len(seen) == 1 and seen[0].startswith("unregistered · model not yet observed · ")


def test_unregistered_and_unobserved_labels_replace_bare_unknown_in_alias_and_model_slots() -> None:
    from coordinator_core.codex_status.render import render
    bare = {"coverage": {}, "backlog_complete": True}
    slots = render(bare).split(" · ")
    assert slots[0] == "unregistered" and slots[1] == "model not yet observed"
    known = render({**bare, "alias": "actual-route", "model": "gpt-6.2-sol", "effort": "low"}).split(" · ")
    assert known[:3] == ["actual-route", "gpt-6.2-sol", "low"]


def test_status_event_is_fail_open_and_never_ends_a_session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(*a, **kw):
        raise OSError("temporarily unavailable")
    monkeypatch.setattr(hook, "snapshot", broken)
    assert hook.handle({"session_id": SID, "cwd": str(tmp_path)}, "SubagentStop")["updated"] is False
    assert hook.handle({"session_id": SID, "cwd": str(tmp_path)}, "SessionEnd")["reason"] == "not a status refresh event"


def test_subagent_stop_binds_actual_child_metadata_and_preserves_fork_dedup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from coordinator_core.codex_status.__main__ import snapshot
    child = "01a1b63c-040a-76f2-a9b9-929d9a0d0099"
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "settings"))
    def meta(sid):
        return {"type": "session_meta", "payload": {"id": sid}}
    def context(turn):
        return {"type": "turn_context", "payload": {"turn_id": turn, "model": "gpt-6.1-sol", "effort": "low"}}
    def usage(inp, out):
        count = {"input_tokens": inp, "cached_input_tokens": 0, "output_tokens": out, "total_tokens": inp + out}
        return {"type": "event_msg", "payload": {"type": "token_count", "info": {
            "last_token_usage": count, "total_token_usage": count, "model_context_window": 475000}}}
    prefix = [meta(SID), context("root-turn"), usage(1000, 100)]
    parent_path, child_path = tmp_path / "parent.jsonl", tmp_path / "child.jsonl"
    parent_path.write_text("".join(json.dumps(row) + "\n" for row in prefix))
    # The first real child metadata has no parent; copied parent metadata must
    # never replace the actual child UUID or be billed a second time.
    child_path.write_text("".join(json.dumps(row) + "\n" for row in [meta(child), *prefix, context("child-turn"), usage(200, 20)]))
    before = snapshot({"session_id": SID, "rollouts": [str(parent_path)]})
    payload = {"session_id": SID, "cwd": str(tmp_path), "agent_id": "not-a-session-identity",
               "transcript_path": str(parent_path), "agent_transcript_path": str(child_path)}
    first = hook.handle(payload, "SubagentStop")["snapshot"]
    second = hook.handle(payload, "SubagentStop")["snapshot"]
    assert before["usd"] == .003
    assert first["usd"] == second["usd"] == .0036
    assert first["threads"] == 2 and first["priced_events"] == 2
    assert first["subagent_stop_ownership"]["child_session_id"] == child
    assert first["subagent_stop_ownership"]["parent_session_id"] == SID


def test_subagent_stop_never_substitutes_parent_uuid_for_child(tmp_path: Path) -> None:
    import pytest
    from coordinator_core.codex_status.collect import bind_subagent_stop
    from coordinator_core.codex_status.model import empty
    path = tmp_path / "wrong-child.jsonl"
    path.write_text(json.dumps({"type": "session_meta", "payload": {"id": SID}}) + "\n")
    state = empty(SID)
    with pytest.raises(ValueError, match="parent, not a child"):
        bind_subagent_stop(state, SID, path)
    assert state["files"] == {} and state["threads"] == {}


def _sol_rollout(path: Path, model: str) -> None:
    count = {"input_tokens": 1000, "cached_input_tokens": 500, "output_tokens": 100, "total_tokens": 1100}
    rows = [{"type": "session_meta", "payload": {"id": SID}},
            {"type": "turn_context", "payload": {"turn_id": "sol-turn", "model": model, "effort": "low"}},
            {"type": "event_msg", "payload": {"type": "token_count", "info": {
                "last_token_usage": count, "total_token_usage": count, "model_context_window": 475000}}}]
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))


def test_gpt_62_sol_cached_input_prices_through_the_hook_with_plus_while_coverage_is_incomplete(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from coordinator_core.codex_status.render import render
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "settings"))
    rollout = tmp_path / "rollout.jsonl"
    _sol_rollout(rollout, "gpt-6.2-sol")
    value = hook.handle({"session_id": SID, "cwd": str(tmp_path), "transcript_path": str(rollout)}, "Stop")["snapshot"]
    assert value["priced_events"] == 1 and value["unpriced_events"] == 0
    assert value["usd"] == pytest.approx(0.00205)  # 500 uncached @2 + 500 cached @0.10 + 100 out @10, per 1M
    assert hook.status_title({**value, "alias": "actual-route"}) == f"actual-route · gpt-6.2-sol · low · unknown · ~$0.00{PARTIAL_MARK}"
    assert "$" in value["text"] and value["text"] == render(value)
    assert value["coverage"] == {"subagents": False, "workflows": False}


def test_status_title_is_the_render_line_with_model_effort_and_gauge_slots() -> None:
    value = {"alias": "actual-route", "model": "gpt-6.2-sol", "effort": "high", "before_compact_percent": 42.0,
             "usd": 1.5, "priced_events": 1, "backlog_complete": True, "coverage": {"subagents": True, "workflows": True}}
    title = hook.status_title(value)
    assert title == hook.render(value)
    assert title.startswith("actual-route · gpt-6.2-sol · high · ") and "42" in title and title.endswith("~$1.50")
    assert hook.status_title({k: v for k, v in value.items() if k != "before_compact_percent"}).count("unknown") == 1
    assert hook.status_title({**value, "alias": None}).startswith("unregistered · gpt-6.2-sol · high · ")


def test_previously_unpriced_gpt_62_sol_events_reprice_on_the_next_refresh(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from coordinator_core.codex_status import model as status_model
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "settings"))
    rollout = tmp_path / "rollout.jsonl"
    _sol_rollout(rollout, "gpt-6.2-sol")
    payload = {"session_id": SID, "cwd": str(tmp_path), "transcript_path": str(rollout)}
    priced = status_model.estimate_usd
    monkeypatch.setattr(status_model, "estimate_usd", lambda *a, **kw: None)  # the table before the rate existed
    before = hook.handle(payload, "Stop")["snapshot"]
    assert before["priced_events"] == 0 and before["unpriced_events"] == 1
    assert hook.status_title({**before, "alias": "actual-route"}) == "actual-route · gpt-6.2-sol · low · unknown · unknown"
    monkeypatch.setattr(status_model, "estimate_usd", priced)
    after = hook.handle(payload, "Stop")["snapshot"]
    assert after["priced_events"] == 1 and after["unpriced_events"] == 0
    assert after["usd"] == pytest.approx(0.00205)
    assert hook.status_title({**after, "alias": "actual-route"}) == f"actual-route · gpt-6.2-sol · low · unknown · ~$0.00{PARTIAL_MARK}"
    assert "~$" in after["text"] and not after["text"].endswith("unknown")


def test_detached_entry_reads_the_stdin_payload_and_runs_the_refresh(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    import io
    seen = []
    payload = {"session_id": SID, "cwd": "C:/x"}
    monkeypatch.setattr(hook.sys, "stdin", io.StringIO(json.dumps(payload)))
    monkeypatch.setattr(hook, "handle", lambda p, event: seen.append((p, event)) or {"updated": True})
    assert hook.main(["--event", "PostCompact"]) == 0
    assert seen == [(payload, "PostCompact")] and capsys.readouterr().out.strip() == "{}"


def test_cold_proxy_handshake_is_not_cut_off_at_200ms(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A cold proxy needs ~0.6s to upgrade; the 0.2s bound made every refresh fail unseen. Fakes _connect: does not cover the interactive-TUI namespace return."""
    from coordinator_core.agent_pipe.codex_app_server import AppServerUnreachable
    class Connection:
        def request(self, method, params, deadline):
            if method == "thread/loaded/list":
                return {"data": [SID]}
            if method == "thread/read":
                return {"thread": {"id": SID, "cwd": str(tmp_path), "name": "Original"}}
            return {}
    def connect(argv, *, experimental=False, timeout=15.0):
        if timeout < 0.6:
            raise AppServerUnreachable(-1, "no upgrade reply from the app-server")
        return contextlib.nullcontext(Connection())
    monkeypatch.setattr(hook.codex_app_server, "_connect", connect)
    monkeypatch.setattr(hook.CodexAdapter, "proxy_argv", lambda self: ["native"])
    monkeypatch.setattr(hook.external_registry, "preserve_task_title",
                        lambda sid, title: {"session_id": sid, "original_task_title": title}, raising=False)
    assert hook.apply_title(value(), str(tmp_path))["updated"] is True


def test_refresh_outcome_reason_is_persisted_never_swallowed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "settings"))
    snap = {**value(), "cache_path": str(tmp_path / "cache.json")}
    monkeypatch.setattr(hook, "snapshot", lambda p: snap)
    def refuse(v, cwd):
        raise RuntimeError("no upgrade reply from the app-server")
    monkeypatch.setattr(hook, "apply_title", refuse)
    result = hook.handle({"session_id": SID, "cwd": str(tmp_path)}, "Stop")
    assert result["updated"] is False
    logged = json.loads((tmp_path / "settings" / "state" / "codex-status" / f"{SID}.last-refresh.json").read_text())
    assert logged["updated"] is False and logged["event"] == "Stop"
    assert logged["reason"] == "RuntimeError: no upgrade reply from the app-server"
    monkeypatch.setattr(hook, "apply_title", lambda v, cwd: {"updated": False, "reason": "thread is not in this loaded app-server namespace"})
    hook.handle({"session_id": SID, "cwd": str(tmp_path)}, "Stop")
    logged = json.loads((tmp_path / "settings" / "state" / "codex-status" / f"{SID}.last-refresh.json").read_text())
    assert logged["reason"] == "thread is not in this loaded app-server namespace"

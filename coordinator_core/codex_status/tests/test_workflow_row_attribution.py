"""Two native Codex sessions, one running a workflow: each snapshot row carries only its own thread tree's usage."""
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from coordinator_core.codex_status.__main__ import snapshot

A = "01a1b63c-040a-76f2-a9b9-929d9a0d0001"
B = "01a1b63c-040a-76f2-a9b9-929d9a0d0002"
A_CHILD = "01a1b63c-040a-76f2-a9b9-929d9a0d00a1"
B_CHILD = "01a1b63c-040a-76f2-a9b9-929d9a0d00b1"
MODEL = "gpt-6.1-sol"


def _usage(total):
    counts = {"input_tokens": total, "cached_input_tokens": 0, "output_tokens": total // 10, "total_tokens": total + total // 10}
    return {"total_token_usage": counts, "last_token_usage": counts, "model_context_window": 258400}


def _rollout(home: Path, sid: str, tokens: int) -> Path:
    now = datetime.now(timezone.utc)
    path = home / "sessions" / now.strftime("%Y/%m/%d") / f"rollout-{now:%Y-%m-%dT%H-%M-%S}-{sid}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [
        {"type": "session_meta", "timestamp": "2026-11-22T00:00:00Z", "payload": {"id": sid, "cwd": "/w"}},
        {"type": "turn_context", "timestamp": "2026-11-22T00:00:01Z", "payload": {"turn_id": f"turn-{sid}", "model": MODEL, "effort": "high"}},
        {"type": "event_msg", "timestamp": "2026-11-22T00:00:02Z", "payload": {"type": "token_count", "info": _usage(tokens)}},
    ]
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return path


def _workflow_record(child: str, parent: str, request: str, tokens: int) -> dict:
    return {"type": "coordinator_usage", "timestamp": "2026-11-22T00:00:03Z", "payload": {
        "session_id": child, "parent_session_id": parent, "request_id": request, "provider": "codex",
        "model": MODEL, "usage_format": "coordinator-canonical-v1",
        "usage": {"input_tokens": tokens, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0, "output_tokens": 0}}}


def _append(path: Path, record: dict) -> None:
    with path.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(record) + "\n")


@pytest.fixture
def box(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    settings = tmp_path / "settings"
    codex = tmp_path / "codex"
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(settings))
    monkeypatch.setenv("CODEX_HOME", str(codex))
    _rollout(codex, A, 1000)
    _rollout(codex, B, 2000)
    journals = settings / "state" / "codex-usage"
    journals.mkdir(parents=True)
    _append(journals / f"{A}.jsonl", _workflow_record(A_CHILD, A, "fire-a:1", 3000))
    _append(journals / f"{B}.jsonl", _workflow_record(B_CHILD, B, "fire-b:1", 5000))
    return settings


def _cache_threads(settings: Path, sid: str) -> dict:
    return json.loads((settings / "state" / "codex-status" / f"{sid}.json").read_text(encoding="utf-8"))["threads"]


def test_each_session_row_counts_only_its_own_workflow_usage(box: Path) -> None:
    a = snapshot({"session_id": A})
    b = snapshot({"session_id": B})
    assert a["priced_events"] == 2 and b["priced_events"] == 2
    threads_a = _cache_threads(box, A)
    assert set(threads_a) == {A, A_CHILD}
    assert set(_cache_threads(box, B)) == {B, B_CHILD}
    assert a["usd"] == pytest.approx(threads_a[A]["native_usd"] + threads_a[A_CHILD]["canonical_usd"])
    assert b["usd"] > a["usd"]
    assert not any("unknown or unrelated" in w for w in a["warnings"] + b["warnings"])


def test_a_foreign_journal_never_charges_this_row(box: Path) -> None:
    foreign = box / "state" / "codex-usage" / f"{B}.jsonl"
    alone = snapshot({"session_id": A})["usd"]
    crossed = snapshot({"session_id": A, "event_journals": [str(foreign)]})
    assert crossed["usd"] == pytest.approx(alone)


def test_nested_workflow_usage_reaches_only_its_own_tree(box: Path) -> None:
    worker, nested = "01a1b63c-040a-76f2-a9b9-929d9a0d00c1", "01a1b63c-040a-76f2-a9b9-929d9a0d00c2"
    journals = box / "state" / "codex-usage"
    _append(journals / f"{A}.jsonl", _workflow_record(worker, A, "fire-a:2", 3000))
    _append(journals / f"{worker}.jsonl", _workflow_record(nested, worker, "fire-w:1", 7000))
    a = snapshot({"session_id": A})
    b = snapshot({"session_id": B})
    assert nested in _cache_threads(box, A) and a["threads"] == 4
    assert nested not in _cache_threads(box, B)
    assert a["priced_events"] == 4 and b["priced_events"] == 2

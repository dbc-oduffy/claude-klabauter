"""A rendered deferred-emit script waits out handover_pending (up to 3 emit attempts) and refuses at once on any other error.

Renders the script through the production deferred path, then runs it under node with stubbed `agent`, `workflow` and a zero-delay `setTimeout`.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import threading
from pathlib import Path

import pytest

from coordinator_core import warp_fire
from coordinator_core.hooks import submit_engine_ask as mod

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node is required to run the rendered script")


def _render(tmp_path: Path, monkeypatch) -> str:
    root = tmp_path / "repo"
    (root / ".git" / "objects").mkdir(parents=True)
    (root / ".git" / "refs").mkdir()
    (root / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    monkeypatch.setenv("COORDINATOR_WORKFLOW_HOOK_TIMEOUT_S", "1.0")
    release = threading.Event()
    payload = {
        "tool_name": "Workflow",
        "tool_input": {"name": warp_fire.SUBMIT_WORKFLOW_NAME, "args": {"ask": "build it", "source": "relayed", "em_context": "short"}},
        "session_id": "sess-1",
        "transcript_path": str(root / "transcript.jsonl"),
        "cwd": str(root),
    }
    try:
        mod.evaluate(payload, emit=lambda params, r: release.wait(30) and {})
    finally:
        release.set()
        for worker in threading.enumerate()[1:]:
            worker.join(10)
    (script,) = (root / "scratch" / "warp").glob("defer-*.workflow.mjs")
    return script.read_text(encoding="utf-8")


def _run(text: str, replies: list, tmp_path: Path) -> dict:
    """Run the script with `agent` answering `replies` in order; returns its result, the agent call count and the waits."""
    body = text.replace("export const meta", "const meta", 1)
    harness = (
        "const replies = " + json.dumps(replies) + ";\n"
        "const calls = []; const waits = [];\n"
        "const agent = async (prompt, opts) => { calls.push(opts.label); return replies[Math.min(calls.length - 1, replies.length - 1)]; };\n"
        "const workflow = async (o) => ({ started: o.scriptPath });\n"
        "const _st = setTimeout;\n"
        "globalThis.setTimeout = (fn, ms) => { waits.push(ms); return _st(fn, 0); };\n"
        "const main = async () => {\n" + body + "\n};\n"
        "main().then((r) => console.log(JSON.stringify({ result: r, calls: calls.length, waits })));\n"
    )
    path = tmp_path / "harness.mjs"
    path.write_text(harness, encoding="utf-8")
    out = subprocess.run(["node", str(path)], capture_output=True, text=True, timeout=60, check=True)
    return json.loads(out.stdout.strip().splitlines()[-1])


_PENDING = {"path": "", "run_id": "", "fire_args": {}, "status": "handover_pending", "retry_after": 7}
_OK = {"path": "scratch/warp/ask-1.workflow.mjs", "run_id": "ask-1", "fire_args": {}}


def test_handover_pending_is_retried_then_started(tmp_path, monkeypatch):
    text = _render(tmp_path, monkeypatch)
    got = _run(text, [_PENDING, _PENDING, _OK], tmp_path)
    assert got["calls"] == 3
    assert got["waits"] == [7000, 7000]
    assert got["result"].get("status") != "emit_refused", got


def test_handover_pending_gives_up_after_three_attempts(tmp_path, monkeypatch):
    text = _render(tmp_path, monkeypatch)
    got = _run(text, [_PENDING], tmp_path)
    assert got["calls"] == 3
    assert got["result"]["status"] == "emit_refused"
    assert "handover_pending" in got["result"]["error"]


def test_other_error_is_refused_at_once(tmp_path, monkeypatch):
    text = _render(tmp_path, monkeypatch)
    got = _run(text, [{"path": "", "run_id": "", "fire_args": {}, "error": "denied by guard"}, _OK], tmp_path)
    assert got["calls"] == 1
    assert got["waits"] == []
    assert got["result"] == {"status": "emit_refused", "error": "denied by guard"}


def test_wait_is_capped_and_falls_back_when_retry_after_absent(tmp_path, monkeypatch):
    text = _render(tmp_path, monkeypatch)
    big = dict(_PENDING, retry_after=900)
    bare = {"path": "", "run_id": "", "fire_args": {}, "error": "handover_pending"}
    got = _run(text, [big, bare, _OK], tmp_path)
    assert got["waits"] == [30000, 5000]

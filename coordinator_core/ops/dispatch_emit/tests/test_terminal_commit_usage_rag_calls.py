"""terminal_commit._write_usage: per-row rag_calls in usage.json, manifest.json untouched."""

from __future__ import annotations

import json
from pathlib import Path

from coordinator_core.ops.dispatch_emit import terminal_commit as tc
from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT

SID = "f3dca0d5-7375-4313-83e8-a173943c2734"
RUN = "ask-20261028T000000-abc123"


def _assistant(mid: str, *tool_names: str) -> str:
    return json.dumps({
        "requestId": mid,
        "message": {
            "id": mid, "model": "claude-opus-5-5",
            "usage": {"input_tokens": 10, "output_tokens": 5,
                      "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0},
            "content": [{"type": "tool_use", "name": n, "id": f"{mid}-{i}", "input": {}}
                        for i, n in enumerate(tool_names)],
        },
    }) + "\n"


def _stage(tmp_path: Path, monkeypatch) -> tuple[Path, Path]:
    cfg = tmp_path / "claude"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cfg))
    monkeypatch.delenv("CLAUDE_HOME", raising=False)
    wf = cfg / "projects" / "slug" / SID / "subagents" / "workflows" / "wf_1"
    wf.mkdir(parents=True)
    rag = "mcp__project-rag__project_symbol"
    (wf / "agent-a1.jsonl").write_text(_assistant("m1", rag, rag, "Read"), encoding="utf-8")
    (wf / "agent-a2.jsonl").write_text(_assistant("m2", "Read"), encoding="utf-8")
    (wf / "agent-a3.jsonl").write_text(_assistant("m3", rag), encoding="utf-8")
    prompt = json.dumps({"message": {"content": f"x scratch/warp/{RUN}/terminal-commit-params.json"}}) + "\n"
    (wf / "agent-a4.meta.json").write_text(json.dumps({"description": "commit"}), encoding="utf-8")
    (wf / "agent-a4.jsonl").write_text(prompt, encoding="utf-8")
    events = [
        {"type": "started", "agentId": "a1", "label": "work:C1"},
        {"type": "started", "agentId": "a2", "label": "work:C2"},
        {"type": "started", "agentId": "a3", "label": "work:C1#cont1"},
        {"type": "started", "agentId": "a4", "label": "commit:wave-0"},
    ]
    (wf / "journal.jsonl").write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")
    root = tmp_path / "repo"
    run_dir = root / RECORD_DIR_ROOT / RUN
    run_dir.mkdir(parents=True)
    manifest = run_dir / "manifest.json"
    manifest.write_bytes(b'{"rows": [{"id": "C1"}, {"id": "C2"}]}')
    return root, manifest


def test_usage_rows_carry_int_rag_calls_summed_per_row_and_manifest_is_untouched(tmp_path, monkeypatch):
    root, manifest = _stage(tmp_path, monkeypatch)
    before = manifest.read_bytes()
    tc._write_usage(root, SID, RUN)
    body = json.loads((root / RECORD_DIR_ROOT / RUN / "usage.json").read_text(encoding="utf-8"))
    assert body["rows"] == {"C1": {"rag_calls": 3}, "C2": {"rag_calls": 0}}
    assert all(isinstance(r["rag_calls"], int) for r in body["rows"].values())
    assert manifest.read_bytes() == before


def test_non_work_agents_are_not_rows(tmp_path, monkeypatch):
    root, _ = _stage(tmp_path, monkeypatch)
    tc._write_usage(root, SID, RUN)
    body = json.loads((root / RECORD_DIR_ROOT / RUN / "usage.json").read_text(encoding="utf-8"))
    assert "commit:wave-0" not in body["rows"] and set(body["rows"]) == {"C1", "C2"}

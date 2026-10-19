"""plan.overlap_edges over the real registry: JSON-RPC dispatch_message, scope table, reply shape."""

from __future__ import annotations

import asyncio
from pathlib import Path

from coordinator_core import ipc
from coordinator_core.authz.classification import OpClass, classify
from coordinator_core.op_scopes import _OP_KEY_SCOPE


def _row(rid: str, writes: str) -> str:
    return f"- id: {rid}\n  title: t\n  surface: s\n  change_kind: code-edit\n  writes: [{writes}]\n"


def _plan(root: Path, name: str, rid: str, writes: str) -> str:
    rel = f"docs/plans/{name}.md"
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        "---\ntitle: t\nstatus: approved\nmise_prepped_sha: abc123\n---\n\n## Tasks\n\n"
        f"```yaml plan-tasks\n{_row(rid, writes)}```\n",
        encoding="utf-8",
    )
    return rel


def test_dispatch_message_returns_the_design_reply_shape(tmp_path):
    (tmp_path / ".git").mkdir()
    a = _plan(tmp_path, "a-first", "A1", "x.py")
    b = _plan(tmp_path, "b-second", "B1", "x.py")
    msg = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "plan.overlap_edges",
        "params": {"plans": [b, a]},
        "_origin_worktree": str(tmp_path),
    }
    d = asyncio.run(ipc.dispatch_message(msg))
    assert "error" not in d, d
    r = d["result"]
    assert set(r) >= {"set", "edges", "cycles", "uncertified", "unreadable"}
    assert r["set"] == [a, b]
    assert [(e["before"], e["after"], e["paths"]) for e in r["edges"]] == [(a, b, ["x.py"])]
    assert r["cycles"] == [] and r["uncertified"] == [] and r["unreadable"] == []


def test_op_is_compute_only_and_common_dir_scoped():
    assert classify("plan.overlap_edges") is OpClass.COMPUTE_ONLY
    assert _OP_KEY_SCOPE["plan.overlap_edges"] == "common_dir"

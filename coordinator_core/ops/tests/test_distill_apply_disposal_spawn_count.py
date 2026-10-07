"""Spawn-count pin: `_delete_tracked_and_append_log` stages denorm writes and reverts on
log-stage failure in O(1) git calls, not O(N)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from coordinator_core.ops import distill_apply_disposal as mod


def _run(n: int, tmp_path: Path, monkeypatch, fail_log_add: bool) -> list[tuple[str, ...]]:
    calls: list[tuple[str, ...]] = []
    log_path = tmp_path / "log.jsonl"
    tracked = [tmp_path / f"c{i}.md" for i in range(n)]
    parents = [tmp_path / f"p{i}.md" for i in range(n)]

    async def fake_run_git(*args, cwd=None, env=None):
        calls.append(tuple(args))
        if fail_log_add and args[0] == "add" and str(log_path) in args:
            return 1, b"", b"boom"
        return 0, b"", b""

    async def fake_write(root, gated):
        return list(parents), [], list(gated), []

    monkeypatch.setattr(mod, "_run_git", fake_run_git)
    monkeypatch.setattr(mod, "_write_denormalizations", fake_write)
    monkeypatch.setattr(mod, "rel_id", lambda p, root: Path(p).name)
    denorm = [
        {"parent": p.name, "child": c.name, "successor_ref": "x", "provenance_comment": "y"}
        for p, c in zip(parents, tracked)
    ]
    monkeypatch.setattr(mod._log_append, "append_rows", lambda p, rows: p.write_text("r"))
    asyncio.run(
        mod._delete_tracked_and_append_log(
            tmp_path, tracked, log_path, [{"k": "v"}], "s", denorm_writes=denorm
        )
    )
    return calls


@pytest.mark.parametrize("fail_log_add", [False, True])
def test_git_call_count_independent_of_n(tmp_path, monkeypatch, fail_log_add):
    c1 = _run(1, tmp_path, monkeypatch, fail_log_add)
    c5 = _run(5, tmp_path, monkeypatch, fail_log_add)
    assert len(c5) == len(c1)
    if fail_log_add:
        assert sum(c[0] == "checkout" for c in c5) == 1
    assert sum(c[0] == "add" for c in c5) == 2

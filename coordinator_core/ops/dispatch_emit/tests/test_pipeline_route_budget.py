"""dispatch.emit pipeline route budget: one real emit of the structured fixture spawns nothing and
costs under 500ms of process time (never wall clock, which measures peer load)."""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

from coordinator_core.ops.dispatch_emit import op as op_module
from coordinator_core.ops.dispatch_emit.op import _dispatch_emit

_FIXTURE = Path(__file__).parent / "fixtures" / "pipeline_structured"
_BUDGET_S = 0.5


def _params(tmp_path, name):
    (tmp_path / "brief.md").write_text("research the subjects", encoding="utf-8")
    return {"pipeline": "structured", "brief": "brief.md",
            "subjects": [
                {"subject": k, "verifiers": [{"role": "v-a", "topic": "a", "name": "A"}]}
                for k in ("Subject One", "Subject Two", "Subject Three")],
            "target_root": str(tmp_path), "session_id": "sess-budget",
            "output_path": str(tmp_path / name)}


def test_pipeline_emit_is_spawn_free_and_under_budget(monkeypatch, tmp_path):
    monkeypatch.setattr(op_module, "read_content_root", lambda: "doe")
    monkeypatch.setattr(op_module, "content_root_for", lambda _root: _FIXTURE)

    warm = _dispatch_emit(_params(tmp_path, "warm.mjs"))
    assert Path(warm["path"]).is_file()

    spawns: list[str] = []

    def counter(name):
        def count(*args, **kwargs):
            spawns.append(name)
            raise AssertionError(f"{name} called on the pipeline emit path")
        return count

    monkeypatch.setattr(subprocess, "Popen", counter("subprocess.Popen"))
    for attr in ("posix_spawn", "fork"):
        if hasattr(os, attr):
            monkeypatch.setattr(os, attr, counter(f"os.{attr}"))

    before = time.process_time()
    reply = _dispatch_emit(_params(tmp_path, "measured.mjs"))
    elapsed = time.process_time() - before

    assert Path(reply["path"]).is_file() and Path(reply["receipt"]).is_file()
    print(f"pipeline emit process time: {elapsed * 1000:.1f}ms, spawns: {len(spawns)}")
    assert len(spawns) == 0
    assert elapsed < _BUDGET_S

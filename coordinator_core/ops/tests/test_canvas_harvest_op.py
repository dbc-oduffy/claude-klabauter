"""canvas.harvest handler: params validation, refusals, the claim-bound key, one real dispatch."""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

import coordinator_core.ops.canvas_harvest as op
from coordinator_core import ipc
from coordinator_core.canvas_harvest import api, model
from coordinator_core.session import claimed_write, touch_record
from coordinator_core.win_portability import no_console_passthrough_kwargs

URL = "https://claude.ai/artifact/abc"
BOUND_KEY = ipc._SCOPE_TOUCH_MAX_PATHS_KEY


def _call(params, repo_root=None):
    return asyncio.run(op._handler(params, repo_root=repo_root))


def _ok_params(**extra):
    return {"mode": "harvest", "url": URL, "out": "rec", **extra}


def test_unknown_mode_is_a_params_refusal_with_the_bound():
    r = _call({"mode": "bogus", "url": URL, "out": "x"}, repo_root="/r")
    assert r["ok"] is False and r["reason"] == op.BAD_PARAMS
    assert r[BOUND_KEY] == model.MAX_RECORD_FILES


@pytest.mark.parametrize("missing", ["url", "out"])
def test_missing_required_param(missing):
    params = _ok_params()
    del params[missing]
    r = _call(params, repo_root="/r")
    assert r["ok"] is False and r["reason"] == op.BAD_PARAMS


def test_relative_out_without_repo_root_is_refused():
    r = _call(_ok_params())
    assert r["ok"] is False and r["reason"] == op.BAD_PARAMS


def test_relative_out_resolves_against_repo_root(monkeypatch, tmp_path):
    seen = {}

    def fake(url, out_dir, **kw):
        seen["out"] = out_dir
        seen["kw"] = kw
        return model.RecordWrite(written=(Path(out_dir) / "harvest.json",), deleted=())

    monkeypatch.setattr(api, "harvest", fake)
    r = _call(_ok_params(session_id="s1", transcript="t.jsonl"), repo_root=str(tmp_path))
    assert seen["out"] == tmp_path / "rec"
    assert seen["kw"] == {"session_id": "s1", "transcript": "t.jsonl"}
    assert r["ok"] is True and r["wrote"] == [str(tmp_path / "rec" / "harvest.json")]
    assert r[BOUND_KEY] == model.MAX_RECORD_FILES


def test_probe_mode_calls_probe(monkeypatch, tmp_path):
    monkeypatch.setattr(api, "probe", lambda url, out, **kw: model.RecordWrite(written=(Path(out) / "p",), deleted=()))
    monkeypatch.setattr(api, "harvest", lambda *a, **k: pytest.fail("harvest called"))
    r = _call({"mode": "probe", "url": URL, "out": str(tmp_path)})
    assert r["ok"] is True and r["wrote"] == [str(tmp_path / "p")]


def test_harvest_error_is_ok_false_with_its_reason(monkeypatch, tmp_path):
    def boom(*a, **k):
        raise model.HarvestError(model.NO_TRANSCRIPT, "none")

    monkeypatch.setattr(api, "harvest", boom)
    r = _call(_ok_params(), repo_root=str(tmp_path))
    assert r["ok"] is False and r["reason"] == model.NO_TRANSCRIPT and r["detail"] == "none"
    assert r["wrote"] == [] and r[BOUND_KEY] == model.MAX_RECORD_FILES


def test_deleted_is_passed_through(monkeypatch, tmp_path):
    gone = tmp_path / "old.json"
    monkeypatch.setattr(api, "harvest", lambda *a, **k: model.RecordWrite(written=(), deleted=(gone,)))
    r = _call(_ok_params(), repo_root=str(tmp_path))
    assert r["deleted"] == [str(gone)]


def _git_repo(path):
    kw = no_console_passthrough_kwargs()
    subprocess.run(["git", "init", "-q"], cwd=path, check=True, **kw)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=path, check=True, **kw)
    subprocess.run(["git", "config", "user.name", "t"], cwd=path, check=True, **kw)
    (path / "README.md").write_text("x")
    subprocess.run(["git", "add", "."], cwd=path, check=True, **kw)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=path, check=True, **kw)


def test_real_dispatch_claims_every_file_past_the_default_cap(monkeypatch, tmp_path):
    _git_repo(tmp_path)
    n = ipc._MAX_DECLARED_TOUCH_PATHS + 8
    assert n <= model.MAX_RECORD_FILES
    monkeypatch.setenv("CLAUDE_SESSION_ID", "sid-canvas")
    for var in ("CLAUDE_CODE_SESSION_ID", "COORDINATOR_SESSION_ID"):
        monkeypatch.delenv(var, raising=False)

    def fake(url, out_dir, **kw):
        paths = []
        for i in range(n):
            paths.append(claimed_write.replace_bytes(Path(out_dir) / f"f{i}.bin", b"x"))
        return model.RecordWrite(written=tuple(paths), deleted=())

    monkeypatch.setattr(api, "harvest", fake)
    saved = ipc._REGISTRY.get("canvas.harvest")
    ipc._REGISTRY["canvas.harvest"] = op._handler
    try:
        msg = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "canvas.harvest",
            "params": _ok_params(),
            "_origin_worktree": str(tmp_path),
        }
        d = asyncio.run(ipc.dispatch_message(msg))
    finally:
        if saved is None:
            ipc._REGISTRY.pop("canvas.harvest", None)
        else:
            ipc._REGISTRY["canvas.harvest"] = saved
    assert "error" not in d
    assert d["result"]["ok"] is True and len(d["result"]["wrote"]) == n
    assert BOUND_KEY not in d["result"] and ipc._SCOPE_TOUCH_PATHS_KEY not in d["result"]
    log = tmp_path / ".git" / "coordinator-sessions" / "sid-canvas" / "touch-record.jsonl"
    events = [touch_record.decode_line(x) for x in touch_record.iter_complete_lines(log.read_bytes())]
    assert len(events) == n

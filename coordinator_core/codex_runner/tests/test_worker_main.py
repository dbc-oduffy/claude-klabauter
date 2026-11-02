"""codex_runner.__main__ worker: request in, result out, roster record bracketed around codex."""
from __future__ import annotations

import json

import pytest

from coordinator_core.codex_runner import __main__ as worker
from coordinator_core.codex_runner import roster
from coordinator_core.codex_runner.contract import STATUS_OK, RunRequest, RunResult


@pytest.fixture
def run_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "home"))
    d = tmp_path / "codex-runs" / "review-abc"
    d.mkdir(parents=True)
    return d


def _write_request(d, **kw):
    req = {"mode": "review", "repo_root": str(d.parent), "artifact": "a.md", **kw}
    (d / "request.json").write_text(json.dumps(req), encoding="utf-8")


def test_success_writes_result_and_brackets_roster(run_dir, monkeypatch):
    _write_request(run_dir, parent_session_id="sess-1")
    seen = {}

    def fake_run(req, d, on_spawn):
        on_spawn(4242)
        seen["during"] = (roster.roster_dir() / "4242.json").is_file()
        return RunResult(run_id=d.name, status=STATUS_OK, verdict="Ready", codex_pid=4242)

    monkeypatch.setattr(worker, "_mode_run", lambda mode: fake_run)
    assert worker.main([str(run_dir)]) == 0

    assert seen["during"] is True
    assert not (roster.roster_dir() / "4242.json").exists()
    out = json.loads((run_dir / "result.json").read_text(encoding="utf-8"))
    assert out["status"] == STATUS_OK and out["run_id"] == "review-abc" and out["codex_pid"] == 4242
    assert not list(run_dir.glob("*.tmp"))


def test_record_carries_request_fields(run_dir, monkeypatch):
    _write_request(run_dir, parent_session_id="sess-1")
    captured = {}
    monkeypatch.setattr(roster, "write_record", lambda *a: captured.setdefault("args", a))
    monkeypatch.setattr(roster, "remove_record", lambda pid: None)

    def fake_run(req, d, on_spawn):
        on_spawn(7)
        return RunResult(run_id=d.name, status=STATUS_OK)

    monkeypatch.setattr(worker, "_mode_run", lambda mode: fake_run)
    worker.run_worker(run_dir)
    assert captured["args"] == (7, "review-abc", "review", str(run_dir.parent), "sess-1")


def test_mode_dispatch_by_request(run_dir, monkeypatch):
    (run_dir / "request.json").write_text(
        json.dumps({"mode": "execute", "repo_root": str(run_dir.parent), "plan": "p.md", "chunk": "C1"}),
        encoding="utf-8",
    )
    modes = []

    def pick(mode):
        modes.append(mode)
        return lambda req, d, on_spawn: RunResult(run_id=d.name, status=STATUS_OK)

    monkeypatch.setattr(worker, "_mode_run", pick)
    worker.run_worker(run_dir)
    assert modes == ["execute"]


def test_mode_run_resolves_each_mode_to_its_function():
    from coordinator_core.codex_runner import agy, execute, review

    assert worker._mode_run("review") is review.run
    assert worker._mode_run("execute") is execute.run
    assert worker._mode_run("agy-review") is agy.review_run


def test_agy_review_roster_record_carries_mode(run_dir, monkeypatch):
    _write_request(run_dir, mode="agy-review", parent_session_id="sess-1")
    captured = {}
    monkeypatch.setattr(roster, "write_record", lambda *a: captured.setdefault("args", a))
    monkeypatch.setattr(roster, "remove_record", lambda pid: None)

    def fake_run(req, d, on_spawn):
        on_spawn(8)
        return RunResult(run_id=d.name, status=STATUS_OK)

    monkeypatch.setattr(worker, "_mode_run", lambda mode: fake_run)
    worker.run_worker(run_dir)
    assert captured["args"][2] == "agy-review"


def test_raising_run_leaves_failed_result_and_removes_record(run_dir, monkeypatch):
    _write_request(run_dir)

    def boom(req, d, on_spawn):
        on_spawn(99)
        raise RuntimeError("codex exploded")

    monkeypatch.setattr(worker, "_mode_run", lambda mode: boom)
    worker.run_worker(run_dir)

    out = json.loads((run_dir / "result.json").read_text(encoding="utf-8"))
    assert out["status"] == "failed" and "codex exploded" in out["error"] and out["codex_pid"] == 99
    assert not (roster.roster_dir() / "99.json").exists()


def test_missing_request_leaves_failed_result(run_dir):
    worker.run_worker(run_dir)
    out = json.loads((run_dir / "result.json").read_text(encoding="utf-8"))
    assert out["status"] == "failed"


def test_main_usage_error():
    assert worker.main([]) == 2


def test_request_roundtrip_matches_contract():
    req = RunRequest(mode="review", repo_root="/r", artifact="a")
    assert RunRequest.from_dict(req.to_dict()) == req

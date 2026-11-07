"""run_write_prints.resolve_run: a subagent write resolves to its own live run when several hold the session."""

from __future__ import annotations

import json
import subprocess

from coordinator_core.ops.dispatch_emit import kept_work, run_token, run_write_prints
from coordinator_core.ops.dispatch_emit.ask_contract import record_dir

A, B = "runA", "runB"


def _live(monkeypatch, *run_ids, session="S1"):
    records = [{"run_id": r, "session_id": session} for r in run_ids]
    monkeypatch.setattr(run_token, "live_records", lambda root: iter(records))


def _manifest(root, run_id, *writes):
    d = record_dir(root, run_id)
    d.mkdir(parents=True, exist_ok=True)
    (d / "manifest.json").write_text(json.dumps({"rows": [{"writes": list(writes)}]}), encoding="utf-8")


def _agents(root, run_id):
    return json.loads((record_dir(root, run_id) / "agent_runs.json").read_text(encoding="utf-8"))


def _two_runs(tmp_path, monkeypatch):
    _live(monkeypatch, A, B)
    _manifest(tmp_path, A, "a.txt")
    _manifest(tmp_path, B, "b.txt")
    for name in ("a.txt", "b.txt", "free.txt"):
        (tmp_path / name).write_bytes(name.encode() + b"\n")


def test_each_edit_prints_under_its_own_run(tmp_path, monkeypatch):
    _two_runs(tmp_path, monkeypatch)

    assert run_write_prints.record_write(tmp_path, "S1", "agentA", "a.txt") == A
    assert run_write_prints.record_write(tmp_path, "S1", "agentB", "b.txt") == B

    assert run_write_prints.read(tmp_path, A) == {"a.txt": kept_work.blob_sha(b"a.txt\n")}
    assert run_write_prints.read(tmp_path, B) == {"b.txt": kept_work.blob_sha(b"b.txt\n")}


def test_binding_carries_an_undeclared_path_to_the_same_run(tmp_path, monkeypatch):
    _two_runs(tmp_path, monkeypatch)
    run_write_prints.record_write(tmp_path, "S1", "agentA", "a.txt")

    assert run_write_prints.record_write(tmp_path, "S1", "agentA", "free.txt") == A

    assert set(run_write_prints.read(tmp_path, A)) == {"a.txt", "free.txt"}
    assert run_write_prints.read(tmp_path, B) is None
    assert "agentA" in _agents(tmp_path, A)
    assert not (record_dir(tmp_path, B) / "agent_runs.json").exists()


def test_undeclared_path_without_binding_is_ambiguous(tmp_path, monkeypatch):
    _two_runs(tmp_path, monkeypatch)

    assert run_write_prints.record_write(tmp_path, "S1", "agentC", "free.txt") is None

    assert run_write_prints.read(tmp_path, A) is None
    assert run_write_prints.read(tmp_path, B) is None
    assert not (record_dir(tmp_path, A) / "agent_runs.json").exists()


def test_path_in_both_footprints_without_binding_records_nothing(tmp_path, monkeypatch):
    _live(monkeypatch, A, B)
    _manifest(tmp_path, A, "a.txt")
    _manifest(tmp_path, B, "a.txt")
    (tmp_path / "a.txt").write_bytes(b"x\n")

    assert run_write_prints.record_write(tmp_path, "S1", "agentA", "a.txt") is None
    assert run_write_prints.read(tmp_path, A) is None
    assert run_write_prints.read(tmp_path, B) is None


def test_binding_beats_a_shared_footprint(tmp_path, monkeypatch):
    _live(monkeypatch, A, B)
    _manifest(tmp_path, A, "a.txt")
    _manifest(tmp_path, B, "a.txt", "b.txt")
    (tmp_path / "a.txt").write_bytes(b"x\n")
    (tmp_path / "b.txt").write_bytes(b"y\n")
    run_write_prints.record_write(tmp_path, "S1", "agentB", "b.txt")  # unique to B: binds

    assert run_write_prints.record_write(tmp_path, "S1", "agentB", "a.txt") == B
    assert "a.txt" in run_write_prints.read(tmp_path, B)
    assert run_write_prints.read(tmp_path, A) is None


def test_a_single_live_run_is_bound_and_still_prints(tmp_path, monkeypatch):
    _live(monkeypatch, A)
    (tmp_path / "x.txt").write_bytes(b"x\n")

    assert run_write_prints.record_write(tmp_path, "S1", "agentA", "x.txt") == A
    assert _agents(tmp_path, A) == {"agentA": True}


def test_another_sessions_run_is_never_chosen(tmp_path, monkeypatch):
    _live(monkeypatch, A, session="S2")
    (tmp_path / "x.txt").write_bytes(b"x\n")

    assert run_write_prints.resolve_run(tmp_path, "S1", "agentA", "x.txt") is None


def test_unreadable_binding_or_manifest_never_raises(tmp_path, monkeypatch):
    _live(monkeypatch, A, B)
    for run in (A, B):
        d = record_dir(tmp_path, run)
        d.mkdir(parents=True, exist_ok=True)
        (d / "manifest.json").write_text("{nope", encoding="utf-8")
        (d / "agent_runs.json").write_text("[1", encoding="utf-8")

    assert run_write_prints.resolve_run(tmp_path, "S1", "agentA", "a.txt") is None


def test_resolution_and_print_spawn_nothing(tmp_path, monkeypatch):
    _two_runs(tmp_path, monkeypatch)
    spawned = []
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: spawned.append(a) or (_ for _ in ()).throw(OSError()))

    run_write_prints.record_write(tmp_path, "S1", "agentA", "a.txt")
    run_write_prints.record_write(tmp_path, "S1", "agentA", "free.txt")

    assert spawned == []


def test_concurrent_binds_keep_every_agent(tmp_path):
    import threading

    record_dir(tmp_path, A).mkdir(parents=True, exist_ok=True)
    agents = [f"agent{i}" for i in range(12)]
    threads = [threading.Thread(target=run_write_prints._bind, args=(tmp_path, A, a)) for a in agents]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(_agents(tmp_path, A)) == sorted(agents)

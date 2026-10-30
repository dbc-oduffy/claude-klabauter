"""Batch job: one call per model, flagged symbols only, sidecar-only commit, leg outcome amended."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import run_close
from coordinator_core.symbol_knowledge import agent_batches, sidecar
from coordinator_core.symbol_knowledge.contract import FlaggedSymbol

ADJ = "scip-python python pkg 0.1 `pkg.m`/adj()."
NEW = "scip-python python pkg 0.1 `pkg.m`/new()."
SHA = "a" * 40


def _flagged():
    return [FlaggedSymbol(ADJ, "pkg/m.py", "adjust", True), FlaggedSymbol(NEW, "pkg/m.py", "net-new", True)]


def _payload(flagged=None, traps=None):
    return {
        "run_id": "r1",
        "sha": SHA,
        "flagged": [
            {"qualified_name": f.qualified_name, "path": f.path, "kind": f.kind, "public": f.public}
            for f in (flagged if flagged is not None else _flagged())
        ],
        "traps": traps or {},
    }


class Recorder:
    def __init__(self, replies=None, fail=()):
        self.calls = []
        self.replies = replies or {}
        self.fail = set(fail)

    def __call__(self, model, prompt):
        self.calls.append((model, prompt))
        if model in self.fail:
            raise RuntimeError("boom")
        return self.replies.get(model, "{}")


def test_one_call_per_model_and_kind_routing(tmp_path):
    rec = Recorder({"haiku": json.dumps({ADJ: "adj def"}), "sonnet": "text " + json.dumps({NEW: "new def"})})
    commits = []
    status, detail = agent_batches.run_job(tmp_path, _payload(), rec, lambda *a: commits.append(a))
    assert [m for m, _ in rec.calls] == ["haiku", "sonnet"]
    assert ADJ in rec.calls[0][1] and NEW not in rec.calls[0][1]
    assert NEW in rec.calls[1][1] and ADJ not in rec.calls[1][1]
    assert (status, detail) == ("ran", f"2 current at {SHA[:10]}")
    entries = sidecar.read_sidecar(tmp_path)
    assert entries[ADJ]["definition"] == "adj def" and entries[NEW]["sha"] == SHA
    assert commits == [(tmp_path, SHA, 2)]


def test_kind_with_no_symbols_makes_no_call(tmp_path):
    rec = Recorder({"haiku": json.dumps({ADJ: "d"})})
    flagged = [FlaggedSymbol(ADJ, "pkg/m.py", "adjust", True)]
    agent_batches.run_job(tmp_path, _payload(flagged), rec, lambda *a: None)
    assert [m for m, _ in rec.calls] == ["haiku"]


def test_failed_runner_loses_only_its_kind(tmp_path):
    rec = Recorder({"sonnet": json.dumps({NEW: "n"})}, fail={"haiku"})
    status, detail = agent_batches.run_job(tmp_path, _payload(), rec, lambda *a: None)
    assert status == "ran" and "haiku RuntimeError" in detail
    assert list(sidecar.read_sidecar(tmp_path)) == [NEW]


def test_nothing_produced_is_failed_and_writes_no_sidecar(tmp_path):
    status, detail = agent_batches.run_job(tmp_path, _payload(), Recorder(), lambda *a: None)
    assert status == "failed" and "unparseable" in detail
    assert not sidecar.sidecar_path(tmp_path).exists()


def test_unrequested_names_are_ignored():
    assert agent_batches.parse_definitions(json.dumps({"x": "d", ADJ: "ok"}), [ADJ]) == {ADJ: "ok"}
    assert agent_batches.parse_definitions("no json", [ADJ]) == {}


def test_traps_merge_into_entries(tmp_path):
    rec = Recorder({"haiku": json.dumps({ADJ: "d"}), "sonnet": json.dumps({NEW: "n"})})
    agent_batches.run_job(tmp_path, _payload(traps={ADJ: ["halted here"]}), rec, lambda *a: None)
    assert sidecar.read_sidecar(tmp_path)[ADJ]["traps"] == ["halted here"]


def test_launch_empty_spawns_nothing(tmp_path):
    spawned = []
    detail = agent_batches.launch_batches(tmp_path, "r1", [], {}, spawn=lambda *a, **k: spawned.append(a) or True)
    assert spawned == [] and "no flagged" in detail
    assert not agent_batches.job_path(tmp_path, "r1").exists()


def test_launch_writes_payload_and_spawns_once(tmp_path):
    spawned = []
    detail = agent_batches.launch_batches(
        tmp_path, "r1", _flagged(), {ADJ: ["t"]}, sha=SHA, spawn=lambda *a, **k: spawned.append(a) or True
    )
    assert len(spawned) == 1
    root, script, args = spawned[0]
    assert Path(script) == Path(agent_batches.__file__).resolve()
    assert args == ["--job", str(agent_batches.job_path(tmp_path, "r1"))]
    payload = json.loads(agent_batches.job_path(tmp_path, "r1").read_text(encoding="utf-8"))
    assert payload["sha"] == SHA and payload["traps"] == {ADJ: ["t"]} and len(payload["flagged"]) == 2
    assert "1 adjust, 1 net-new" in detail


def test_launch_reports_spawn_failure(tmp_path):
    detail = agent_batches.launch_batches(tmp_path, "r1", _flagged(), {}, sha=SHA, spawn=lambda *a, **k: False)
    assert "not spawned" in detail


def _git(root, *args):
    return subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=True,
        check=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    ).stdout


@pytest.mark.spawns_process
def test_commit_names_only_the_sidecar_and_peer_dirt_stays_unstaged(tmp_path):
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@example.com")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / "peer.txt").write_text("v1", encoding="utf-8")
    _git(tmp_path, "add", "peer.txt")
    _git(tmp_path, "commit", "-q", "-m", "base")
    (tmp_path / "peer.txt").write_text("dirty", encoding="utf-8")
    sidecar.write_sidecar(tmp_path, sidecar.merge_entries({}, {ADJ: ("d", SHA)}))
    agent_batches._commit_sidecar(tmp_path, SHA, 1)
    assert _git(tmp_path, "show", "--name-only", "--format=", "HEAD").split() == [sidecar.SIDECAR_REL]
    assert _git(tmp_path, "status", "--porcelain").split() == ["M", "peer.txt"]


def test_main_amends_leg_outcome_and_removes_payload(tmp_path, monkeypatch):
    record = {"legs": {agent_batches.LEG: {"status": "queued", "detail": "x"}}, "deadline": "2099-01-01T00:00:00+00:00"}
    jobfile = tmp_path / run_close.close_job_path("r1")
    jobfile.parent.mkdir(parents=True)
    jobfile.write_text(json.dumps(record), encoding="utf-8")
    path = agent_batches.job_path(tmp_path, "r1")
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(_payload()), encoding="utf-8")
    rec = Recorder({"haiku": json.dumps({ADJ: "d"}), "sonnet": json.dumps({NEW: "n"})})
    monkeypatch.setattr(agent_batches, "claude_runner", lambda model, prompt, cwd=None: rec(model, prompt))
    monkeypatch.setattr(agent_batches, "_commit_sidecar", lambda *a: None)
    assert agent_batches.main(["--job", str(path)]) == 0
    leg = json.loads(jobfile.read_text(encoding="utf-8"))["legs"][agent_batches.LEG]
    assert leg == {"status": "ran", "detail": f"2 current at {SHA[:10]}"}
    assert not path.exists()

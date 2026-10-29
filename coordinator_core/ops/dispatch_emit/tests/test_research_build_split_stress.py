"""Stress the research/build split end to end: gate split, research.close release, failure paths, unchanged routes."""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path

import pytest
import yaml

from coordinator_core.ops.dispatch_emit import ask_fanout_contract as contract
from coordinator_core.ops.dispatch_emit import requirements_ledger as rl
from coordinator_core.ops.dispatch_emit import sibling_hold
from coordinator_core.ops.dispatch_emit.ask_gate import gate
from coordinator_core.ops.research_close import _handler as close_handler
from coordinator_core.warp_fire import fire_command

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_NOWIN = getattr(subprocess, "CREATE_NO_WINDOW", 0)
REL = "state/sizings/2026-10-29-split-stress.yaml"
WRITES = ["docs/a.md", "docs/b.md"]
RESEARCH = {"value_class": "scouts", "appetite": "small", "sources": ["web"], "questions": ["why?"]}


def _git(repo: Path, *args: str) -> str:
    r = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false", *args],
        cwd=repo, capture_output=True, text=True, check=True, creationflags=_NOWIN,
    )
    return r.stdout.strip()


def _doc(research):
    doc = {
        "schema": "sizing-object", "name": "split stress", "intent": "research then write",
        "estimate": {"tshirt": "S", "provisional": True}, "route": "spec-dispatch", "detents": [],
        "fork": None, "xl_exit": None, "status": "sized",
        "premise": {"provenance": "not-applicable", "evidence": "fixture"},
        "deliverable_id": "dlv-fixture-abc123", "interaction_mode": "pm",
        "exit_criterion": {"statement": "done", "accepted": None},
    }
    if research is not None:
        doc["research"] = research
    return doc


@pytest.fixture
def repo(tmp_path, monkeypatch):
    r = tmp_path / "repo"
    (r / "state" / "sizings").mkdir(parents=True)
    _git(r, "init", "-q")
    (r / "seed.txt").write_text("seed\n")
    _git(r, "add", "seed.txt")
    _git(r, "commit", "-q", "-m", "seed")
    monkeypatch.chdir(r)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", "11111111-2222-3333-4444-555555555555")
    monkeypatch.delenv("DELIVERABLE_ID", raising=False)
    return r


def _put(repo, research, rel=REL):
    (repo / rel).write_text(yaml.safe_dump(_doc(research)), encoding="utf-8", newline="\n")


def _split(repo, research=RESEARCH, writes=WRITES):
    _put(repo, research)
    v = gate(repo, REL, writes=writes)
    assert v.arm is None and "split" in v.halt, v
    s = v.halt["split"]
    _bind_research(repo, s)
    return s


def _bind_research(repo, s):
    """The status file `dispatch.emit` writes when the fired research slice binds its run."""
    path = repo / contract.status_rel(s["research"]["run_id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"status": contract.STATUS_RUNNING, "parent_id": s["parent_id"]}), encoding="utf-8")


def _status(repo, run_id):
    """A run's recorded status; a sibling never bound has no file and reads as its parent-record status."""
    path = repo / contract.status_rel(run_id)
    if path.is_file():
        return json.loads(path.read_text(encoding="utf-8"))["status"]
    return "unbound"


def _close(repo, tmp_path, split, **extra):
    r = split["research"]
    scratch = tmp_path / "scratch"
    scratch.mkdir(exist_ok=True)
    (scratch / "final.md").write_text("body\n")
    params = {
        "scratch_dir": str(scratch), "tier": "corpus", "run_id": r["run_id"],
        "topic_slug": "stress", "outputs": ["final.md"], "sizing": r["sizing"],
    }
    params.update(extra)
    return asyncio.run(close_handler(params, repo / ".git"))


def test_happy_path_build_held_until_close_then_released(repo, tmp_path):
    s = _split(repo)
    r, b = s["research"], s["build"]
    assert _status(repo, r["run_id"]) == contract.STATUS_RUNNING and _status(repo, b["run_id"]) == "unbound"
    assert sibling_hold.predecessor_closed(repo, r["run_id"]) is None
    out = _close(repo, tmp_path, s)
    assert out["exit_code"] == 0 and out["research_block_dropped"] is True, out
    assert out["released_runs"] == [b["run_id"]]
    assert sibling_hold.predecessor_closed(repo, r["run_id"])
    assert "research" not in yaml.safe_load((repo / r["sizing"]).read_text(encoding="utf-8"))


def test_research_halted_never_releases_the_build(repo):
    from coordinator_core.ops.dispatch_emit import run_close

    s = _split(repo)
    reply = run_close.sibling_release(repo, s["research"]["run_id"], landed_sha=None)
    assert reply is not None and not reply.get("released"), reply
    assert [x["run_id"] for x in reply.get("blocked", [])] == [s["build"]["run_id"]]
    assert _status(repo, s["build"]["run_id"]) == "unbound"


def test_refused_close_leaves_build_held(repo, tmp_path):
    s = _split(repo)
    out = _close(repo, tmp_path, s, outputs=["missing.md"])
    assert out["exit_code"] == 1 and "released_runs" not in out, out
    assert _status(repo, s["build"]["run_id"]) == "unbound"


def test_close_twice_releases_once(repo, tmp_path):
    s = _split(repo)
    first = _close(repo, tmp_path, s)
    assert first["released_runs"] == [s["build"]["run_id"]], first
    second = _close(repo, tmp_path, s)
    assert second["released_runs"] == first["released_runs"], second  # same single sibling, never extra


def test_build_only_revise_before_close_does_not_fire_or_orphan_the_build(repo, tmp_path):
    s = _split(repo)
    rid = s["research"]["run_id"]
    rl.start(repo, "ledger-run")
    rl.bind(repo, "ledger-run", s["research"]["sizing"], ask_text="research then write")
    reply = rl._handler({"run_id": "ledger-run", "build_only": True}, repo_root=repo)
    assert reply["ok"] is True and reply["research_dropped"] is True, reply
    assert _status(repo, s["build"]["run_id"]) == "unbound"  # revise never fires the build
    out = _close(repo, tmp_path, s)
    assert out["exit_code"] == 0, out
    # The research run closed with its block already gone: the build must still be freed.
    assert out.get("released_runs") == [s["build"]["run_id"]], (rid, out)


@pytest.mark.parametrize("with_questions", [True, False])
def test_notebooklm_targets_derive_or_halt(repo, with_questions):
    research = {**RESEARCH, "sources": ["notebooklm"]}
    if not with_questions:
        research.pop("questions")
    _put(repo, research)
    v = gate(repo, REL, writes=WRITES)
    assert v.arm is None and v.halt["kind"] == "research"
    if with_questions:
        s = v.halt["split"]
        doc = yaml.safe_load((repo / s["research"]["sizing"]).read_text(encoding="utf-8"))
        assert [t["ref"] for t in doc["research"]["targets"]] == ["why?"]
        assert _status(repo, s["build"]["run_id"]) == "unbound"
    else:
        assert "notebooklm targets" in v.halt["reason"] and "split" not in v.halt
        assert [p.name for p in (repo / "state" / "sizings").iterdir()] == [Path(REL).name]


def test_no_writes_research_sizing_does_not_split(repo):
    _put(repo, RESEARCH)
    v = gate(repo, REL, writes=[])
    assert "split" not in (v.halt or {})
    assert [p.name for p in (repo / "state" / "sizings").iterdir()] == [Path(REL).name]


def test_non_research_sizing_does_not_split(repo):
    _put(repo, None)
    v = gate(repo, REL, writes=WRITES)
    assert "split" not in (v.halt or {})
    assert [p.name for p in (repo / "state" / "sizings").iterdir()] == [Path(REL).name]


def test_fire_command_of_the_research_sibling_is_the_only_fire(repo):
    _put(repo, RESEARCH)
    v = gate(repo, REL, writes=WRITES)
    assert v.halt["fire"] == [fire_command(v.halt["split"]["research"]["sizing"])]


def _assert_build_closed(repo, s, reason):
    b = s["build"]["run_id"]
    assert _status(repo, b) == contract.STATUS_HALTED
    status = json.loads((repo / contract.status_rel(b)).read_text(encoding="utf-8"))
    assert status["reason"] == reason and status["abandoned_with"] == s["research"]["run_id"]
    parent = json.loads((repo / contract.parent_record_rel(s["parent_id"])).read_text(encoding="utf-8"))
    entry = next(r for r in parent["runs"] if r["run_id"] == b)
    assert entry["status"] == contract.STATUS_HALTED and entry["hold_reason"] == reason
    assert sibling_hold.predecessor_closed(repo, b) is None


def test_abandon_run_closes_the_build_without_firing_it(repo):
    from coordinator_core.ops.dispatch_emit import op, run_token

    s = _split(repo)
    rid = s["research"]["run_id"]
    (repo / "state" / "warp" / rid).mkdir(parents=True)
    (repo / "state" / "warp" / rid / "run-stage.json").write_text('{"stage": "staged"}', encoding="utf-8")
    run_token.mint(repo, rid, "scratch/w.mjs", session_id="s1", sizing_rel=s["research"]["sizing"])
    out = op._abandon_run({"abandon_run": rid, "session_id": "s1", "target_root": str(repo)}, repo)
    assert out["abandoned"] is True
    _assert_build_closed(repo, s, "research-run-abandoned")


def test_abandon_of_a_foreign_run_leaves_the_build_held(repo):
    from coordinator_core.ops.dispatch_emit import op, run_token

    s = _split(repo)
    rid = s["research"]["run_id"]
    (repo / "state" / "warp" / rid).mkdir(parents=True)
    run_token.mint(repo, rid, "scratch/w.mjs", session_id="other", sizing_rel=s["research"]["sizing"])
    out = op._abandon_run({"abandon_run": rid, "session_id": "s1", "target_root": str(repo)}, repo)
    assert out["abandoned"] is False
    assert _status(repo, s["build"]["run_id"]) == "unbound"


def test_reap_closes_the_build_without_firing_it(repo):
    import os
    import time

    from coordinator_core.ops.dispatch_emit import abandoned_runs, run_token

    s = _split(repo)
    rid = s["research"]["run_id"]
    rec = repo / "state" / "warp" / rid
    rec.mkdir(parents=True)
    (rec / "ask.md").write_text("ask\n", encoding="utf-8")
    (rec / "run.json").write_text("{}", encoding="utf-8")
    old = time.time() - run_token.TTL_SECONDS - 3600
    for f in rec.iterdir():
        os.utime(f, (old, old))
    out = abandoned_runs._handler({}, repo_root=repo / ".git")
    assert rid in out["reaped"], out
    _assert_build_closed(repo, s, "research-run-reaped")

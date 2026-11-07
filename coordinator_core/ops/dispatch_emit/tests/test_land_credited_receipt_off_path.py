"""``dispatch.land_credited`` commits before, and never waits on, the completion receipt write."""

from __future__ import annotations

import logging
import subprocess
import threading
from pathlib import Path

import pytest

from coordinator_core.completion_receipts import store
from coordinator_core.ops.dispatch_emit import completion_receipt
from coordinator_core.ops.dispatch_emit import land_credited as lane
from coordinator_core.ops.dispatch_emit import land_credited_contract as contract
from coordinator_core.ops.dispatch_emit.commit_request import ChunkCommit, CommitRequest, render_marker
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.complete]

RUN = "warp-receipt-run"
SCRIPT = "scratch/run.js"


def _git(args, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True, **no_console_creationflags(),
    ).stdout


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / "scratch").mkdir()
    _git(["init", "-q"], root)
    _git(["config", "user.email", "t@example.com"], root)
    _git(["config", "user.name", "t"], root)
    _git(["config", "core.autocrlf", "false"], root)
    (root / "src/a.py").write_text("v = 0\n", encoding="utf-8", newline="")
    marker = render_marker(CommitRequest(chunks=(ChunkCommit(id="C1", title="one", paths=("src/a.py",)),)))
    (root / SCRIPT).write_text(f"// script\n{marker}\n", encoding="utf-8", newline="")
    _git(["add", "."], root)
    _git(["commit", "-q", "-m", "seed"], root)
    (root / "src/a.py").write_text("v = 1\n", encoding="utf-8", newline="")
    return root


def _request() -> contract.LandCreditedRequest:
    return contract.LandCreditedRequest(run_id=RUN, plan_path="docs/plans/p.md", script_path=SCRIPT)


@pytest.fixture(autouse=True)
def _drain():
    yield
    for t in list(lane.DEFERRED_RECEIPT_THREADS):
        t.join(timeout=5)


def test_stalled_receipt_write_does_not_delay_the_commit_or_the_reply(repo, monkeypatch):
    release = threading.Event()
    entered = threading.Event()

    def blocked(root, fm, prose):
        entered.set()
        release.wait(timeout=30)
        return "unused"

    monkeypatch.setattr(completion_receipt, "build_run_receipts", lambda *a, **k: [({}, "")])
    monkeypatch.setattr(store, "write_receipt", blocked)
    try:
        reply = lane.land(repo, _request())

        assert reply["committed"] is True, reply
        assert _git(["rev-parse", "HEAD"], repo).strip() == reply["sha"]
        assert entered.wait(timeout=5), "the deferred receipt write never started"
        assert any(t.is_alive() for t in lane.DEFERRED_RECEIPT_THREADS)
    finally:
        release.set()


def test_released_receipt_write_lands_after_the_reply(repo, monkeypatch):
    release = threading.Event()
    landed = []

    def gated(root, fm, prose):
        release.wait(timeout=30)
        landed.append((fm, prose))
        return "receipt.md"

    monkeypatch.setattr(completion_receipt, "build_run_receipts", lambda *a, **k: [({"k": "v"}, "p")])
    monkeypatch.setattr(store, "write_receipt", gated)
    try:
        reply = lane.land(repo, _request())
        assert reply["committed"] is True, reply
        assert landed == []
    finally:
        release.set()
    for t in list(lane.DEFERRED_RECEIPT_THREADS):
        t.join(timeout=5)
    assert landed == [({"k": "v"}, "p")]


def test_failed_receipt_write_is_logged_and_the_land_stands(repo, monkeypatch, caplog):
    def boom(root, fm, prose):
        raise OSError("disk frozen")

    monkeypatch.setattr(completion_receipt, "build_run_receipts", lambda *a, **k: [({}, "")])
    monkeypatch.setattr(store, "write_receipt", boom)
    with caplog.at_level(logging.WARNING, logger=lane.__name__):
        reply = lane.land(repo, _request())
        for t in list(lane.DEFERRED_RECEIPT_THREADS):
            t.join(timeout=5)

    assert reply["committed"] is True, reply
    assert _git(["rev-parse", "HEAD"], repo).strip() == reply["sha"]
    assert any("receipt write" in r.getMessage() and "disk frozen" in r.getMessage() for r in caplog.records)


def test_real_build_run_receipts_writes_a_receipt_after_the_land(repo, caplog):
    plan = repo / "docs/plans/p.md"
    plan.parent.mkdir(parents=True)
    plan.write_text("---\ndeliverable_id: dlv-real-write\nworkstream: ws-x\n---\nbody\n", encoding="utf-8", newline="")
    with caplog.at_level(logging.WARNING, logger=lane.__name__):
        reply = lane.land(repo, _request())
        for t in list(lane.DEFERRED_RECEIPT_THREADS):
            t.join(timeout=10)

    assert reply["committed"] is True, reply
    assert not [r for r in caplog.records if "receipt write" in r.getMessage()], [r.getMessage() for r in caplog.records]
    written = [p for p in (repo / ".coordinator-local").rglob("*.md")] if (repo / ".coordinator-local").exists() else []
    assert any("dlv-real-write" in p.read_text(encoding="utf-8") for p in written), written


def test_refused_land_writes_no_receipt(repo, monkeypatch):
    calls = []
    monkeypatch.setattr(completion_receipt, "build_run_receipts", lambda *a, **k: calls.append(1) or [])
    (repo / "src/a.py").write_text("v = 0\n", encoding="utf-8", newline="")

    reply = lane.land(repo, _request())

    assert reply["committed"] is False
    assert reply["refused"] == contract.REFUSED_NOTHING_TO_LAND
    assert calls == []

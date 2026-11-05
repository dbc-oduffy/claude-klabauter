"""dispatch.emit on the ask route answers a pending publish handover with a hold reply, writing nothing."""

from __future__ import annotations

from coordinator_core.ops.dispatch_emit import op, run_token
from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT
from coordinator_core.ops.dispatch_emit.tests.test_ask_ledger_seed import repo  # noqa: F401
from coordinator_core.warm import breadcrumb


def _isolate(monkeypatch, tmp_path):
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "settings"))
    monkeypatch.setenv(breadcrumb.RUNTIME_BASE_ENV, str(tmp_path / "rt"))


def test_a_pending_handover_holds_the_ask_emit_and_leaves_no_run_state(repo, tmp_path, monkeypatch):  # noqa: F811
    _isolate(monkeypatch, tmp_path)
    run_id = "ask-20261103T204518-b06bac"
    run_token.mint(repo, run_id, "x.js", marker=True)
    with breadcrumb.hold_handover_pending():
        reply = op._dispatch_emit({"ask": "add a thing", "preminted_run_id": run_id}, repo_root=repo)
    assert reply["status"] == "handover_pending"
    assert reply["retry_after"] >= 1
    assert not (repo / RECORD_DIR_ROOT / run_id).exists()
    assert not run_token.is_live(repo, run_id)


def test_the_same_emit_mints_once_the_handover_clears(repo, tmp_path, monkeypatch):  # noqa: F811
    _isolate(monkeypatch, tmp_path)
    reply = op._dispatch_emit({"ask": "add a thing"}, repo_root=repo)
    assert reply.get("status") != "handover_pending"
    base = repo / RECORD_DIR_ROOT
    assert any(p.is_dir() for p in base.iterdir())

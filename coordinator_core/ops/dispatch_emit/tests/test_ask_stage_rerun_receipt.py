"""dispatch.ask_stage re-run: a landed stage replies its handle, a partial landing refuses, a revise stages afresh."""

from __future__ import annotations

import pytest

from coordinator_core.ops.dispatch_emit import ask_stage, ask_stage_receipt, reply_sidecar
from coordinator_core.ops.dispatch_emit.ask_contract import record_dir
from coordinator_core.ops.dispatch_emit.tests._size_l_plan import build_size_l_plan, isolate_launch_dirs


@pytest.fixture
def staged_repo(tmp_path, monkeypatch):
    isolate_launch_dirs(monkeypatch, tmp_path)
    root = tmp_path / "repo"
    root.mkdir()
    return build_size_l_plan(root, rows=3, live_tokens=2)


def _tree(path):
    return {p: p.read_bytes() for p in sorted(path.rglob("*")) if p.is_file()}


def test_rerun_after_landed_stage_replies_recorded_handle_and_writes_nothing(staged_repo, monkeypatch):
    root, plan_rel, run_id = staged_repo
    params = {"run_id": run_id, "plan_path": plan_rel}
    first = ask_stage._handler(dict(params), repo_root=root)
    assert "manifest_handle" in first and ask_stage_receipt.ALREADY_STAGED_KEY not in first
    before = _tree(root / "state") | _tree(root / "scratch")
    plan_before = (root / plan_rel).read_bytes()

    def boom(*a, **k):
        raise AssertionError("stage() ran on a landed re-run")

    monkeypatch.setattr(ask_stage, "stage", boom)
    again = ask_stage._handler({**params, "session_id": "other-session"}, repo_root=root)
    assert again == {"manifest_handle": first["manifest_handle"], ask_stage_receipt.ALREADY_STAGED_KEY: True}
    assert _tree(root / "state") | _tree(root / "scratch") == before
    assert (root / plan_rel).read_bytes() == plan_before


def test_manifest_without_receipt_refuses_and_writes_nothing(staged_repo, monkeypatch):
    root, plan_rel, run_id = staged_repo
    params = {"run_id": run_id, "plan_path": plan_rel}
    ask_stage._handler(dict(params), repo_root=root)
    (record_dir(root, run_id) / ask_stage_receipt.RECEIPT_NAME).unlink()
    before = _tree(root / "state") | _tree(root / "scratch")
    monkeypatch.setattr(ask_stage, "stage", lambda *a, **k: pytest.fail("stage() ran on a partial landing"))
    reply = ask_stage._handler(dict(params), repo_root=root)
    assert reply["error"].startswith(ask_stage.STAGE_RECEIPT_MISSING)
    assert _tree(root / "state") | _tree(root / "scratch") == before


def test_changed_params_stage_afresh_and_rewrite_the_receipt(staged_repo):
    root, plan_rel, run_id = staged_repo
    first = ask_stage._handler({"run_id": run_id, "plan_path": plan_rel}, repo_root=root)
    revised = {"run_id": run_id, "plan_path": plan_rel, "only_rows": ["C1"]}
    second = ask_stage._handler(dict(revised), repo_root=root)
    assert "manifest_handle" in second and ask_stage_receipt.ALREADY_STAGED_KEY not in second
    assert second["manifest_handle"] != first["manifest_handle"]
    digest = ask_stage_receipt.params_digest(revised)
    assert ask_stage_receipt.landed(record_dir(root, run_id), digest) == second["manifest_handle"]
    assert reply_sidecar.read_reply(root, second["manifest_handle"])["rows"]

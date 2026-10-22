"""dispatch.ask_stage stage claim: a live owner replies stage_in_flight, a dead owner re-stages, a late receipt replies already_staged, a refusal releases."""

from __future__ import annotations

import json
import os
import time

import pytest

from coordinator_core.ops.dispatch_emit import ask_stage, ask_stage_receipt
from coordinator_core.ops.dispatch_emit.ask_contract import record_dir
from coordinator_core.ops.dispatch_emit.tests._size_l_plan import build_size_l_plan, isolate_launch_dirs


@pytest.fixture
def staged_repo(tmp_path, monkeypatch):
    isolate_launch_dirs(monkeypatch, tmp_path)
    root = tmp_path / "repo"
    root.mkdir()
    return build_size_l_plan(root, rows=3, live_tokens=2)


def _plant_claim(root, run_id, params, *, alive):
    rec = record_dir(root, run_id)
    rec.mkdir(parents=True, exist_ok=True)
    body = {
        "digest": ask_stage_receipt.params_digest(params),
        "owner_pid": os.getpid() if alive else 2**22 + 12345,
        "started_at": time.time(),
    }
    (rec / ask_stage_receipt.CLAIM_NAME).write_text(json.dumps(body) + "\n", encoding="ascii")
    return rec


def test_live_owner_claim_replies_in_flight_and_writes_nothing(staged_repo, monkeypatch):
    root, plan_rel, run_id = staged_repo
    params = {"run_id": run_id, "plan_path": plan_rel}
    rec = _plant_claim(root, run_id, params, alive=True)
    monkeypatch.setattr(ask_stage, "stage", lambda *a, **k: pytest.fail("stage() ran under a live claim"))
    reply = ask_stage._handler(dict(params), repo_root=root)
    assert "error" not in reply
    assert reply[ask_stage_receipt.STAGE_IN_FLIGHT_KEY]["owner_pid"] == os.getpid()
    assert not (rec / ask_stage_receipt.RECEIPT_NAME).exists()
    assert (rec / ask_stage_receipt.CLAIM_NAME).exists()


def test_dead_owner_claim_restages_and_releases(staged_repo):
    root, plan_rel, run_id = staged_repo
    params = {"run_id": run_id, "plan_path": plan_rel}
    rec = _plant_claim(root, run_id, params, alive=False)
    reply = ask_stage._handler(dict(params), repo_root=root)
    assert "manifest_handle" in reply and ask_stage_receipt.STAGE_IN_FLIGHT_KEY not in reply
    assert not (rec / ask_stage_receipt.CLAIM_NAME).exists()
    assert (rec / ask_stage_receipt.RECEIPT_NAME).is_file()


def test_late_receipt_replies_already_staged_despite_live_claim(staged_repo):
    root, plan_rel, run_id = staged_repo
    params = {"run_id": run_id, "plan_path": plan_rel}
    first = ask_stage._handler(dict(params), repo_root=root)
    _plant_claim(root, run_id, params, alive=True)
    again = ask_stage._handler(dict(params), repo_root=root)
    assert again == {"manifest_handle": first["manifest_handle"], ask_stage_receipt.ALREADY_STAGED_KEY: True}


def test_refused_stage_releases_the_claim(staged_repo):
    root, plan_rel, run_id = staged_repo
    params = {"run_id": run_id, "plan_path": "docs/plans/absent.md"}
    reply = ask_stage._handler(dict(params), repo_root=root)
    assert "error" in reply
    assert not (record_dir(root, run_id) / ask_stage_receipt.CLAIM_NAME).exists()

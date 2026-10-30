"""requirements_ledger.late_rows / verify_staged / workflow.late_rows: which live ledger ids no staged work covers."""

from __future__ import annotations

import json

import yaml

from coordinator_core.ops.dispatch_emit import requirements_ledger as rl
from coordinator_core.ops.dispatch_emit import run_token
from coordinator_core.ops.dispatch_emit.ask_contract import LATE_ROWS_FIELD, OP_LATE_ROWS, record_dir

RUN = "ask-20261015T000000-abc123"
REL = "state/sizings/2026-10-15-late.yaml"


def _repo(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / "state" / "sizings").mkdir(parents=True)
    sizing = {
        "schema": "sizing-object", "intent": "build the op", "estimate": {"tshirt": "M", "provisional": True},
        "route": "plan", "detents": [], "fork": None, "xl_exit": None, "status": "sized",
        "premise": {"provenance": "not-applicable", "evidence": "fixture"},
        "pm_verbatims": [{"turn": 1, "text": "build the op"}],
    }
    (tmp_path / REL).write_text(yaml.safe_dump(sizing, sort_keys=False), encoding="utf-8")
    rl.start(tmp_path, RUN)
    rl.bind(tmp_path, RUN, REL, ask_text="a raw ask")
    return tmp_path


def _revise(root, monkeypatch, text):
    monkeypatch.setattr(run_token, "is_live", lambda *_a, **_k: True)
    return rl._handler({"run_id": RUN, "verbatim": text}, repo_root=root)["item"]


def _stage_manifest(root, ids):
    (record_dir(root, RUN) / "manifest.json").write_text(json.dumps({"ledger_item_ids": list(ids)}), encoding="utf-8")


def test_no_manifest_returns_empty(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    _revise(root, monkeypatch, "first")
    assert rl.late_rows(root, RUN) == []


def test_row_revised_after_staging_is_late_in_arrival_order(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    first = _revise(root, monkeypatch, "first")
    _stage_manifest(root, [i.id for i in rl.read_ledger(root, record_dir(root, RUN)).items])
    assert rl.late_rows(root, RUN) == []
    second = _revise(root, monkeypatch, "second")
    third = _revise(root, monkeypatch, "third")
    late = rl.late_rows(root, RUN)
    assert late == [second, third]


def test_op_replies_late_rows_and_refuses_unsafe_id(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    first = _revise(root, monkeypatch, "first")
    _stage_manifest(root, [])
    ids = [i.id for i in rl.read_ledger(root, record_dir(root, RUN)).items]
    assert first in ids
    assert rl._late_rows_handler({"run_id": RUN}, repo_root=root) == {LATE_ROWS_FIELD: ids}
    assert "error" in rl._late_rows_handler({"run_id": "../x"}, repo_root=root)
    assert OP_LATE_ROWS == "workflow.late_rows"


def test_late_rows_op_declares_a_repo_scope():
    from coordinator_core.op_scopes import OP_KEY_SCOPE

    assert OP_KEY_SCOPE.get(OP_LATE_ROWS) == "common_dir"


def test_verify_staged_follows_the_prompt_file(tmp_path):
    dir_ = tmp_path / "run"
    (dir_ / "prompts").mkdir(parents=True)
    assert rl.verify_staged(dir_) is False
    (dir_ / "prompts" / "ledger-fix.md").write_text("x", encoding="utf-8")
    assert rl.verify_staged(dir_) is False
    (dir_ / "prompts" / "verify.md").write_text("x", encoding="utf-8")
    assert rl.verify_staged(dir_) is True

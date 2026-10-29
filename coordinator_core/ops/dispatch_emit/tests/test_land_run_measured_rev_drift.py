"""land_run._ledger_gate refuses a `met` item when a blob the measured rev was judged on has drifted."""

from __future__ import annotations

import json
from types import SimpleNamespace

from coordinator_core.git import commit
from coordinator_core.ops.dispatch_emit import land_run, requirements_ledger
from coordinator_core.ops.dispatch_emit.ask_contract import measured_rev_record_path

RID = "run-c5"
VERDICT = {"items": [{"id": "R1", "status": "met"}]}


def _gate(root, monkeypatch, run_dir):
    monkeypatch.setattr(requirements_ledger, "read_ledger", lambda r, d: SimpleNamespace(items=[1], all_of_pm_ask=False))
    monkeypatch.setattr(requirements_ledger, "noted_paths", lambda r, d: [])
    monkeypatch.setattr(
        requirements_ledger, "judge", lambda *a, **k: {"pass": True, "failures": [], "open": []},
    )
    return land_run._ledger_gate(root, run_dir, VERDICT, None, [], {}, None, {}, None, None)


def _blob(root, rel):
    # The gate hashes via commit.checkin_blob_sha; raw-byte hashing disagrees under autocrlf=true.
    return commit.checkin_blob_sha(root, rel)


def _setup(tmp_path, blob_of):
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    record = measured_rev_record_path(tmp_path, RID)
    record.parent.mkdir(parents=True)
    record.write_text(json.dumps({"blobs": {"a.py": blob_of(tmp_path, "a.py")}, "removed": []}), encoding="utf-8")
    return record.parent


def test_refuses_met_on_drift(tmp_path, monkeypatch):
    run_dir = _setup(tmp_path, _blob)
    (tmp_path / "a.py").write_text("x = 2\n", encoding="utf-8")
    refusal, _, _ = _gate(tmp_path, monkeypatch, run_dir)
    assert refusal["refused"] == "ledger-unmet"
    assert any(f["reason"] == "measured-rev-drift: a.py" for f in refusal["ledger_failures"])


def test_refuses_met_on_unreadable_record(tmp_path, monkeypatch):
    run_dir = _setup(tmp_path, _blob)
    (run_dir / "measured-rev.json").write_text("{not json", encoding="utf-8")
    refusal, _, _ = _gate(tmp_path, monkeypatch, run_dir)
    assert refusal["refused"] == "ledger-unmet"
    assert any(f["reason"] == "measured-rev-record-unreadable" for f in refusal["ledger_failures"])


def test_passes_on_match(tmp_path, monkeypatch):
    run_dir = _setup(tmp_path, _blob)
    refusal, _, _ = _gate(tmp_path, monkeypatch, run_dir)
    assert refusal is None


def test_unchanged_without_record(tmp_path, monkeypatch):
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    run_dir = tmp_path / "state" / "warp" / RID
    run_dir.mkdir(parents=True)
    refusal, _, _ = _gate(tmp_path, monkeypatch, run_dir)
    assert refusal is None

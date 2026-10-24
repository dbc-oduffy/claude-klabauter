"""land_run's ledger pre-existing-dirt check: an absent path and a path the run's own checkpoint
wrote are not dirt; a claimed path holding foreign dirt still refuses."""

from __future__ import annotations

import json
from types import SimpleNamespace

from coordinator_core.ops.dispatch_emit import land_run, measured_rev, own_checkpoint_paths, requirements_ledger
from coordinator_core.ops.dispatch_emit.write_baseline import capture


def _gate(tmp_path, monkeypatch, paths, own=()):
    for p in paths:
        if p.startswith("present"):
            (tmp_path / p).write_text("x")
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({
        "write_baseline": capture(tmp_path, paths),
        "rows": [{"register_rows": ["R4"], "writes": list(paths)}],
    }))
    monkeypatch.setattr(requirements_ledger, "read_ledger", lambda *_a, **_k: SimpleNamespace(items=[], all_of_pm_ask=False))
    monkeypatch.setattr(
        requirements_ledger, "judge", lambda *_a, **_k: {"pass": True, "failures": [], "open": []},
    )
    monkeypatch.setattr(requirements_ledger, "noted_paths", lambda *_a, **_k: [])
    monkeypatch.setattr(measured_rev, "measured_rev_drift", lambda *_a, **_k: [])
    monkeypatch.setattr(
        own_checkpoint_paths, "checkpoint_landed_paths", lambda *_a, **_k: frozenset(own),
    )
    return land_run._ledger_gate(
        tmp_path, run_dir, {"items": [{"id": "R4", "status": "met"}]}, manifest, [], {}, None,
        {"prep": {"run_base_sha": "abc"}}, None, "docs/plans/p.md",
    )[0]


def test_foreign_dirt_still_refuses(tmp_path, monkeypatch):
    out = _gate(tmp_path, monkeypatch, ["present.py"])
    assert out is not None and out["refused"] == "ledger-unmet", out
    assert "pre-existing-dirt: present.py" in json.dumps(out)


def test_absent_path_is_not_dirt(tmp_path, monkeypatch):
    assert _gate(tmp_path, monkeypatch, ["absent_test.py"]) is None


def test_path_written_by_own_checkpoint_is_not_dirt(tmp_path, monkeypatch):
    assert _gate(tmp_path, monkeypatch, ["present.py"], own=["present.py"]) is None

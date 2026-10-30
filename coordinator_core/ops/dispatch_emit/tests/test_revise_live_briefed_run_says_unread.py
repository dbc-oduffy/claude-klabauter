"""workflow.revise on a live run: says the revise is unread once rows are briefed, silent before."""

from __future__ import annotations

import json

import yaml

from coordinator_core.ops.dispatch_emit import requirements_ledger as rl
from coordinator_core.ops.dispatch_emit import run_token
from coordinator_core.ops.dispatch_emit.ask_contract import record_dir

RUN = "ask-20261015T000000-abc123"
REL = "state/sizings/2026-10-15-revise.yaml"


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


def _revise(root, monkeypatch):
    monkeypatch.setattr(run_token, "is_live", lambda *_a, **_k: True)
    return rl._handler({"run_id": RUN, "verbatim": "also cover Windows"}, repo_root=root)


def test_briefed_live_run_says_the_revise_will_not_be_read(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    manifest = {"rows": [{"id": "X1", "brief_path": "scratch/warp/x/briefs/X1.md"}]}
    (record_dir(root, RUN) / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    out = _revise(root, monkeypatch)
    assert out["run_live"] is True and out["briefed"] is True
    assert "no row brief reads this revision" in out["note"]


def test_unbriefed_live_run_names_the_late_row_leg(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    out = _revise(root, monkeypatch)
    assert out["run_live"] is True
    assert "briefed" not in out and "late-row leg" in out["note"]
    assert out["item"]


def test_manifest_without_briefs_is_unbriefed(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    (record_dir(root, RUN) / "manifest.json").write_text(json.dumps({"rows": []}), encoding="utf-8")
    out = _revise(root, monkeypatch)
    assert "briefed" not in out

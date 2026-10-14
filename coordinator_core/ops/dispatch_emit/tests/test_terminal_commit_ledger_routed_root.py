"""The terminal ledger gate refuses an engine-routed item judged at another root."""

from __future__ import annotations

import json

import pytest
import yaml

from coordinator_core.ops.dispatch_emit import requirements_ledger as rl
from coordinator_core.ops.dispatch_emit import terminal_commit as tc
from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT

RUN = "ask-20261014T000000-abc123"
REL = "state/sizings/2026-10-14-routed.yaml"


@pytest.fixture
def run(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / "state" / "sizings").mkdir(parents=True)
    doc = {
        "schema": "sizing-object", "intent": "x", "estimate": {"tshirt": "S", "provisional": True},
        "route": "dispatch", "detents": [], "fork": None, "xl_exit": None, "status": "sized",
        "premise": {"provenance": "not-applicable", "evidence": "fixture"},
        "pm_verbatims": [{"turn": 1, "text": "deliver sizing.intake_mint in the engine"}],
    }
    (tmp_path / REL).write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    rl.start(tmp_path, RUN)
    rl.bind(tmp_path, RUN, REL)
    return tmp_path, tmp_path / RECORD_DIR_ROOT / RUN


def _manifest(run_dir, engine_root):
    path = run_dir / "manifest.json"
    path.write_text(
        json.dumps({"engine_rows": [{"id": "R1", "owner_root": engine_root, "engine": True}]}), encoding="utf-8"
    )
    return path


def _gate(run, verdict, manifest=None):
    return tc._ledger_gate(run[0], run[1], verdict, manifest)[0]


def test_engine_routed_item_judged_at_the_cwd_is_refused(run, tmp_path):
    engine = (tmp_path / "engine").as_posix()
    manifest = _manifest(run[1], engine)
    refusal = _gate(run, {"items": [{"id": "R1", "status": "met", "wired": True, "root_read": run[0].as_posix()}]}, manifest)
    assert refusal["refused"] == "ledger-unmet"
    assert "owed the engine root" in refusal["ledger_failures"][0]["reason"]


def test_engine_routed_item_judged_at_the_engine_root_passes(run, tmp_path):
    engine = tmp_path / "engine"
    engine.mkdir()
    manifest = _manifest(run[1], engine.as_posix())
    assert _gate(run, {"items": [{"id": "R1", "status": "met", "wired": True, "root_read": [str(engine)]}]}, manifest) is None


def test_no_manifest_or_unreadable_manifest_is_the_cwd_only_gate(run):
    verdict = {"items": [{"id": "R1", "status": "met", "wired": True}]}
    assert _gate(run, verdict) is None
    bad = run[1] / "manifest.json"
    bad.write_text("{not json", encoding="utf-8")
    assert _gate(run, verdict, bad) is None
    assert _gate(run, verdict, run[1] / "missing.json") is None

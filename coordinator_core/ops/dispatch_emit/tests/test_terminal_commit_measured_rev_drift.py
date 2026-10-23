"""The terminal ledger gate refuses a met item whose measured blobs drifted from the worktree."""

from __future__ import annotations

import json

import pytest
import yaml

from coordinator_core.git import commit
from coordinator_core.ops.dispatch_emit import requirements_ledger as rl
from coordinator_core.ops.dispatch_emit import terminal_commit as tc
from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT, measured_rev_record_path

RUN = "ask-20261014T000000-abc123"
REL = "state/sizings/2026-10-14-drift.yaml"
VERDICT = {"items": [{"id": "R1", "status": "met", "wired": True}]}


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
    (tmp_path / "a.txt").write_text("one\n")
    return tmp_path, tmp_path / RECORD_DIR_ROOT / RUN


def _record(root):
    path = measured_rev_record_path(root, RUN)
    path.write_text(json.dumps({
        "sha": "s", "base_sha": "b", "paths": ["a.txt"], "removed": [],
        "blobs": {"a.txt": commit.checkin_blob_sha(root, "a.txt")},
    }), encoding="utf-8")


def _gate(run):
    return tc._ledger_gate(run[0], run[1], VERDICT)[0]


def test_matching_record_passes(run):
    _record(run[0])
    assert _gate(run) is None


def test_drifted_blob_refuses_naming_the_path(run):
    _record(run[0])
    (run[0] / "a.txt").write_text("two\n")
    refusal = _gate(run)
    assert refusal["refused"] == "ledger-unmet"
    assert "measured-rev-drift" in refusal["ledger_failures"][0]["reason"]
    assert "a.txt" in refusal["ledger_failures"][0]["reason"]


def test_no_record_is_unchanged(run):
    assert _gate(run) is None


def test_drift_does_not_refuse_a_verdict_without_a_met_item(run):
    _record(run[0])
    (run[0] / "a.txt").write_text("two\n")
    verdict = {"items": [{"id": "R1", "status": "not_met", "reason": "x"}]}
    refusal = tc._ledger_gate(run[0], run[1], verdict)[0]
    assert "measured-rev-drift" not in json.dumps(refusal)

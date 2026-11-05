"""A register row lands wired only with its consumer command and output: both writers read the
verdict item's `{command, observed}` and record `wired_evidence: {command, output}`; wired with none
lands unwired and partial."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
import yaml

from coordinator_core.ops import requirement_register as rr
from coordinator_core.ops.dispatch_emit import requirements_ledger as rl

PLAN = "docs/plans/2026-11/p.md"
REL = "state/sizings/2026-11-04-evidence.yaml"
NOW = datetime(2026, 11, 4, 12, 0, tzinfo=timezone.utc)
EVIDENCE = {"command": "python -m x --census", "observed": "R1 reached: 3 consumers"}


def _sizing_text(status="open"):
    row = {"id": "R1", "source_text": "wire it", "anchor": "wire it", "surface": "cli",
           "status": status, "claimed_by": [PLAN], "wired": False, "met_by": []}
    doc = {
        "schema": "sizing-object", "intent": "build the op", "estimate": {"tshirt": "S", "provisional": True},
        "route": "dispatch", "detents": [], "fork": None, "xl_exit": None, "status": "sized",
        "premise": {"provenance": "not-applicable", "evidence": "fixture"},
        "requirement_register": {"rows": [row]},
    }
    return yaml.safe_dump(doc, sort_keys=False)


def _ledger_row(tmp_path, item):
    (tmp_path / "state" / "sizings").mkdir(parents=True)
    (tmp_path / REL).write_text(_sizing_text(), encoding="utf-8")
    (tmp_path / ".git").mkdir()
    rl.apply_verdict(tmp_path, REL, {"items": [item]}, run_id="run-1", sha="abc1234")
    return rr.read_register((tmp_path / REL).read_text(encoding="utf-8")).rows[0]


def _register_row(item):
    out = rr.apply_verdicts(_sizing_text(), [item], PLAN, "abc1234", NOW)
    return rr.read_register(out).rows[0]


@pytest.mark.parametrize("land", [_ledger_row, lambda _tmp, item: _register_row(item)], ids=["ledger", "register"])
class TestBothWriters:
    def test_wired_with_evidence_records_command_and_output(self, tmp_path, land):
        row = land(tmp_path, {"id": "R1", "status": "met", "wired": True, **EVIDENCE})
        assert row["status"] == "met" and row["wired"] is True
        assert row["wired_evidence"] == {"command": EVIDENCE["command"], "output": EVIDENCE["observed"]}

    @pytest.mark.parametrize("missing", [{"command": ""}, {"observed": "  "}, {"command": None}, {"observed": 3}])
    def test_wired_with_incomplete_evidence_lands_unwired_partial(self, tmp_path, land, missing):
        row = land(tmp_path, {"id": "R1", "status": "met", "wired": True, **{**EVIDENCE, **missing}})
        assert row["status"] == "partial" and row["wired"] is False
        assert "wired_evidence" not in row

    def test_wired_with_no_evidence_lands_unwired_partial(self, tmp_path, land):
        row = land(tmp_path, {"id": "R1", "status": "met", "wired": True})
        assert row["status"] == "partial" and row["wired"] is False
        assert "wired_evidence" not in row

    def test_evidence_without_wired_true_records_nothing(self, tmp_path, land):
        row = land(tmp_path, {"id": "R1", "status": "met", "wired": False, **EVIDENCE})
        assert row["wired"] is False and "wired_evidence" not in row


def test_wired_evidence_requires_both_non_empty_strings():
    assert rr.wired_evidence(EVIDENCE) == {"command": EVIDENCE["command"], "output": EVIDENCE["observed"]}
    assert rr.wired_evidence({"command": "x"}) is None

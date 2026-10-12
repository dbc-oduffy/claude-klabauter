"""Receipt renderer: owed decisions, failed runs, behind index, banned phrasing, heading fence."""
from __future__ import annotations

import re

import pytest

from coordinator_core.architecture_survey import receipt
from coordinator_core.architecture_survey.contract import owed_decisions

NEED = {"covered": True, "door": "project_x", "calls": 1, "reason": None, "payload": None}


def _structure(state="current", behind=0):
    needs = {n: dict(NEED) for n in ("system-map", "edges", "file-index", "dead-paths", "last-touch")}
    needs["dead-paths"]["payload"] = {"a.md": [{"path": "gone.py", "last_touch": "abc123"}, "x.py"]}
    return {
        "run_id": "20261012T100000Z",
        "index": {"commit": "deadbeef", "commits_behind": behind, "state": state,
                  "dropped_paths": 2, "unindexed": ["u.py"]},
        "needs": needs,
        "systems": [{"name": "a", "file_count": 3, "line_count": 9, "fingerprint": "f"}],
    }


def _digest(mode="refresh", source="rag"):
    return {"run_id": "20261012T100000Z", "mode": mode, "source": source, "consume_gate": None,
            "existing_atlas": {"systems": [{"name": "a", "page": "a.md", "last_mapped": "2026-01-01"},
                                           {"name": "zz", "page": "zz.md", "last_mapped": None}]}}


def _rows(mode="refresh", source="rag", skip=()):
    return [{"id": d, "chosen": "ok", "rule": "r", "evidence": "e"}
            for d in owed_decisions(mode, source) if d not in skip]


def _agents():
    return {"pages_written": ["a.md"], "decisions": [], "agents": {"count": 2, "tokens": 500},
            "clarity": [{"page": "a.md", "verdict": "revise", "findings": ["vague"]},
                        {"page": "b.md", "verdict": "pass", "findings": []}],
            "dead_paths_repaired": [{"page": "a.md", "path": "x.py", "action": "removed"}],
            "manifest": {}}


def _render(**kw):
    args = dict(ledger_rows=[{"step": "source", "status": "ok", "spawns": 2, "process_ms": 70,
                              "cost_class": "engine"}],
                digest=_digest(), structure=_structure(), agent_results=_agents(),
                decisions=_rows(), state="ran", reason="")
    args.update(kw)
    return receipt.render(args["ledger_rows"], args["digest"], args["structure"],
                          args["agent_results"], args["decisions"], state=args["state"],
                          reason=args["reason"])


def test_complete_run_renders_every_owed_decision():
    out = _render()
    for d in owed_decisions("refresh", "rag"):
        assert f"| {d} |" in out
    assert [h for h in re.findall(r"^### (.+)$", out, re.M)] == ["Outcome", "Decisions", "Findings", "Run cost"]
    assert "Agents: 2; tokens 500" in out
    assert "x.py: repaired (removed)" in out and "gone.py: left, last touched abc123" in out


def test_zero_agent_run_renders():
    out = _render(agent_results=None)
    assert "0 systems mapped" in out and "Agents:" not in out


def test_behind_index_named_in_findings():
    out = _render(structure=_structure("behind", 4))
    assert "behind, 4 behind HEAD" in out and "not-clean" in out


def test_dropping_a_decision_raises():
    with pytest.raises(ValueError, match="clarity"):
        _render(decisions=_rows(skip=("clarity",)))


def test_failed_run_renders_with_reason():
    out = _render(state="failed", reason="rag door timed out", decisions=_rows()[:2], agent_results=None,
                  ledger_rows=[{"step": "structure", "status": "failed", "reason": "rag door timed out"}])
    assert "failed at step structure: rag door timed out" in out
    assert "### Not run" in out


def test_not_run_row_reads_not_clean():
    rows = [r for r in _rows() if r["id"] != "attestation"]
    rows.append({"id": "attestation", "chosen": "not-run", "rule": "not-composed", "evidence": ""})
    assert "not-clean" in _render(decisions=rows)


def test_orphan_and_stale_pages_found():
    out = _render()
    assert "Orphan pages: zz.md" in out and "a.md (284 days)" in out


def test_no_action_inviting_text():
    out = _render(structure=_structure("behind", 4),
                  ledger_rows=[{"step": "x", "status": "not-composed", "reason": "rag-present"}])
    for word in ("resume", "Parked", "Next up", "Still to do", "awaiting"):
        assert word not in out


def test_heading_outside_tuple_cannot_be_produced():
    with pytest.raises(ValueError):
        receipt._heading("Parked")
    assert receipt.SECTIONS == ("Outcome", "Decisions", "Findings", "Not run", "Run cost")

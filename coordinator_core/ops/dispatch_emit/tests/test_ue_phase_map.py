import json

import pytest

from coordinator_core.ops.dispatch_emit import ue_phase_map as m


@pytest.mark.parametrize(
    "agent_type,phase",
    [
        ("coordinator:sizing-scout", "sizing"),
        ("coordinator:ue-corpus-checker", "planning"),
        ("coordinator:plan-author", "planning"),
        ("coordinator:plan-reviewer", "review"),
        ("coordinator:code-reviewer", "review"),
        ("coordinator:staff-eng", "review"),
        ("coordinator:executor", "execution"),
        ("sizing-scout", "sizing"),
        ("staff-eng", "review"),
    ],
)
def test_mapped_types(agent_type, phase):
    assert m.phase_of(agent_type) == phase


@pytest.mark.parametrize("agent_type", [None, "", "  ", "general-purpose", "coordinator:general-purpose", "other:executor", 7])
def test_unmapped_is_none(agent_type):
    assert m.phase_of(agent_type) is None


def test_required_phases_and_frozen_map():
    assert m.REQUIRED_PHASES == ("sizing", "planning", "review")
    with pytest.raises(TypeError):
        m.AGENT_TYPE_PHASE["x"] = "sizing"


def _write(root, lines):
    d = root / ".coordinator-local"
    d.mkdir(parents=True)
    (d / "ue-research-gate-metrics.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _row(run_id, decision="retrieval", agent_type="coordinator:plan-author"):
    return json.dumps(
        {"ts": "t", "agent_id": "a", "run_id": run_id, "session_id": "s", "agent_type": agent_type, "decision": decision, "tool": "x"}
    )


def test_read_filters_run_and_skips_malformed(tmp_path):
    _write(tmp_path, [_row("r1"), "{not json", "[1]", _row("r2"), _row("r1", "deny", None), _row("r1", "bogus")])
    rows = m.read_gate_metrics(tmp_path, "r1")
    assert [r["decision"] for r in rows] == ["retrieval", "deny"]
    assert set(rows[0]) == set(m.GATE_METRICS_FIELDS)
    assert rows[1]["agent_type"] is None


def test_read_missing_file_is_empty(tmp_path):
    assert m.read_gate_metrics(tmp_path, "r1") == []


def test_read_tail_is_bounded(tmp_path):
    _write(tmp_path, [_row("r1", agent_type="old")] * 50 + [_row("r1", agent_type="new")])
    rows = m.read_gate_metrics(tmp_path, "r1", max_bytes=len(_row("r1")) * 3)
    assert rows and rows[-1]["agent_type"] == "new" and len(rows) < 51

"""run_spend: ceremony_of stem table, spend_summary, record_run over a synthetic wf_ dir."""

import json

import pytest

from coordinator_core.usage import run_spend
from coordinator_core.usage.run_spend import ceremony_of, record_run, spend_summary

OPUS = "claude-opus-5-5-20260101"


def _wf(tmp_path):
    wf = tmp_path / "wf_x"
    wf.mkdir()
    lines = [{"type": "launched"}] + [
        {"type": "started", "agentId": a, "key": f"k{a}", "label": label}
        for a, label in (("a1", "executor C0"), ("a2", "executor C1"))]
    (wf / "journal.jsonl").write_text("\n".join(json.dumps(x) for x in lines) + "\n")
    for a in ("a1", "a2"):
        msg = {"id": f"m{a}", "model": OPUS,
               "usage": {"input_tokens": 100, "output_tokens": 50,
                         "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}}
        rows = [{"type": "user", "message": {"role": "user"}},
                {"type": "assistant", "requestId": f"r{a}", "message": msg}]
        (wf / f"agent-{a}.jsonl").write_text("\n".join(json.dumps(x) for x in rows) + "\n")
    return wf


@pytest.mark.parametrize("path,expected", [
    ("coordinator/pipelines/artifact-distillation/distill-harvest.workflow.js", "distill"),
    ("coordinator/pipelines/deep-architecture-survey/survey.workflow.js", "atlas"),
    ("x/update-docs-run.mjs", "update-docs"),
    ("x/lessons.workflow.js", "learn-lessons"),
    ("x/learn-lessons-abc.js", "learn-lessons"),
    ("x/plan-blitz.workflow.js", "review"),
    ("x/ask-123.js", "emit"),
    ("x/research.workflow.js", "research"),
    ("x/mystery.workflow.js", "other"),
    (None, "other"),
    ("", "other"),
])
def test_ceremony_of_table(path, expected):
    assert ceremony_of(path) == expected


def test_record_run_writes_spend_row(tmp_path):
    wf = _wf(tmp_path)
    repo = tmp_path / "repo"
    repo.mkdir()
    out = record_run(str(wf), str(repo), "run-1", "distill",
                     script_path="a/distill-harvest.workflow.js")
    assert "reason" not in out
    row = json.loads((repo / out["spend_row"]).read_text())
    assert out["spend_row"].startswith("state/spend/") and out["spend_row"].endswith("/run-1.json")
    assert row["schema"] == "ceremony-spend/1"
    assert row["ceremony"] == "distill"
    assert row["script_stem"] == "distill-harvest"
    assert row["agents"] == 2
    assert row["tokens"] == 300
    assert row["usd"] > 0
    assert row["by_role"] and all("agents" in b for b in row["by_role"].values())
    assert sum(b["agents"] for b in row["by_role"].values()) == 2


def test_record_run_reuses_passed_scan(tmp_path, monkeypatch):
    scan = {"agents": [{}], "by_model": {}, "by_role": {"r": {"agents": 1}},
            "run_total": {"total_tokens": 7, "usd": 0.5}}
    repo = tmp_path / "repo"
    repo.mkdir()
    out = record_run(str(tmp_path / "nowhere"), str(repo), "run-2", "other", scan=scan)
    assert out["summary"] == spend_summary(scan)
    assert out["summary"]["tokens"] == 7 and out["summary"]["agents"] == 1
    assert json.loads((repo / out["spend_row"]).read_text())["script_stem"] is None


def test_record_run_never_raises(tmp_path):
    assert "reason" in record_run(str(tmp_path / "missing"), str(tmp_path), "../evil", "other")
    assert "reason" in record_run(str(tmp_path), str(tmp_path), "ok", "other", scan={"agents": 1})


def test_record_run_write_failure_returns_reason(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise OSError("disk")
    monkeypatch.setattr("coordinator_core.session.claimed_write.replace_text", boom)
    out = record_run(str(tmp_path), str(tmp_path), "run-3", "other",
                     scan={"agents": [], "by_role": {}, "by_model": {}, "run_total": {}})
    assert "disk" in out["reason"]
    assert run_spend.SPEND_SCHEMA == "ceremony-spend/1"

"""_watch at default arguments leaves a run-cost record and a ceremony-keyed spend row per Workflow run."""

from __future__ import annotations

import json

import pytest

from coordinator_core import workflow_watch as ww


class _Rec:
    status = "completed"
    result_text = ""


class _Watcher:
    def __init__(self, *_a):
        pass

    def check_record(self):
        return _Rec()


def _usage(inp, out):
    return {"input_tokens": inp, "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0, "output_tokens": out}


def _wf_dir(root, run_id):
    wf = root / "runs" / run_id
    wf.mkdir(parents=True)
    agents = [("a1", "work:C1", "sonnet"), ("a2", "verify:C1", "haiku"), ("a3", "work:C2", "sonnet")]
    lines = [{"type": "launched"}]
    for agent_id, label, model in agents:
        lines.append({"type": "started", "agentId": agent_id, "label": label})
        (wf / f"agent-{agent_id}.meta.json").write_text(
            json.dumps({"agentType": "t", "model": model}), encoding="utf-8")
        events = [{"type": "user", "timestamp": "2026-11-13T10:00:00Z", "message": {"role": "user"}},
                  {"type": "assistant", "timestamp": "2026-11-13T10:01:00Z",
                   "message": {"id": "m0", "usage": _usage(1000, 200)}}]
        (wf / f"agent-{agent_id}.jsonl").write_text(
            "\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")
    (wf / "journal.jsonl").write_text("\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")
    return wf


@pytest.fixture
def repo(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    (root / "scratch" / "warp").mkdir(parents=True)
    ratchet_dir = root / "coordinator_core" / "workflow_watch"
    ratchet_dir.mkdir(parents=True)
    (ratchet_dir / "run_cost_ratchet.json").write_text(json.dumps(
        {"executor_max_same_file_reads": 32, "executor_peak_context": 366000,
         "executor_tool_calls": 254}), encoding="utf-8")
    monkeypatch.chdir(root)
    monkeypatch.setattr(ww, "TerminalWatcher", _Watcher)
    monkeypatch.setattr(ww, "stamp_terminal", lambda *a: None)
    return root


def _spend_rows(root):
    return sorted((root / "state" / "spend").glob("*/*.json"))


@pytest.mark.parametrize("stem,ceremony", [("distill-harvest", "distill"), ("survey", "atlas")])
def test_watch_leaves_run_cost_and_ceremony_spend_row(repo, stem, ceremony):
    wf = _wf_dir(repo, f"wf_{ceremony}")
    script = str(repo / "scripts" / f"{stem}.workflow.js")
    assert ww._watch("t", str(wf / "journal.jsonl"), "tid", 0.0, 1.0, script_path=script) == 0

    record = json.loads((wf / "run-cost.json").read_text(encoding="utf-8"))
    assert record["agents"] == 3
    assert set(record["by_role"]) == {"work", "verify"} and record["by_role"]["work"]["agents"] == 2
    assert set(record["by_model"]) == {"sonnet", "haiku"}

    (row_path,) = _spend_rows(repo)
    row = json.loads(row_path.read_text(encoding="utf-8"))
    assert row["ceremony"] == ceremony and row["run_id"] == f"wf_{ceremony}"
    assert row["tokens"] == 3 * 1200 and row["agents"] == 3
    assert isinstance(row["usd"], float)
    assert set(row["by_role"]) == {"work", "verify"}


def test_no_script_path_still_writes_other_row(repo):
    wf = _wf_dir(repo, "wf_plain")
    assert ww._watch("t", str(wf / "journal.jsonl"), "tid", 0.0, 1.0) == 0
    (row_path,) = _spend_rows(repo)
    assert json.loads(row_path.read_text(encoding="utf-8"))["ceremony"] == "other"


def test_no_repo_root_skips_spend_row_and_exit_stays_zero(repo, tmp_path, monkeypatch):
    wf = _wf_dir(repo, "wf_norepo")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    assert ww._watch("t", str(wf / "journal.jsonl"), "tid", 0.0, 1.0) == 0
    assert (wf / "run-cost.json").is_file()
    assert _spend_rows(repo) == [] and not (elsewhere / "state").exists()


def test_watch_tightens_only_the_resolved_repo_ratchet(repo):
    wf = _wf_dir(repo, "wf_exec")
    # an executor-labelled agent with tool calls gives a positive observed value below the ceiling
    (wf / "agent-a1.jsonl").write_text("\n".join(json.dumps(e) for e in [
        {"type": "user", "timestamp": "2026-11-13T10:00:00Z", "message": {"role": "user"}},
        {"type": "assistant", "timestamp": "2026-11-13T10:01:00Z",
         "message": {"id": "m0", "usage": _usage(1000, 200),
                     "content": [{"type": "tool_use", "name": "Read", "input": {"file_path": "A.py"}}]}},
    ]) + "\n", encoding="utf-8")
    ratchet = repo / "coordinator_core" / "workflow_watch" / "run_cost_ratchet.json"
    ww._watch("t", str(wf / "journal.jsonl"), "tid", 0.0, 1.0)
    after = json.loads(ratchet.read_text(encoding="utf-8"))
    assert after["executor_peak_context"] == 1000
    assert after["executor_tool_calls"] < 254

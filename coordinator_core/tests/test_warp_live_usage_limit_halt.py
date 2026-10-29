"""warp_live: a run whose every unsettled leg ends on a 429 reads not live and leaves a usage_limit halt."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from coordinator_core import warp_live
from coordinator_core.ops.dispatch_emit import halted_run, run_token
from coordinator_core.ops.dispatch_emit.ask_contract import record_dir

SIZING = "state/sizings/mine.yaml"
SCRIPT = "scratch/warp/run.workflow.mjs"
RUN = "warp-mine-20261021T191648-bb4d27"


FIXTURE = json.loads(
    (Path(__file__).parents[1] / "workflow_watch" / "tests" / "fixtures" / "usage_limit_hit.json").read_text(
        encoding="utf-8"
    )
)


def _limit_turn(resets_at: int) -> str:
    """The recorded 429 turn with its `resetsAt` replaced."""
    fixture = json.loads(json.dumps(FIXTURE))
    fixture["quotaLimits"]["resetsAt"] = resets_at
    return json.dumps({
        "type": "assistant",
        "message": {"model": "<synthetic>", "content": [{"type": "text", "text": "You've hit your session limit"}]},
        "isApiErrorMessage": True,
        **fixture,
    })


@pytest.fixture
def repo(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    managed = tmp_path / "managed"
    legacy = tmp_path / "legacy"
    managed.mkdir()
    legacy.mkdir()
    monkeypatch.setattr(warp_live, "managed_tempdir", lambda: managed)
    monkeypatch.setattr(warp_live, "legacy_top_level_path", lambda name: legacy / name)
    run_token.mint(root, RUN, SCRIPT, session_id="s1", sizing_rel=SIZING)
    warp_live.record_submission(
        root, RUN, session_id="s1", transcript_path=None, script_path=SCRIPT, sizing_rel=SIZING
    )
    wf = tmp_path / "wf" / "run1"
    wf.mkdir(parents=True)
    monkeypatch.setattr(warp_live, "_run_transcript_dir", lambda *a, **k: str(wf))
    calls = []
    monkeypatch.setattr(halted_run, "_RESUME_SPAWNER", lambda r, i: calls.append(i))
    return root, wf, calls


def _journal(wf, last_line):
    (wf / "journal.jsonl").write_text(
        json.dumps({"type": "started", "agentId": "a1", "label": "leg"}) + "\n", encoding="utf-8"
    )
    (wf / "agent-a1.jsonl").write_text(last_line + "\n", encoding="utf-8")


def _live(root):
    return warp_live.live_run_for_sizing(root, SIZING, session_id="s1")


@pytest.mark.parametrize("resets_at", [4102444800, 1000000000])
def test_all_429_run_reads_not_live_and_writes_halt(repo, resets_at):
    root, wf, calls = repo
    _journal(wf, _limit_turn(resets_at))
    assert _live(root) is None
    marker = halted_run.read_strict(record_dir(root, RUN))
    assert marker["status"] == "halted" and marker["halted"] == "usage_limit"
    assert run_token.is_live(root, RUN)
    assert calls == [RUN]
    assert _live(root) is None
    assert calls == [RUN]


def test_non_429_run_still_reads_live(repo):
    root, wf, _ = repo
    _journal(wf, json.dumps({"type": "assistant", "message": {"content": []}}))
    assert _live(root) == RUN
    assert halted_run.read_strict(record_dir(root, RUN)) is None


def test_run_with_no_journal_reads_live(repo):
    root, _, _ = repo
    assert _live(root) == RUN
    assert halted_run.read_strict(record_dir(root, RUN)) is None

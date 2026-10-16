"""End-to-end: `sizing-assemble --write --research-class <c>` never leaves a plan-terminal obligation.

Drives the real write path (not a hand-written sizing), then the intake drain, a Workflow fire and
a Stop payload.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import coordinator_core.sizing_assemble as sa
from coordinator_core.hooks.watchdog_undischarged_next_move import _RESEARCH_TERMINAL, _handler
from coordinator_core.session import machinery_paths, record_homes

_SID = "22222222-3333-4444-5555-666666666666"
_NAME = "2026-10-16-repro.yaml"
_DRAFT = """schema: sizing-object
intent: "Research the thing"
estimate:
  tshirt: M
  provisional: true
route: dispatch
detents: []
fork: null
xl_exit: null
status: draft
premise:
  provenance: unrecorded
  evidence: PLACEHOLDER - cite the evidence
deliverable_id: null
"""


@pytest.fixture()
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    (tmp_path / ".git").mkdir()
    Path(record_homes.home_dir(str(tmp_path), "sizings")).mkdir(parents=True)
    monkeypatch.chdir(tmp_path)
    for name in ("COORDINATOR_SESSION_ID", "CLAUDE_SESSION_ID"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", _SID)
    return tmp_path


def _rows(repo: Path) -> list:
    ledger = machinery_paths.ledger_path(str(repo), _SID)
    try:
        with open(ledger, "r", encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]
    except OSError:
        return []


def _open_sizing_rows(repo: Path) -> list:
    return [r for r in _rows(repo) if r.get("seam") == "sizing-routed" and r.get("discharged_at") is None]


def _hook(repo: Path, event: str, **extra) -> dict:
    payload = {"session_id": _SID, "cwd": str(repo), "hook_event_name": event, **extra}
    return _handler({"payload": payload})


@pytest.mark.parametrize("value_class", ["scouts", "corpus", "deep", "spike"])
def test_research_class_write_opens_research_terminal_and_fire_discharges(repo, value_class) -> None:
    path = Path(record_homes.record_path(str(repo), "sizings", _NAME))
    path.write_text(_DRAFT, encoding="utf-8")
    rel = Path(record_homes.record_path("", "sizings", _NAME)).as_posix()
    code = sa.main(
        [
            "--tshirt", "M", "--interaction-mode", "pm", "--write", rel, "--json",
            "--research-class", value_class,
            "--research-source", "notebooklm", "--research-source", "web",
        ]
    )
    assert code == 0

    assert _hook(repo, "PostToolUse", tool_name="Bash", tool_input={"command": "ls"}) == {}
    opened = _open_sizing_rows(repo)
    assert len(opened) == 1
    assert opened[0]["next_action"] == _RESEARCH_TERMINAL

    fire = {"scriptPath": "scratch/warp/pipeline-research-x-1.workflow.mjs"}
    assert _hook(repo, "PostToolUse", tool_name="Workflow", tool_input=fire) == {}
    assert _open_sizing_rows(repo) == []

    stop = _hook(repo, "Stop")
    assert "never invoked" not in json.dumps(stop)

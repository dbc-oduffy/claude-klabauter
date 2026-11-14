"""A source:pm asks item binds the recorded PM turn its text equals (whitespace-normalised), searched over
every recorded turn; one matching none is refused naming source pm, never rebound to the newest turn."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from coordinator_core.ops import baton_pm_turns as PT
from coordinator_core.ops.dispatch_emit import ask_compose
from coordinator_core.ops.dispatch_emit import ask_fanout as fanout_mod
from coordinator_core.session_baton import store

pytestmark = pytest.mark.complete

SID = "sess-pm-binds-matching-turn"
OLDER = "make the hair   softer and\nkeep the tattoo metadata intact"
NEWER = "now also rename the exporter and drop the legacy flag"
_BLITZ = "export const meta = { phases: [{ title: 'Plan' }] };\nreturn { ready: [] };\n"


@pytest.fixture
def repo(tmp_path, monkeypatch):
    for name in ("CLAUDE_CODE_REMOTE", "CLAUDE_CODE_REMOTE_SESSION_ID", "CLAUDE_CODE_CONTAINER_ID",
                 "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_PLUGIN_ROOT", "DELIVERABLE_ID"):
        monkeypatch.delenv(name, raising=False)
    root = tmp_path / "repo"
    (root / "state" / "sizings").mkdir(parents=True)
    git = root / ".git"
    (git / "objects").mkdir(parents=True)
    (git / "refs").mkdir()
    (git / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (tmp_path / "hub" / SID).mkdir(parents=True)
    monkeypatch.setattr(store, "baton_dir", lambda s, cwd=None: tmp_path / "hub" / s)
    monkeypatch.setattr(ask_compose, "_read_plan_blitz", lambda: _BLITZ)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", SID)
    assert PT.append_turn(OLDER, session_id=SID)["ok"]
    assert PT.append_turn(NEWER, session_id=SID)["ok"]
    return root


def _submit(root: Path, asks: list[dict]) -> dict:
    return fanout_mod.fanout({"asks": asks, "session_id": SID}, root)


def _verbatims(root: Path, out: dict, i: int = 0) -> list[dict]:
    doc = yaml.safe_load((Path(out["writer_root"]) / out["ready"][i]["sizing"]).read_text(encoding="utf-8"))
    return doc.get("pm_verbatims") or []


def test_pm_ask_equal_to_an_older_turn_binds_that_turn_not_the_newest(repo):
    out = _submit(repo, [
        {"ask": "make the hair softer and keep the tattoo metadata intact", "source": "pm", "writes": ["a.py"]},
        {"ask": NEWER, "source": "pm", "writes": ["b.py"]},
    ])
    by_text = {v["text"]: v for i in range(2) for v in _verbatims(repo, out, i)}
    assert OLDER in by_text and NEWER in by_text
    assert by_text[OLDER]["turn"] == 0 and by_text[NEWER]["turn"] == 1


def test_pm_ask_matching_no_recorded_turn_is_refused_naming_source_pm(repo):
    out = _submit(repo, [{"ask": "words nobody said", "source": "pm", "writes": ["a.py"]}])
    assert out.get("exit_code") != 0 and "source pm" in str(out) and "relayed" in str(out)
    assert not list((repo / "state" / "sizings").rglob("*.yaml"))


def test_relayed_item_with_the_same_text_still_runs_relayed(repo):
    out = _submit(repo, [{"ask": NEWER, "source": "relayed", "writes": ["a.py"]}])
    assert out["applied"] is True and _verbatims(repo, out) == []

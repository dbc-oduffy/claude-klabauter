"""A relayed-ask sizing embeds no recency-fallback session turn as pm_verbatims; a quoted turn still binds."""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

import coordinator_core.ops.baton_pm_turns as pm_turns
import coordinator_core.sizing_assemble as sizing_assemble
from coordinator_core.session import record_homes
from coordinator_core.session_hub_path import session_dir_for
from coordinator_core.session_baton import store

_NAME = "2026-11-15-relayed.yaml"
_UNRELATED = "agy grok codex status bar line shows the wrong model name again today"
_ASKED = "please distill the relay slice into the cut list for the new warp plan"

_DRAFT = """schema: sizing-object
intent: "Ship the thing"
estimate:
  tshirt: XS
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
em_analysis:
  ask_source: relayed
"""


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    (tmp_path / ".git").mkdir()
    Path(record_homes.record_path(str(tmp_path), "sizings", _NAME)).parent.mkdir(parents=True)
    return tmp_path


@pytest.fixture()
def sid(repo: Path, monkeypatch) -> str:
    sdir = session_dir_for(repo / ".git", "sid-rl")
    sdir.mkdir(parents=True)
    monkeypatch.setattr(store, "baton_dir", lambda s, cwd=None: sdir.parent / s)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", "sid-rl")
    return "sid-rl"


def _run(repo: Path, intent: str, text: str, capsys) -> list:
    path = Path(record_homes.record_path(str(repo), "sizings", _NAME))
    path.write_text(text, encoding="utf-8")
    args = ["--tshirt", "M", "--intent", intent, "--premise-provenance", "read",
            "--premise-evidence", "read it", "--write", str(path), "--json"]
    assert sizing_assemble.main(args) == 0
    capsys.readouterr()
    return yaml.safe_load(path.read_text(encoding="utf-8")).get("pm_verbatims") or []


def test_relayed_ask_embeds_no_unrelated_newer_turn(repo, sid, capsys) -> None:
    pm_turns.append_turn(_ASKED, session_id=sid)
    pm_turns.append_turn(_UNRELATED, session_id=sid)
    assert _run(repo, "Distill a slice of the relay, paraphrased", _DRAFT, capsys) == []


def test_relayed_ask_still_binds_a_turn_its_intent_quotes(repo, sid, capsys) -> None:
    pm_turns.append_turn(_ASKED, session_id=sid)
    pm_turns.append_turn(_UNRELATED, session_id=sid)
    rows = _run(repo, _ASKED, _DRAFT, capsys)
    assert [r["turn"] for r in rows] == [0]


def test_non_relayed_ask_keeps_the_recency_fallback(repo, sid, capsys) -> None:
    pm_turns.append_turn(_ASKED, session_id=sid)
    pm_turns.append_turn(_UNRELATED, session_id=sid)
    direct = _DRAFT.replace("em_analysis:\n  ask_source: relayed\n", "")
    rows = _run(repo, "Distill a slice of the relay, paraphrased", direct, capsys)
    assert [r["turn"] for r in rows] == [1]

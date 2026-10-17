"""sizing.intake_mint: newest-turn seed, optional em_brief, mint-twice guard, schema validity.

Run: python -m pytest coordinator_core/ops/tests/test_sizing_intake_mint.py -q
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from coordinator_core.frontmatter.schema_validate import validate_frontmatter
from coordinator_core.ops import baton_pm_turns as PT
from coordinator_core.ops import sizing_intake as mod
from coordinator_core.session_baton import store

SID = "sid-intake"


@pytest.fixture
def repo(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()
    sdir = tmp_path / "hub" / SID
    sdir.mkdir(parents=True)
    monkeypatch.setattr(store, "baton_dir", lambda s, cwd=None: tmp_path / "hub" / s)
    return tmp_path


def _turns(*prompts: str) -> None:
    for p in prompts:
        assert PT.append_turn(p, session_id=SID)["ok"]


def _mint(repo: Path, **params) -> dict:
    return mod._handler({"name": "Do The Thing", "session_id": SID, **params}, repo_root=repo)


def _doc(repo: Path, out: dict) -> dict:
    return yaml.safe_load((repo / out["sizing_path"]).read_text(encoding="utf-8"))


def test_seeds_only_the_newest_turn(repo):
    _turns("first ask", "second ask", "third and newest ask")
    out = _mint(repo)
    assert out["exit_code"] == 0 and out["turn"] == 2
    doc = _doc(repo, out)
    assert [v["text"] for v in doc["pm_verbatims"]] == ["third and newest ask"]
    assert doc["intent"] == "third and newest ask"
    assert out["sizing_path"].endswith("-do-the-thing.yaml")
    assert out["submit"] == (
        'Workflow({name: "coordinator:engine-ask", args: {sizing: "' + out["sizing_path"] + '"}})'
    )


def test_output_is_schema_valid(repo):
    _turns("ship it now please")
    out = _mint(repo, em_brief="keep it small")
    assert validate_frontmatter(_doc(repo, out), mod._SIZING_SCHEMA_PATH) == []


@pytest.mark.parametrize("brief,expect", [(None, None), ("", None), ("   ", None),
                                          ("  go wide  ", "go wide")])
def test_em_brief_is_optional_and_never_blocks(repo, brief, expect):
    _turns("an ask")
    params = {} if brief is None else {"em_brief": brief}
    out = _mint(repo, **params)
    assert out["exit_code"] == 0
    assert _doc(repo, out)["em_analysis"].get("em_brief") == expect


def test_no_recorded_turn_mints_relayed(repo):
    out = _mint(repo, source="relayed", sender="peer-x", relayed_text="please do X")
    assert out["exit_code"] == 0 and out["turn"] is None and out["provenance"] == "relayed"
    doc = _doc(repo, out)
    assert "pm_verbatims" not in doc
    assert doc["em_analysis"]["provenance"] == "relayed"
    assert doc["em_analysis"]["relayed_text"] == "please do X"
    assert doc["em_analysis"]["relayed_sender"] == "peer-x"
    assert doc["intent"] == "please do X"
    assert validate_frontmatter(doc, mod._SIZING_SCHEMA_PATH) == []


def test_turnless_ask_without_source_is_relayed(repo):
    out = _mint(repo)
    assert out["exit_code"] == 0 and out["provenance"] == "relayed"
    assert _doc(repo, out)["em_analysis"]["relayed_sender"] == "unknown"


def test_relayed_source_ignores_a_recorded_turn_and_leaves_the_mark(repo):
    _turns("pm words")
    out = _mint(repo, source="relayed", sender="peer", relayed_text="peer ask")
    assert out["exit_code"] == 0 and out["turn"] is None
    assert "pm_verbatims" not in _doc(repo, out)
    pm = _mint(repo, name="pm one", source="pm")
    assert pm["exit_code"] == 0 and pm["turn"] == 0 and pm["provenance"] == "pm-verbatim"
    assert [v["text"] for v in _doc(repo, pm)["pm_verbatims"]] == ["pm words"]


def test_refuses_an_existing_path(repo, monkeypatch):
    _turns("an ask")
    from datetime import date
    target = repo / "state" / "sizings" / f"{date.today().isoformat()}-do-the-thing.yaml"
    target.parent.mkdir(parents=True)
    target.write_text("schema: sizing-object\n", encoding="utf-8")
    out = _mint(repo)
    assert out["exit_code"] == 1 and "already exists" in out["error"]
    assert target.read_text(encoding="utf-8") == "schema: sizing-object\n"


def test_second_mint_of_the_same_turn_refuses(repo):
    _turns("an ask")
    assert _mint(repo)["exit_code"] == 0
    again = _mint(repo, name="other name")
    assert again["exit_code"] == 1 and "turn 0 is already sized" in again["error"]
    assert not list((repo / "state" / "sizings").glob("*other-name*"))


def test_a_newer_turn_mints_after_the_mark(repo):
    _turns("an ask")
    assert _mint(repo)["exit_code"] == 0
    _turns("a later ask")
    out = _mint(repo, name="later")
    assert out["exit_code"] == 0 and out["turn"] == 1


def test_missing_name_refuses(repo):
    _turns("an ask")
    out = mod._handler({"name": "  ", "session_id": SID}, repo_root=repo)
    assert out["exit_code"] == 1 and "name" in out["error"]

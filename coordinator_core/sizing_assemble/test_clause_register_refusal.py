"""Tests for the PM-clause register mint in `sizing_assemble.write_back`.

Run (from repo root):
    python -m pytest coordinator_core/sizing_assemble/test_clause_register_refusal.py -q
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

import coordinator_core.sizing_assemble as sa
from coordinator_core.frontmatter.schema_validate import validate_frontmatter
from coordinator_core.session import record_homes

SCHEMA = Path(sa.__file__).resolve().parent.parent / "frontmatter" / "schemas" / "sizing-object.schema.json"
_NAME = "2026-10-02-x.yaml"
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
"""
_LIST = "do these:\n1. first thing\n2) second thing\n- third thing\n"


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    (tmp_path / ".git").mkdir()
    Path(record_homes.home_dir(str(tmp_path), "sizings")).mkdir(parents=True)
    return tmp_path


def _sizing(repo: Path, text: str = _DRAFT) -> Path:
    path = Path(record_homes.record_path(str(repo), "sizings", _NAME))
    path.write_text(text, encoding="utf-8")
    return path


def _write(repo: Path, window, **kw) -> dict:
    import coordinator_core.ops.baton_pm_turns as pm_turns

    pm_turns.recent_window = lambda **_: list(window)
    decision = sa.route(estimate={"tshirt": "M"}, intent="Ship the thing")
    return sa.write_back(repo, f"state/sizings/{_NAME}", decision, **kw)


@pytest.fixture(autouse=True)
def _restore_window():
    import coordinator_core.ops.baton_pm_turns as pm_turns

    original = pm_turns.recent_window
    yield
    pm_turns.recent_window = original


def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _v(turn: int, text: str) -> dict:
    return {"turn": turn, "ts": None, "text": text}


def test_two_enumerated_lines_mint_one_row_per_clause(repo, capsys):
    path = _sizing(repo)
    _write(repo, [_v(7, _LIST)])
    doc = _load(path)
    reg = doc["requirement_register"]
    assert [r["id"] for r in reg["rows"]] == ["T7.1", "T7.2", "T7.3"]
    assert [r["source_text"] for r in reg["rows"]] == ["first thing", "second thing", "third thing"]
    row = reg["rows"][0]
    assert row["anchor"] == "pm_verbatims turn 7, clause 1"
    assert (row["surface"], row["status"], row["claimed_by"]) == ("internal", "open", [])
    assert reg["sources"] == [{"doc": f"state/sizings/{_NAME}", "ref": "pm_verbatims turn 7"}]
    assert doc["status"] == "routed"
    assert validate_frontmatter(doc, SCHEMA) == []
    err = capsys.readouterr().err
    assert "T7.1" in err and "--register <yaml> --supersede --write" in err


def test_clauses_of_two_turns_both_mint_in_turn_order(repo):
    path = _sizing(repo)
    _write(repo, [_v(9, "1. late a\n2. late b"), _v(3, "- early a\n- early b")])
    reg = _load(path)["requirement_register"]
    assert [r["id"] for r in reg["rows"]] == ["T3.1", "T3.2", "T9.1", "T9.2"]
    assert [s["ref"] for s in reg["sources"]] == ["pm_verbatims turn 3", "pm_verbatims turn 9"]


def test_existing_register_is_untouched(repo):
    reg = (
        "requirement_register:\n  sources:\n  - doc: d.md\n    ref: r\n  rows:\n"
        "  - id: a\n    source_text: mine\n    surface: cli\n    status: open\n"
    )
    path = _sizing(repo, _DRAFT + reg)
    _write(repo, [_v(7, _LIST)])
    rows = _load(path)["requirement_register"]["rows"]
    assert [r["id"] for r in rows] == ["a"]


def test_ruled_no_register_mints_nothing(repo):
    path = _sizing(repo, _DRAFT + "pm_resolution:\n  decided_on: 2026-10-12\n  requirement_register: none needed\n")
    _write(repo, [_v(7, _LIST)])
    assert "requirement_register" not in _load(path)


def test_mint_works_on_a_scaffolded_missing_sizing(repo):
    _write(repo, [_v(7, _LIST)])
    assert "requirement_register" in _load(Path(record_homes.record_path(str(repo), "sizings", _NAME)))


def test_resize_embeds_nothing_and_never_mints_or_refuses(repo):
    path = _sizing(repo)
    _write(repo, [_v(7, _LIST)])
    doc = _load(path)
    del doc["requirement_register"]
    path.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    _write(repo, [_v(8, _LIST)])
    assert "requirement_register" not in _load(path)


def test_verbatim_without_turn_mints_indexed_ids(repo):
    path = _sizing(repo)
    _write(repo, [{"text": "1. a\n2. b"}])
    ids = [r["id"] for r in _load(path)["requirement_register"]["rows"]]
    assert ids == ["V0.1", "V0.2"]


def test_fenced_quoted_and_agent_message_lists_do_not_trigger():
    text = (
        "```\n- log a\n- log b\n```\n> - quoted a\n> - quoted b\n"
        "<agent-message from=\"x\">\n- relayed a\n- relayed b\n</agent-message>\nfix it\n"
    )
    assert sa.pm_clause_list([("T1", text)]) == []


def test_single_item_and_no_list_write_as_before(repo):
    path = _sizing(repo)
    _write(repo, [_v(1, "just one:\n- only item"), _v(2, "prose")])
    assert "requirement_register" not in _load(path)


def test_list_only_in_intent_without_pm_verbatim_does_not_trigger(repo):
    path = _sizing(repo, _DRAFT.replace("Ship the thing", "Ship:\\n1. a\\n2. b"))
    _write(repo, [])
    assert "requirement_register" not in _load(path)


def test_pm_verbatim_intent_list_mints_intent_rows(repo, monkeypatch):
    path = _sizing(repo)
    import coordinator_core.ops.baton_pm_turns as pm_turns

    monkeypatch.setattr(pm_turns, "recent_window", lambda **_: [])
    decision = sa.route(estimate={"tshirt": "M"}, intent="Ship:\n1. a\n2. b")
    sa.write_back(repo, f"state/sizings/{_NAME}", decision, intent_verbatim=True)
    reg = _load(path)["requirement_register"]
    assert [r["id"] for r in reg["rows"]] == ["I.1", "I.2"]
    assert reg["rows"][0]["anchor"] == "intent, clause 1"
    assert reg["sources"] == [{"doc": f"state/sizings/{_NAME}", "ref": "intent"}]


def test_stubbed_mint_flags_uncovered_turns_and_never_refuses(repo, monkeypatch, capsys):
    path = _sizing(repo)
    monkeypatch.setattr(sa, "_mint_clause_register", lambda text, *a, **k: text)
    result = _write(repo, [_v(7, _LIST)])
    assert "requirement_register" not in yaml.safe_load(path.read_text(encoding="utf-8"))
    assert result["register_coverage"] == {"turns_total": 1, "turns_uncovered": [7]}
    err = capsys.readouterr().err
    assert "turn(s) 7" in err and "--register" in err

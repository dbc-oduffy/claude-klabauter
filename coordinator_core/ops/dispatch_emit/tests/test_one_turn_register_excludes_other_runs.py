"""A one-turn sizing whose lone embedded PM turn is another run's (embedded by recency): the run's
register carries its own ask, never the foreign turn as a row or a coverage gap."""

from __future__ import annotations

import pytest
import yaml

from coordinator_core.ops import baton_pm_turns as pm_turns
from coordinator_core.ops import requirement_register as rr
from coordinator_core.session_baton import store
from coordinator_core.ops.dispatch_emit import requirements_ledger as rl

RUN = "ask-20261014T000000-abc123"
REL = "state/sizings/2026-10-14-one-turn.yaml"
OWN_ASK = "add retry with backoff to the uploader so transient failures recover"
FOREIGN = "rebuild the nightly report exporter to stream rows instead of buffering them"


def _write(repo, turn_text, derived_rows):
    doc = {
        "schema": "sizing-object", "intent": "Uploader retry", "estimate": {"tshirt": "S", "provisional": True},
        "route": "dispatch", "detents": [], "fork": None, "xl_exit": None, "status": "sized",
        "premise": {"provenance": "not-applicable", "evidence": "fixture"},
        "pm_verbatims": [{"turn": 3, "text": turn_text}],
    }
    path = repo / REL
    path.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    if derived_rows:
        text = path.read_text(encoding="utf-8")
        block = {
            rr.SOURCES_KEY: [{"doc": REL, "ref": "pm_verbatims turn 3"}],
            rr.ROWS_KEY: [{
                rr.ROW_ID: "T3", rr.ROW_TEXT: turn_text, rr.ROW_ANCHOR: "pm_verbatims turn 3",
                rr.ROW_SURFACE: "internal", rr.ROW_STATUS: rr.STATUS_OPEN, rr.ROW_CLAIMED_BY: [],
            }],
        }
        from datetime import datetime, timezone

        path.write_text(rr.write_register_text(text, block, datetime.now(timezone.utc)), encoding="utf-8")


@pytest.fixture
def repo(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()
    (tmp_path / "state" / "sizings").mkdir(parents=True)
    sdir = tmp_path / ".git" / "coordinator-sessions" / "sid-f2"
    sdir.mkdir(parents=True)
    monkeypatch.setattr(store, "baton_dir", lambda s, cwd=None: sdir.parent / s)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", "sid-f2")
    for prompt in ("chatter", OWN_ASK, "unrelated middle turn here today", FOREIGN):
        pm_turns.append_turn(prompt, session_id="sid-f2")
    rl.start(tmp_path, RUN)
    return tmp_path


def _register(repo):
    return rr.read_register((repo / REL).read_text(encoding="utf-8"))


def test_a_foreign_turn_derived_into_the_register_is_dropped_and_waived(repo):
    _write(repo, FOREIGN, derived_rows=True)
    rl.bind(repo, RUN, REL, ask_text=OWN_ASK)
    reg = _register(repo)
    assert [(r["id"], r["source_text"], r["anchor"]) for r in reg.rows] == [("R1", OWN_ASK, "ask")]
    assert [e["turn"] for e in reg.raw[rr.UNRELATED_TURNS_KEY]] == [3]
    assert [i.id for i in rl.read_ledger(repo, repo / rl.RECORD_DIR_ROOT / RUN).items] == ["R1"]


def test_a_foreign_turn_without_derived_rows_is_not_seeded(repo):
    _write(repo, FOREIGN, derived_rows=False)
    rl.bind(repo, RUN, REL, ask_text=OWN_ASK)
    assert [r["source_text"] for r in _register(repo).rows] == [OWN_ASK]


def test_binding_twice_changes_nothing_more(repo):
    _write(repo, FOREIGN, derived_rows=True)
    rl.bind(repo, RUN, REL, ask_text=OWN_ASK)
    first = (repo / REL).read_text(encoding="utf-8")
    rl.bind(repo, RUN, REL, ask_text=OWN_ASK)
    assert [r["source_text"] for r in _register(repo).rows] == [OWN_ASK]
    assert (repo / REL).read_text(encoding="utf-8").count("unrelated_turns") == first.count("unrelated_turns")


def test_an_ask_no_session_turn_quotes_leaves_the_turn_seeded(repo):
    _write(repo, FOREIGN, derived_rows=False)
    rl.bind(repo, RUN, REL, ask_text="a paraphrase nobody typed into the session")
    assert [r["anchor"] for r in _register(repo).rows] == ["pm_verbatims", "ask"]


def test_the_runs_own_turn_stays_a_row(repo):
    _write(repo, OWN_ASK, derived_rows=True)
    rl.bind(repo, RUN, REL, ask_text=OWN_ASK)
    assert [r["id"] for r in _register(repo).rows] == ["T3"]


def test_a_short_ask_contained_in_the_turn_keeps_the_turn(repo):
    _write(repo, "fix it", derived_rows=False)
    rl.bind(repo, RUN, REL, ask_text="fix it")
    assert [r["source_text"] for r in _register(repo).rows] == ["fix it"]


TOKEN_Q = "how many tokens did the last session burn on exploration alone"
FOLLOW_UP = "also log every retry attempt with the failure reason at warning level"


def _write_two(repo, turns):
    doc = {
        "schema": "sizing-object", "intent": "Uploader retry", "estimate": {"tshirt": "S", "provisional": True},
        "route": "dispatch", "detents": [], "fork": None, "xl_exit": None, "status": "sized",
        "premise": {"provenance": "not-applicable", "evidence": "fixture"},
        "pm_verbatims": [{"turn": n, "text": t} for n, t in turns],
    }
    (repo / REL).write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")


def test_a_later_session_turn_the_ask_does_not_address_is_unrelated_not_uncovered(repo):
    pm_turns.append_turn(TOKEN_Q, session_id="sid-f2")
    _write_two(repo, [(1, OWN_ASK), (4, TOKEN_Q)])
    rl.bind(repo, RUN, REL, ask_text=OWN_ASK)
    text = (repo / REL).read_text(encoding="utf-8")
    assert [e["turn"] for e in _register(repo).raw[rr.UNRELATED_TURNS_KEY]] == [4]
    assert rr.uncovered_turns(text) == []


def test_a_follow_up_after_the_ask_stays_coverable(repo):
    pm_turns.append_turn(FOLLOW_UP, session_id="sid-f2")
    _write_two(repo, [(1, OWN_ASK), (4, FOLLOW_UP)])
    rl.bind(repo, RUN, REL, ask_text=OWN_ASK)
    assert rr.UNRELATED_TURNS_KEY not in _register(repo).raw
    assert FOLLOW_UP in [r["source_text"] for r in _register(repo).rows]


def test_a_claimed_foreign_row_is_not_removed(repo):
    _write(repo, FOREIGN, derived_rows=True)
    path = repo / REL
    path.write_text(path.read_text(encoding="utf-8").replace("claimed_by: []", "claimed_by: [docs/plans/p.md]"), encoding="utf-8")
    rl.bind(repo, RUN, REL, ask_text=OWN_ASK)
    assert "T3" in [r["id"] for r in _register(repo).rows]

"""A multi-turn sizing's register: `bind` never seeds a turn listed in `unrelated_turns` or covered
by a register row, proves earlier foreign turns on a multi-turn sizing, and an amended swept sizing
still ships after a re-bind."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
import yaml

from coordinator_core.ops import baton_pm_turns as pm_turns
from coordinator_core.ops import requirement_register as rr
from coordinator_core.ops import sizing_record_register as srr
from coordinator_core.ops.dispatch_emit import requirements_ledger as rl
from coordinator_core.session_baton import store

RUN = "ask-20261014T000000-def456"
REL = "state/sizings/2026-10-14-multi-turn.yaml"
EARLY_A = "rebuild the nightly report exporter to stream rows instead of buffering them"
EARLY_B = "rename the legacy cache warmer module and update every import that names it"
OWN_ASK = "add retry with backoff to the uploader so transient failures recover"
FOLLOW_UP = "also log every retry attempt with the failure reason at warning level"
TURNS = (EARLY_A, EARLY_B, OWN_ASK, FOLLOW_UP)


def _write(repo, rows=(), unrelated=()):
    doc = {
        "schema": "sizing-object", "intent": "Uploader retry", "estimate": {"tshirt": "S", "provisional": True},
        "route": "dispatch", "detents": [], "fork": None, "xl_exit": None, "status": "sized",
        "premise": {"provenance": "not-applicable", "evidence": "fixture"},
        "pm_verbatims": [{"turn": n, "text": t} for n, t in enumerate(TURNS)],
    }
    path = repo / REL
    path.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    if rows or unrelated:
        block = {
            rr.SOURCES_KEY: [{"doc": REL, "ref": "pm_verbatims"}],
            rr.ROWS_KEY: [
                {
                    rr.ROW_ID: rid, rr.ROW_TEXT: text, rr.ROW_ANCHOR: anchor, rr.ROW_SURFACE: "internal",
                    rr.ROW_STATUS: rr.STATUS_OPEN, rr.ROW_CLAIMED_BY: [],
                }
                for rid, text, anchor in rows
            ],
        }
        if unrelated:
            block[rr.UNRELATED_TURNS_KEY] = [{"turn": n, "reason": "other ask"} for n in unrelated]
        path.write_text(
            rr.write_register_text(path.read_text(encoding="utf-8"), block, datetime.now(timezone.utc)),
            encoding="utf-8",
        )


@pytest.fixture
def repo(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()
    (tmp_path / "state" / "sizings").mkdir(parents=True)
    sdir = tmp_path / ".git" / "coordinator-sessions" / "sid-mt"
    sdir.mkdir(parents=True)
    monkeypatch.setattr(store, "baton_dir", lambda s, cwd=None: sdir.parent / s)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", "sid-mt")
    for prompt in TURNS:
        pm_turns.append_turn(prompt, session_id="sid-mt")
    rl.start(tmp_path, RUN)
    return tmp_path


def _register(repo):
    return rr.read_register((repo / REL).read_text(encoding="utf-8"))


def _texts(repo):
    return [r["source_text"] for r in _register(repo).rows]


def test_turns_listed_unrelated_are_not_seeded_and_binding_twice_adds_nothing(repo):
    _write(repo, rows=[("X1", "placeholder", "ask")], unrelated=[0, 1])
    rl.bind(repo, RUN, REL)
    first = _texts(repo)
    assert first == ["placeholder", OWN_ASK, FOLLOW_UP]
    rl.bind(repo, RUN, REL)
    assert _texts(repo) == first


def test_a_turn_covered_by_a_derived_clause_row_is_not_reseeded_whole(repo):
    clause = "add retry with backoff to the uploader"
    _write(repo, rows=[("C1", clause, "pm_verbatims turn 2")], unrelated=[0, 1, 3])
    rl.bind(repo, RUN, REL)
    assert _texts(repo) == [clause]


def test_earlier_foreign_turns_are_proven_and_dropped_a_later_follow_up_is_kept(repo):
    _write(repo, rows=[
        ("T0", EARLY_A, "pm_verbatims turn 0"), ("T1", EARLY_B, "pm_verbatims"),
        ("T2", OWN_ASK, "pm_verbatims turn 2"), ("T3", FOLLOW_UP, "pm_verbatims turn 3"),
    ])
    rl.bind(repo, RUN, REL, ask_text=OWN_ASK)
    reg = _register(repo)
    assert [r["id"] for r in reg.rows] == ["T2", "T3"]
    assert sorted(e["turn"] for e in reg.raw[rr.UNRELATED_TURNS_KEY]) == [0, 1]
    assert all(e["reason"] == rl.FOREIGN_TURN_REASON for e in reg.raw[rr.UNRELATED_TURNS_KEY])


def test_an_ask_no_session_turn_quotes_proves_nothing_foreign(repo):
    _write(repo)
    rl.bind(repo, RUN, REL, ask_text="a paraphrase nobody typed into the session")
    assert _texts(repo) == [*TURNS, "a paraphrase nobody typed into the session"]
    assert rr.UNRELATED_TURNS_KEY not in _register(repo).raw


def test_an_amended_swept_sizing_stays_clean_after_rebind_and_ships(repo):
    _write(repo)
    rl.bind(repo, RUN, REL)
    swept = [r["id"] for r in _register(repo).rows if r["source_text"] in (EARLY_A, EARLY_B)]
    assert swept == ["R1", "R2"]
    reply = srr._handler(
        {
            "sizing": REL, "amend": True,
            "register": {
                "drop": swept,
                "unrelated_turns": [{"turn": 0, "reason": "other ask"}, {"turn": 1, "reason": "other ask"}],
            },
        },
        repo_root=repo,
    )
    assert reply["exit_code"] == 0, reply
    rl.bind(repo, RUN, REL)
    assert _texts(repo) == [OWN_ASK, FOLLOW_UP]

    path = repo / REL
    reg = rr.read_register(path.read_text(encoding="utf-8"))
    rows = [dict(r, **{rr.ROW_STATUS: rr.STATUS_MET, rr.ROW_WIRED: True}) for r in reg.rows]
    path.write_text(
        rr.write_register_text(path.read_text(encoding="utf-8"), {**reg.raw, rr.ROWS_KEY: rows}, datetime.now(timezone.utc)),
        encoding="utf-8",
    )
    new_text, refusal = rr.ship_text(path.read_text(encoding="utf-8"))
    assert refusal is None and new_text is not None
    assert yaml.safe_load(new_text)["status"] == "shipped"

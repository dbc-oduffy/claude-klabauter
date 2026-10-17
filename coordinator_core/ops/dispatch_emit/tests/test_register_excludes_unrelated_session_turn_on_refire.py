"""A re-fired run's register binds its ask, relayed text and revisions; a later session PM turn
that shares a content word with the ask but was never relayed or revised in is listed under
`unrelated_turns`, not seeded as an open row."""

from __future__ import annotations

import pytest
import yaml

from coordinator_core.ops import baton_pm_turns as pm_turns
from coordinator_core.ops import requirement_register as rr
from coordinator_core.ops.dispatch_emit import requirements_ledger as rl
from coordinator_core.session_baton import store

RUN = "ask-20261017T000000-aaaaaa"
REL = "state/sizings/2026-10-17-refire.yaml"
ASK = "fix the requirement register so a re-fired run keeps its commits tidy and bound"
UNRELATED = "the auto-push still appears to be slow or unreliable, in that commits in this repo should have been pushed by now"
TURNS = (ASK, UNRELATED)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()
    (tmp_path / "state" / "sizings").mkdir(parents=True)
    sdir = tmp_path / ".git" / "coordinator-sessions" / "sid-rf"
    sdir.mkdir(parents=True)
    monkeypatch.setattr(store, "baton_dir", lambda s, cwd=None: sdir.parent / s)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", "sid-rf")
    for prompt in TURNS:
        pm_turns.append_turn(prompt, session_id="sid-rf")
    doc = {
        "schema": "sizing-object", "intent": "Re-fire fix", "estimate": {"tshirt": "S", "provisional": True},
        "route": "dispatch", "detents": [], "fork": None, "xl_exit": None, "status": "sized",
        "premise": {"provenance": "not-applicable", "evidence": "fixture"},
        "pm_verbatims": [{"turn": n, "text": t} for n, t in enumerate(TURNS)],
    }
    (tmp_path / REL).write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    rl.start(tmp_path, RUN)
    return tmp_path


def _register(repo):
    return rr.read_register((repo / REL).read_text(encoding="utf-8"))


def test_a_later_unrelated_session_turn_sharing_a_word_is_listed_not_seeded(repo):
    rl.bind(repo, RUN, REL, ask_text=ASK)
    reg = _register(repo)
    assert UNRELATED not in [r["source_text"] for r in reg.rows]
    assert [e["turn"] for e in reg.raw[rr.UNRELATED_TURNS_KEY]] == [1]
    assert not [r for r in reg.rows if r["status"] == rr.STATUS_OPEN and r["source_text"] == UNRELATED]


def test_rebinding_is_idempotent_and_a_revision_still_binds(repo):
    rl.bind(repo, RUN, REL, ask_text=ASK)
    rl.revise(repo, RUN, verbatim="also keep the sizing shippable")
    rl.bind(repo, RUN, REL, ask_text=ASK)
    reg = _register(repo)
    texts = [r["source_text"] for r in reg.rows]
    assert ASK in texts and "also keep the sizing shippable" in texts and UNRELATED not in texts
    assert len(reg.raw[rr.UNRELATED_TURNS_KEY]) == 1


def test_a_relayed_run_binds_the_relayed_text_only(repo):
    rl.bind(repo, RUN, REL, relayed_text="relayed fix text for the register")
    assert [r["source_text"] for r in _register(repo).rows] == ["relayed fix text for the register"]

"""A re-fired run's register binds its ask, relayed text and revisions; a later session PM turn
that was never relayed or revised in and is not proven another run's seeds an open row; it is
never listed under `unrelated_turns`."""

from __future__ import annotations

import pytest
import yaml

from coordinator_core.session_hub_path import session_dir_for
from coordinator_core.ops import baton_pm_turns as pm_turns
from coordinator_core.ops import requirement_register as rr
from coordinator_core.ops.dispatch_emit import requirements_ledger as rl
from coordinator_core.session_baton import store
from pathlib import Path

RUN = "ask-20261017T000000-aaaaaa"
REL = "state/sizings/2026-10-17-refire.yaml"
ASK = "fix the requirement register so a re-fired run keeps its commits tidy and bound"
UNRELATED = "the auto-push still appears to be slow or unreliable, in that commits in this repo should have been pushed by now"
TURNS = (ASK, UNRELATED)


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    (tmp_path / ".git").mkdir()
    (tmp_path / "state" / "sizings").mkdir(parents=True)
    sdir = session_dir_for(tmp_path / ".git", "sid-rf")
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


def test_a_later_pm_turn_not_proven_foreign_is_seeded_as_an_open_row(repo) -> None:
    rl.bind(repo, RUN, REL, ask_text=ASK)
    reg = _register(repo)
    assert [r["source_text"] for r in reg.rows if r["status"] == rr.STATUS_OPEN] == [ASK, UNRELATED]
    assert not reg.raw.get(rr.UNRELATED_TURNS_KEY)


def test_rebinding_is_idempotent_and_a_revision_still_binds(repo) -> None:
    rl.bind(repo, RUN, REL, ask_text=ASK)
    rl.revise(repo, RUN, verbatim="also keep the sizing shippable")
    rl.bind(repo, RUN, REL, ask_text=ASK)
    reg = _register(repo)
    texts = [r["source_text"] for r in reg.rows]
    assert ASK in texts and "also keep the sizing shippable" in texts
    assert texts.count(UNRELATED) == 1
    assert not reg.raw.get(rr.UNRELATED_TURNS_KEY)


def test_a_relayed_run_binds_the_relayed_text_only(repo) -> None:
    rl.bind(repo, RUN, REL, relayed_text="relayed fix text for the register")
    assert [r["source_text"] for r in _register(repo).rows] == ["relayed fix text for the register"]

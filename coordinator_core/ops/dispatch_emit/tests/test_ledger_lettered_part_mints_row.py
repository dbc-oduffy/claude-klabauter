"""A lettered part named in a revise note or the sizing's em_brief mints one relayed register row."""

from __future__ import annotations

import pytest
import yaml

from coordinator_core.ops import requirement_register as rr
from coordinator_core.ops.dispatch_emit import requirements_ledger as rl

REL = "state/sizings/2026-10-01-lettered.yaml"
RUN = "ask-20261001T000000-aaaaaa"
BRIEF = "Do two things: (a) fix the reacher gating. (b) finish the remnant."


@pytest.fixture
def repo(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()
    (tmp_path / "state" / "sizings").mkdir(parents=True)
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _put(repo, brief=None):
    doc = {
        "schema": "sizing-object", "name": "lettered", "intent": "exercise lettered parts",
        "estimate": {"tshirt": "XS", "provisional": True}, "route": "dispatch", "detents": [],
        "fork": None, "xl_exit": None, "status": "sized",
        "premise": {"provenance": "not-applicable", "evidence": "fixture"},
        "deliverable_id": "dlv-lettered-abc123", "interaction_mode": "pm",
        "exit_criterion": {"statement": "done", "accepted": {"pm_quote": "yes", "on": "2026-10-01", "mode": "pm"}},
        "pm_verbatims": [{"turn": 1, "text": "make it so"}],
    }
    if brief:
        doc["em_analysis"] = {"em_brief": brief}
    (repo / REL).write_text(yaml.safe_dump(doc), encoding="utf-8", newline="\n")


def _texts(repo):
    return [r["source_text"] for r in rr.read_register((repo / REL).read_text(encoding="utf-8")).rows]


def _bound(repo, brief=None):
    _put(repo, brief)
    rl.start(repo, RUN)
    rl.bind(repo, RUN, REL, ask_text="the ask")


def test_note_naming_lettered_parts_mints_one_relayed_row_each(repo):
    _bound(repo)
    before = _texts(repo)
    rl.revise(repo, RUN, note="Scope: (a) the gating fix; (b) remnant completion.")
    after = _texts(repo)
    assert after[len(before):] == ["(a) the gating fix", "(b) remnant completion."]
    rows = rr.read_register((repo / REL).read_text(encoding="utf-8")).rows
    assert {r["anchor"] for r in rows[len(before):]} == {"relayed"}


def test_repeated_note_adds_no_duplicate(repo):
    _bound(repo)
    note = "(a) the gating fix (b) remnant completion"
    rl.revise(repo, RUN, note=note)
    once = _texts(repo)
    rl.revise(repo, RUN, note=note)
    assert _texts(repo) == once


def test_note_without_lettered_part_adds_no_row(repo):
    _bound(repo)
    before = _texts(repo)
    rl.revise(repo, RUN, note="just a progress remark, nothing lettered")
    assert _texts(repo) == before


def test_bind_mints_a_row_per_lettered_part_of_the_em_brief_once(repo):
    _bound(repo, BRIEF)
    texts = _texts(repo)
    assert "(a) fix the reacher gating." in texts
    assert "(b) finish the remnant." in texts
    rl.bind(repo, RUN, REL, ask_text="the ask")
    assert _texts(repo) == texts

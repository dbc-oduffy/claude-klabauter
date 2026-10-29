"""A revise note is prose in notes.jsonl: it never mints a register row, lettered or not."""

from __future__ import annotations

import pytest
import yaml

from coordinator_core.ops import requirement_register as rr
from coordinator_core.ops.dispatch_emit import requirements_ledger as rl

REL = "state/sizings/2026-10-01-notes.yaml"
RUN = "ask-20261001T000000-bbbbbb"


@pytest.fixture
def repo(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()
    (tmp_path / "state" / "sizings").mkdir(parents=True)
    monkeypatch.chdir(tmp_path)
    doc = {
        "schema": "sizing-object", "name": "notes", "intent": "exercise notes",
        "estimate": {"tshirt": "XS", "provisional": True}, "route": "dispatch", "detents": [],
        "fork": None, "xl_exit": None, "status": "sized",
        "premise": {"provenance": "not-applicable", "evidence": "fixture"},
        "deliverable_id": "dlv-notes-abc123", "interaction_mode": "pm",
        "exit_criterion": {"statement": "done", "accepted": {"pm_quote": "yes", "on": "2026-10-01", "mode": "pm"}},
        "pm_verbatims": [{"turn": 1, "text": "make it so"}],
    }
    (tmp_path / REL).write_text(yaml.safe_dump(doc), encoding="utf-8", newline="\n")
    rl.start(tmp_path, RUN)
    rl.bind(tmp_path, RUN, REL, ask_text="the ask")
    return tmp_path


def _texts(repo):
    return [r["source_text"] for r in rr.read_register((repo / REL).read_text(encoding="utf-8")).rows]


def test_lettered_note_mints_no_row_and_is_stored(repo):
    before = _texts(repo)
    rl.revise(repo, RUN, note="(e) scout-reply parse; (f) twin scan fixed by hand.")
    assert _texts(repo) == before
    assert rl.read_notes(rl.run_dir(repo, RUN)) == ["(e) scout-reply parse; (f) twin scan fixed by hand."]


def test_verbatim_still_mints_a_row(repo):
    before = _texts(repo)
    rl.revise(repo, RUN, verbatim="do the other thing")
    assert len(_texts(repo)) == len(before) + 1

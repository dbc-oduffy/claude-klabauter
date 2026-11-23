"""A revise note is prose in notes.jsonl: it never mints a register row, lettered or not."""

from __future__ import annotations

import pytest
import yaml

from coordinator_core.ops import requirement_register as rr
from coordinator_core.ops.dispatch_emit import requirements_ledger as rl
from pathlib import Path

REL = "state/sizings/2026-10-01-notes.yaml"
RUN = "ask-20261001T000000-bbbbbb"


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
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


def test_lettered_note_mints_no_row_and_is_stored(repo) -> None:
    before = _texts(repo)
    rl.revise(repo, RUN, note="(e) scout-reply parse; (f) twin scan fixed by hand.")
    assert _texts(repo) == before
    assert rl.read_notes(rl.run_dir(repo, RUN)) == ["(e) scout-reply parse; (f) twin scan fixed by hand."]


def _gate(repo: Path, items: list[dict]) -> dict | None:
    from coordinator_core.ops.dispatch_emit import land_run

    refusal, _ledger, _open = land_run._ledger_gate(
        repo, rl.run_dir(repo, RUN), {"items": items}, None, [], {}, None, {}, None, None,
    )
    return refusal


def _three_rows(repo: Path) -> list[str]:
    rl.revise(repo, RUN, verbatim="second thing")
    rl.revise(repo, RUN, verbatim="third thing")
    ledger = rl.read_ledger(repo, rl.run_dir(repo, RUN))
    assert ledger is not None
    return [i.id for i in ledger.items]


def test_row_superseded_by_a_live_note_commits(repo) -> None:
    *met, r3 = _three_rows(repo)
    rl.revise(repo, RUN, note="the third thing is ruled out; deleted outright.")
    assert rl.note_ids(rl.run_dir(repo, RUN)) == ["N1"]
    items = [
        *({"id": m, "status": "met", "evidence": "verified"} for m in met),
        {"id": r3, "status": "superseded", "superseded_by": "N1"},
    ]
    ledger = rl.read_ledger(repo, rl.run_dir(repo, RUN))
    assert rl.judge(ledger, {"items": items}, note_ids=["N1"])["failures"] == []
    assert _gate(repo, items) is None


def test_row_superseded_by_an_unrecorded_note_refuses_naming_the_id(repo) -> None:
    *met, r3 = _three_rows(repo)
    rl.revise(repo, RUN, note="a note exists, but N9 is not it.")
    ledger = rl.read_ledger(repo, rl.run_dir(repo, RUN))
    items = [
        *({"id": m, "status": "met", "evidence": "verified"} for m in met),
        {"id": r3, "status": "superseded", "superseded_by": "N9"},
        {"id": "N1", "status": "met", "evidence": "verified"},
    ]
    failures = rl.judge(ledger, {"items": items}, note_ids=rl.note_ids(rl.run_dir(repo, RUN)))["failures"]
    assert [f["id"] for f in failures] == [r3] and "N9" in failures[0]["reason"]
    assert _gate(repo, items) is not None


def test_note_absent_from_the_live_set_does_not_resolve_a_supersession(repo) -> None:
    *met, r3 = _three_rows(repo)
    ledger = rl.read_ledger(repo, rl.run_dir(repo, RUN))
    items = [
        *({"id": m, "status": "met", "evidence": "verified"} for m in met),
        {"id": r3, "status": "superseded", "superseded_by": "N1"},
    ]
    failures = rl.judge(ledger, {"items": items}, note_ids=[])["failures"]
    assert [f["id"] for f in failures] == [r3] and "N1" in failures[0]["reason"]


def test_verbatim_still_mints_a_row(repo) -> None:
    before = _texts(repo)
    rl.revise(repo, RUN, verbatim="do the other thing")
    assert len(_texts(repo)) == len(before) + 1

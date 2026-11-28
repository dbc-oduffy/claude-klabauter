"""The PM's register (all_of_pm_ask, pm_verbatims, register rows, source docs, EM revise notes) survives
a chain hop and a fresh mint that supersedes a walled or halted run."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from coordinator_core.ops import requirement_register as rr
from coordinator_core.ops.dispatch_emit import requirements_ledger as rl
from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT

ASK = "ask-20261121T215750-cdbbba"
WARP = "warp-2026-11-21-holo-cdbbba-20261121T215918-d12be8"
OLD = "ask-20261121T212246-46b2c4"
NEW = "ask-20261121T212304-fde7c5"
OLD_REL = "state/sizings/2026-11/2026-11-21-old.yaml"
NEW_REL = "state/sizings/2026-11/2026-11-21-new.yaml"
TURN21 = "the PM's turn 21 words, in full, never abridged"
SOURCES = ("docs/a.md", "docs/b.md", "docs/c.md")
NOTE = "holo-23: the proof is CPU-only at runtime"
NOTE_AT = "2026-11-21T22:00:00Z"


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    (tmp_path / ".git").mkdir()
    return tmp_path


def _dir(repo: Path, run_id: str) -> Path:
    return repo / RECORD_DIR_ROOT / run_id


def _sizing(repo: Path, rel: str, verbatims: list[dict]) -> None:
    doc = {
        "schema": "sizing-object", "intent": "carry the register", "estimate": {"tshirt": "S", "provisional": True},
        "route": "dispatch", "detents": [], "fork": None, "xl_exit": None, "status": "sized",
        "premise": {"provenance": "not-applicable", "evidence": "fixture"},
    }
    if verbatims:
        doc["pm_verbatims"] = verbatims
    (repo / rel).parent.mkdir(parents=True, exist_ok=True)
    (repo / rel).write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")


def _note(repo: Path, run_id: str, note: str, at: str = NOTE_AT) -> None:
    d = _dir(repo, run_id)
    d.mkdir(parents=True, exist_ok=True)
    with (d / rl.NOTES_FILE).open("a", encoding="utf-8") as f:
        f.write(json.dumps({"note": note, "at": at}) + "\n")


def _view(repo: Path, run_id: str) -> str:
    return (_dir(repo, run_id) / rl.LEDGER_VIEW_FILE).read_text(encoding="utf-8")


def _flag_line(repo: Path, run_id: str) -> str:
    return next(x for x in _view(repo, run_id).splitlines() if "(all_of_pm_ask)" in x).rsplit(": ", 1)[1]


def _register(repo: Path, rel: str) -> rr.Register:
    return rr.read_register((repo / rel).read_text(encoding="utf-8"))


def _predecessor(repo: Path) -> None:
    """46b2c4: bound to a sizing holding PM turn 21, an EM row, three source docs and a note."""
    _sizing(repo, OLD_REL, [{"turn": 21, "text": TURN21}])
    rl.start(repo, OLD, all_of_pm_ask=True)
    rl.bind(repo, OLD, OLD_REL, ask_text="the original ask text, long enough")
    rl._mutate_register(repo, OLD_REL, lambda rows: rows, extra_sources=[{"doc": d, "ref": "fixture"} for d in SOURCES])
    _note(repo, OLD, NOTE)
    rl._write_view(repo, _dir(repo, OLD), OLD)


def _successor(repo: Path, **start_kw) -> None:
    """fde7c5: a fresh mint holding only a one-line relayed R1."""
    _sizing(repo, NEW_REL, [])
    rl.start(repo, NEW, **start_kw)
    rl.bind(repo, NEW, NEW_REL, relayed_text="one-line relayed ask")


def test_chain_hop_carries_the_flag_and_the_em_note_the_cdbbba_witness(repo: Path) -> None:
    rl.start(repo, ASK)
    rl.revise(repo, ASK, all_of_pm_ask=True)
    _note(repo, ASK, NOTE)
    rl.start(repo, WARP, chain_parent=ASK)
    assert bool(rl.read_run(_dir(repo, WARP))["all_of_pm_ask"]) is True
    assert _flag_line(repo, WARP) == "yes"
    assert [n["note"] for n in rl.carried_notes(repo, WARP)] == [NOTE]
    assert NOTE in _view(repo, WARP)


def test_a_note_filed_on_the_parent_after_the_hop_reads_through_the_child(repo: Path) -> None:
    rl.start(repo, ASK)
    rl.start(repo, WARP, chain_parent=ASK)
    _note(repo, ASK, NOTE)
    assert [n["run"] for n in rl.carried_notes(repo, WARP)] == [ASK]
    rl._write_view(repo, _dir(repo, WARP), WARP)
    assert NOTE in _view(repo, WARP)


def test_a_retracted_parent_note_stays_retracted_in_the_child(repo: Path) -> None:
    rl.start(repo, ASK)
    _note(repo, ASK, NOTE)
    with (_dir(repo, ASK) / rl.NOTES_FILE).open("a", encoding="utf-8") as f:
        f.write(json.dumps({"retracts": "N1", "reason": "withdrawn", "at": NOTE_AT}) + "\n")
    rl.start(repo, WARP, chain_parent=ASK)
    from coordinator_core.ops.dispatch_emit import run_notes

    assert run_notes.live(rl.carried_notes(repo, WARP)) == []


def test_the_chain_child_does_not_feed_its_notes_back_to_the_parent(repo: Path) -> None:
    rl.start(repo, ASK)
    rl.start(repo, WARP, chain_parent=ASK)
    _note(repo, WARP, "child-only note")
    assert rl.carried_notes(repo, ASK) == []


def test_chain_notes_follow_a_second_hop_and_a_cycle_terminates(repo: Path) -> None:
    _note(repo, ASK, NOTE)
    rl.start(repo, ASK, chain_parent=WARP)
    rl.start(repo, WARP, chain_parent=ASK)
    assert [n["note"] for n in rl.carried_notes(repo, ASK)] == [NOTE]
    assert [n["note"] for n in rl.carried_notes(repo, WARP)] == [NOTE]


def test_supersedes_is_recorded_kept_and_refuses_a_self_link(repo: Path) -> None:
    d = rl.start(repo, NEW, supersedes=OLD)
    assert rl.read_run(d)[rl.SUPERSEDES_FIELD] == OLD
    rl.start(repo, NEW)
    assert rl.read_run(d)[rl.SUPERSEDES_FIELD] == OLD
    for bad in (NEW, "../x", ""):
        with pytest.raises(rl.LedgerRefused):
            rl.start(repo, NEW, supersedes=bad)


def test_supersedes_carries_the_flag_and_the_notes_with_no_sizing(repo: Path) -> None:
    rl.start(repo, OLD, all_of_pm_ask=True)
    _note(repo, OLD, NOTE)
    rl.start(repo, NEW, supersedes=OLD)
    assert _flag_line(repo, NEW) == "yes"
    assert NOTE in _view(repo, NEW)


def test_a_fresh_mint_inherits_the_46b2c4_register_on_bind(repo: Path) -> None:
    _predecessor(repo)
    _successor(repo, supersedes=OLD)
    register = _register(repo, NEW_REL)
    texts = {r[rr.ROW_TEXT]: r for r in register.rows}
    assert TURN21 in texts and texts[TURN21][rr.ROW_ANCHOR] == rl.ORIGIN_PM_VERBATIMS
    assert "one-line relayed ask" in texts
    assert {s["doc"] for s in register.raw[rr.SOURCES_KEY]} >= set(SOURCES)
    view = _view(repo, NEW)
    assert TURN21 in view and NOTE in view
    assert _flag_line(repo, NEW) == "yes"
    assert rl.register_shrinks(repo, OLD, NEW) == []


def test_ids_stay_unique_and_the_carry_is_idempotent(repo: Path) -> None:
    _predecessor(repo)
    _successor(repo, supersedes=OLD)
    ids = [r[rr.ROW_ID] for r in _register(repo, NEW_REL).rows]
    assert len(ids) == len(set(ids))
    assert rl.carry_register(repo, OLD, NEW) == []
    assert [r[rr.ROW_ID] for r in _register(repo, NEW_REL).rows] == ids


def test_register_shrinks_names_what_a_bare_mint_lacks_and_carry_clears_it(repo: Path) -> None:
    _predecessor(repo)
    _successor(repo)
    lacking = rl.register_shrinks(repo, OLD, NEW)
    assert lacking[:2] == ["R1", "R2"]
    assert all(f"source {d}" in lacking for d in SOURCES)
    copied = rl.carry_register(repo, OLD, NEW)
    assert all(f"source:{d}" in copied for d in SOURCES)
    assert rl.register_shrinks(repo, OLD, NEW) == []
    assert rl.read_run(_dir(repo, NEW))[rl.SUPERSEDES_FIELD] == OLD
    assert _flag_line(repo, NEW) == "yes"


def test_register_shrinks_reports_a_dropped_non_pm_row_by_its_predecessor_id(repo: Path) -> None:
    _predecessor(repo)
    _successor(repo, supersedes=OLD)
    gone = next(r for r in _register(repo, OLD_REL).rows if r[rr.ROW_TEXT] == "the original ask text, long enough")
    rl._mutate_register(repo, NEW_REL, lambda rows: [r for r in rows if r[rr.ROW_TEXT] != gone[rr.ROW_TEXT]])
    assert rl.register_shrinks(repo, OLD, NEW) == [gone[rr.ROW_ID]]


def test_a_carried_met_row_keeps_its_status_and_loses_its_claim(repo: Path) -> None:
    _predecessor(repo)

    def settle(rows: list[dict]) -> list[dict]:
        rows[0][rr.ROW_STATUS] = rr.STATUS_PARTIAL
        rows[0][rr.ROW_CLAIMED_BY] = ["docs/plans/x.md"]
        return rows

    rl._mutate_register(repo, OLD_REL, settle)
    carried_text = _register(repo, OLD_REL).rows[0][rr.ROW_TEXT]
    _successor(repo, supersedes=OLD)
    row = next(r for r in _register(repo, NEW_REL).rows if r[rr.ROW_TEXT] == carried_text)
    assert row[rr.ROW_STATUS] == rr.STATUS_PARTIAL and row[rr.ROW_CLAIMED_BY] == []


def test_a_turn_the_predecessor_waived_as_unrelated_is_not_carried(repo: Path) -> None:
    _predecessor(repo)
    rl._mutate_register(
        repo, OLD_REL, lambda rows: rows, unrelated=[{"turn": 21, "reason": rl.FOREIGN_TURN_REASON}],
        extra_sources=[{"doc": "docs/d.md", "ref": "fixture"}],
    )
    _successor(repo, supersedes=OLD)
    assert TURN21 not in {r[rr.ROW_TEXT] for r in _register(repo, NEW_REL).rows}
    assert rl.register_shrinks(repo, OLD, NEW) == []


def test_an_unbound_successor_has_nothing_to_compare_and_gets_notes_only(repo: Path) -> None:
    _predecessor(repo)
    rl.start(repo, NEW)
    assert rl.register_shrinks(repo, OLD, NEW) == []
    assert rl.carry_register(repo, OLD, NEW) == []
    assert rl.read_run(_dir(repo, NEW))[rl.SUPERSEDES_FIELD] == OLD
    assert NOTE in _view(repo, NEW)
    assert _flag_line(repo, NEW) == "yes"


def test_carry_register_refuses_nothing_for_unsafe_or_self_ids(repo: Path) -> None:
    rl.start(repo, NEW)
    assert rl.carry_register(repo, NEW, NEW) == []
    assert rl.carry_register(repo, "../x", NEW) == []
    assert rl.register_shrinks(repo, "../x", NEW) == []


def test_a_tied_stamp_reads_the_successors_own_value_across_supersedes(repo: Path) -> None:
    stamp = "2026-11-21T22:00:00.000000Z"
    rl._write_run(_dir(repo, OLD), {"sizing_rel": None, "all_of_pm_ask": True, rl.ALL_OF_AT_FIELD: stamp})
    rl._write_run(
        _dir(repo, NEW),
        {"sizing_rel": None, "all_of_pm_ask": False, rl.ALL_OF_AT_FIELD: stamp, rl.SUPERSEDES_FIELD: OLD},
    )
    assert rl.lineage_all_of(repo, NEW, rl.read_run(_dir(repo, NEW))) == (False, stamp)
    rl._write_run(_dir(repo, NEW), {**rl.read_run(_dir(repo, NEW)), rl.ALL_OF_AT_FIELD: "2026-11-21T21:00:00.000000Z"})
    assert rl.lineage_all_of(repo, NEW, rl.read_run(_dir(repo, NEW))) == (True, stamp)


def test_a_supersedes_cycle_terminates(repo: Path) -> None:
    rl.start(repo, OLD, supersedes=NEW)
    rl.start(repo, NEW, supersedes=OLD)
    _note(repo, OLD, NOTE)
    assert [n["note"] for n in rl.carried_notes(repo, NEW)] == [NOTE]
    assert rl.lineage_all_of(repo, NEW, rl.read_run(_dir(repo, NEW)))[0] is False


def test_a_second_carried_source_hands_its_flag_over_though_supersedes_is_taken(repo: Path) -> None:
    other = "ask-20261121T210000-aaaaaa"
    rl.start(repo, other)
    rl.start(repo, OLD, all_of_pm_ask=True)
    _note(repo, OLD, NOTE)
    rl.start(repo, NEW, supersedes=other)
    assert rl.read_run(_dir(repo, NEW))["all_of_pm_ask"] is False
    copied = rl.carry_register(repo, OLD, NEW)
    assert rl.read_run(_dir(repo, NEW))[rl.SUPERSEDES_FIELD] == other
    assert rl.read_run(_dir(repo, NEW))["all_of_pm_ask"] is True
    assert copied and NOTE in _view(repo, NEW)


def test_a_second_carried_source_never_overrides_a_later_explicit_false(repo: Path) -> None:
    other = "ask-20261121T210000-aaaaaa"
    rl.start(repo, OLD, all_of_pm_ask=True)
    rl.start(repo, NEW, supersedes=other)
    rl.revise(repo, NEW, all_of_pm_ask=False)
    rl.carry_register(repo, OLD, NEW)
    assert rl.read_run(_dir(repo, NEW))["all_of_pm_ask"] is False


def test_revise_origin_pm_with_a_turn_mints_a_pm_row_not_a_revision_row(repo: Path) -> None:
    _sizing(repo, NEW_REL, [])
    rl.start(repo, NEW)
    rl.bind(repo, NEW, NEW_REL, relayed_text="one-line relayed ask")
    rl.revise(repo, NEW, verbatim=TURN21, origin="pm", turn={"session": "s-1", "ts": "2026-11-21T22:00:00Z"})
    row = next(r for r in _register(repo, NEW_REL).rows if r[rr.ROW_TEXT] == TURN21)
    assert row[rr.ROW_ANCHOR] == rl.ORIGIN_PM_VERBATIMS
    assert _flag_line(repo, NEW) == "yes"


def test_revise_without_origin_still_mints_a_revision_row(repo: Path) -> None:
    _sizing(repo, NEW_REL, [])
    rl.start(repo, NEW)
    rl.bind(repo, NEW, NEW_REL, relayed_text="one-line relayed ask")
    rl.revise(repo, NEW, verbatim=TURN21)
    row = next(r for r in _register(repo, NEW_REL).rows if r[rr.ROW_TEXT] == TURN21)
    assert row[rr.ROW_ANCHOR].startswith(rl.ORIGIN_REVISION + "@")


@pytest.mark.parametrize("kw", [{"origin": "pm"}, {"origin": "pm", "turn": {"session": "s-1"}}, {"origin": "em", "turn": {"session": "s", "ts": "t"}}])
def test_revise_origin_pm_without_a_cited_turn_is_refused(repo: Path, kw: dict) -> None:
    _sizing(repo, NEW_REL, [])
    rl.start(repo, NEW)
    rl.bind(repo, NEW, NEW_REL, relayed_text="one-line relayed ask")
    with pytest.raises(rl.LedgerRefused):
        rl.revise(repo, NEW, verbatim=TURN21, **kw)
    assert all(r[rr.ROW_TEXT] != TURN21 for r in _register(repo, NEW_REL).rows)


def test_revise_origin_pm_over_an_already_held_revision_row_reanchors_it_to_pm(repo: Path) -> None:
    _sizing(repo, NEW_REL, [])
    rl.start(repo, NEW)
    rl.bind(repo, NEW, NEW_REL, relayed_text="one-line relayed ask")
    rl.revise(repo, NEW, verbatim=TURN21)
    held = next(r for r in _register(repo, NEW_REL).rows if r[rr.ROW_TEXT] == TURN21)
    assert held[rr.ROW_ANCHOR].startswith(rl.ORIGIN_REVISION + "@")
    out = rl.revise(repo, NEW, verbatim=TURN21, origin="pm", turn={"session": "s-1", "ts": "t"})
    rows = [r for r in _register(repo, NEW_REL).rows if r[rr.ROW_TEXT] == TURN21]
    assert len(rows) == 1
    assert rows[0][rr.ROW_ID] == held[rr.ROW_ID]
    assert rows[0][rr.ROW_ANCHOR] == rl.ORIGIN_PM_VERBATIMS
    assert held[rr.ROW_ID] in str(out)


def test_a_queued_origin_pm_revision_folds_into_a_pm_row_on_bind(repo: Path) -> None:
    _sizing(repo, NEW_REL, [])
    rl.start(repo, NEW)
    rl.revise(repo, NEW, verbatim=TURN21, origin="pm", turn={"session": "s-1", "ts": "t"})
    rl.bind(repo, NEW, NEW_REL, relayed_text="one-line relayed ask")
    row = next(r for r in _register(repo, NEW_REL).rows if r[rr.ROW_TEXT] == TURN21)
    assert row[rr.ROW_ANCHOR] == rl.ORIGIN_PM_VERBATIMS


def test_the_register_copy_keeps_a_pm_origin_row_pm_origin(repo: Path) -> None:
    _sizing(repo, OLD_REL, [])
    rl.start(repo, OLD)
    rl.bind(repo, OLD, OLD_REL, relayed_text="old relayed ask")
    rl.revise(repo, OLD, verbatim=TURN21, origin="pm", turn={"session": "s-1", "ts": "t"})
    _sizing(repo, NEW_REL, [])
    rl.start(repo, NEW, supersedes=OLD)
    rl.bind(repo, NEW, NEW_REL, relayed_text="new relayed ask")
    rl.carry_register(repo, OLD, NEW)
    row = next(r for r in _register(repo, NEW_REL).rows if r[rr.ROW_TEXT] == TURN21)
    assert row[rr.ROW_ANCHOR] == rl.ORIGIN_PM_VERBATIMS

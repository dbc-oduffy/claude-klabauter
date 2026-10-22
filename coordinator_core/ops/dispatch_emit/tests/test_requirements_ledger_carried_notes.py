"""requirements_ledger: resumed_from link, carried_notes and the view's notes section."""

from __future__ import annotations

import json

import pytest

from coordinator_core.ops.dispatch_emit import requirements_ledger as rl


def _note(repo, run_id, note, at):
    d = repo / "state" / "warp" / run_id
    d.mkdir(parents=True, exist_ok=True)
    with (d / rl.NOTES_FILE).open("a", encoding="utf-8") as f:
        f.write(json.dumps({"note": note, "at": at}) + "\n")


def test_start_preserves_resumed_from_across_rewrite(tmp_path):
    d = rl.start(tmp_path, "warp-new", resumed_from="ask-old")
    assert rl.read_run(d)[rl.RESUMED_FROM_FIELD] == "ask-old"
    rl.start(tmp_path, "warp-new")
    assert rl.read_run(d)[rl.RESUMED_FROM_FIELD] == "ask-old"


def test_start_without_link_writes_none(tmp_path):
    d = rl.start(tmp_path, "warp-new")
    assert rl.RESUMED_FROM_FIELD not in rl.read_run(d)


@pytest.mark.parametrize("bad", ["../x", "a/b", "warp-new", ""])
def test_start_refuses_unsafe_or_self_link(tmp_path, bad):
    with pytest.raises(rl.LedgerRefused):
        rl.start(tmp_path, "warp-new", resumed_from=bad)


def test_carried_notes_reads_both_dirs_oldest_first_and_dedups(tmp_path):
    _note(tmp_path, "ask-old", "first", "2026-10-21T10:00:00Z")
    _note(tmp_path, "warp-new", "second", "2026-10-22T10:00:00Z")
    _note(tmp_path, "warp-new", "first", "2026-10-21T10:00:00Z")
    rl.start(tmp_path, "warp-new", resumed_from="ask-old")
    got = rl.carried_notes(tmp_path, "warp-new")
    assert [(n["note"], n["run"]) for n in got] == [("first", "ask-old"), ("second", "warp-new")]


def test_carried_notes_missing_predecessor_yields_own(tmp_path):
    _note(tmp_path, "warp-new", "mine", "2026-10-22T10:00:00Z")
    rl.start(tmp_path, "warp-new", resumed_from="ask-gone")
    assert [n["note"] for n in rl.carried_notes(tmp_path, "warp-new")] == ["mine"]


def test_carried_notes_follows_second_hop(tmp_path):
    _note(tmp_path, "ask-first", "deep", "2026-10-20T10:00:00Z")
    rl.start(tmp_path, "ask-old", resumed_from="ask-first")
    rl.start(tmp_path, "warp-new", resumed_from="ask-old")
    assert [n["note"] for n in rl.carried_notes(tmp_path, "warp-new")] == ["deep"]


def test_carried_notes_cycle_terminates(tmp_path):
    _note(tmp_path, "ask-a", "na", "2026-10-20T10:00:00Z")
    _note(tmp_path, "ask-b", "nb", "2026-10-21T10:00:00Z")
    rl.start(tmp_path, "ask-a", resumed_from="ask-b")
    rl.start(tmp_path, "ask-b", resumed_from="ask-a")
    assert [n["note"] for n in rl.carried_notes(tmp_path, "ask-a")] == ["na", "nb"]


def test_carried_notes_hop_cap(tmp_path):
    n = rl.CARRIED_HOP_CAP + 4
    for i in range(n):
        _note(tmp_path, f"r{i}", f"n{i}", f"2026-10-01T10:00:{i:02d}Z")
        rl.start(tmp_path, f"r{i}", resumed_from=f"r{i + 1}" if i + 1 < n else None)
    got = rl.carried_notes(tmp_path, "r0")
    assert len(got) == rl.CARRIED_HOP_CAP + 1
    assert "n0" in [x["note"] for x in got] and f"n{n - 1}" not in [x["note"] for x in got]


def test_carried_notes_carry_id_and_retraction(tmp_path):
    _note(tmp_path, "ask-old", "first", "2026-10-21T10:00:00Z")
    d = tmp_path / "state" / "warp" / "ask-old"
    with (d / rl.NOTES_FILE).open("a", encoding="utf-8") as f:
        f.write(json.dumps({"retracts": "N1", "reason": "done", "at": "2026-10-21T11:00:00Z"}) + "\n")
    rl.start(tmp_path, "warp-new", resumed_from="ask-old")
    [got] = rl.carried_notes(tmp_path, "warp-new")
    assert got["id"] == "N1" and got["retracted"]["reason"] == "done"


def test_view_carries_a_revise_notes_section_when_notes_exist(tmp_path):
    _note(tmp_path, "ask-old", "also add the key", "2026-10-21T10:00:00Z")
    d = rl.start(tmp_path, "warp-new", resumed_from="ask-old")
    view = (d / rl.LEDGER_VIEW_FILE).read_text(encoding="utf-8")
    assert "## Revise notes" in view
    assert "> also add the key" in view
    assert "ask-old N1" in view


def test_view_has_no_revise_notes_section_without_notes(tmp_path):
    d = rl.start(tmp_path, "warp-new")
    assert "Revise notes" not in (d / rl.LEDGER_VIEW_FILE).read_text(encoding="utf-8")

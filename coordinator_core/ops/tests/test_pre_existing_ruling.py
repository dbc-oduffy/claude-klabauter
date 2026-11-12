from __future__ import annotations

import json

import pytest

from coordinator_core.ops.dispatch_emit import requirements_ledger as rl
from coordinator_core.ops.dispatch_emit import run_notes
from coordinator_core.ops.pre_existing_ruling import PRE_EXISTING_FILE, ruled_pre_existing

RUN = "ask-20261112T000000-aaaaaa"
PREV = "ask-20261111T000000-bbbbbb"
T = "pkg/test_x.py::test_a"


def _dir(repo, run):
    d = rl.run_dir(repo, run)
    d.mkdir(parents=True, exist_ok=True)
    return d


def _notes(repo, run, notes):
    p = _dir(repo, run) / rl.NOTES_FILE
    p.write_text("".join(json.dumps({"note": n, "at": f"2026-11-12T10:0{i}:00Z"}) + "\n" for i, n in enumerate(notes)), encoding="utf-8")
    return p


def test_nothing_recorded_is_empty(tmp_path):
    assert ruled_pre_existing(tmp_path, RUN, [T]) == {}


def test_structured_rows_rule_only_failing_ids_and_skip_malformed(tmp_path):
    rows = [{"nodeid": T, "reason": "red at base", "at": "x"}, {"nodeid": "other.py::t", "reason": "r"}, ["bad"]]
    (_dir(tmp_path, RUN) / PRE_EXISTING_FILE).write_text("\n".join(json.dumps(r) for r in rows) + "\n{nope\n", encoding="utf-8")
    assert ruled_pre_existing(tmp_path, RUN, [T, "unruled.py::t"]) == {T: "red at base"}


def test_live_note_with_word_and_id_rules_it(tmp_path):
    _notes(tmp_path, RUN, [f"{T} is pre-existing at base, do not revert"])
    assert list(ruled_pre_existing(tmp_path, RUN, [T])) == [T]
    _notes(tmp_path, RUN, [f"{T} is PRE_EXISTING"])
    assert list(ruled_pre_existing(tmp_path, RUN, [T])) == [T]


def test_note_without_ruling_word_does_not_rule(tmp_path):
    _notes(tmp_path, RUN, [f"please look at {T}"])
    assert ruled_pre_existing(tmp_path, RUN, [T]) == {}


def test_retracted_note_excluded(tmp_path):
    p = _notes(tmp_path, RUN, [f"{T} pre-existing"])
    with p.open("ab") as fh:
        fh.write(run_notes.retraction_line("N1", "wrong", "2026-11-12T11:00:00Z", "em"))
    assert ruled_pre_existing(tmp_path, RUN, [T]) == {}


def test_id_is_a_whole_token_both_ways(tmp_path):
    _notes(tmp_path, RUN, ["pkg/test_x.py::test_ab is pre-existing"])
    assert ruled_pre_existing(tmp_path, RUN, ["pkg/test_x.py::test_a"]) == {}
    assert list(ruled_pre_existing(tmp_path, RUN, ["pkg/test_x.py::test_ab"])) == ["pkg/test_x.py::test_ab"]
    _notes(tmp_path, RUN, ["pkg/test_x.py::test_a, pkg/test_x.py::test_ab pre-existing"])
    assert set(ruled_pre_existing(tmp_path, RUN, ["pkg/test_x.py::test_a", "pkg/test_x.py::test_ab"])) == {
        "pkg/test_x.py::test_a",
        "pkg/test_x.py::test_ab",
    }


def test_ruling_survives_resume_for_notes_and_structured(tmp_path):
    _notes(tmp_path, PREV, [f"{T} pre-existing"])
    (_dir(tmp_path, PREV) / PRE_EXISTING_FILE).write_text(json.dumps({"nodeid": "s.py::t", "reason": "r"}) + "\n", encoding="utf-8")
    rl._write_run(_dir(tmp_path, RUN), {rl.RESUMED_FROM_FIELD: PREV})
    got = ruled_pre_existing(tmp_path, RUN, [T, "s.py::t"])
    assert set(got) == {T, "s.py::t"} and got["s.py::t"] == "r"


def test_resume_cycle_terminates(tmp_path):
    rl._write_run(_dir(tmp_path, RUN), {rl.RESUMED_FROM_FIELD: PREV})
    rl._write_run(_dir(tmp_path, PREV), {rl.RESUMED_FROM_FIELD: RUN})
    assert ruled_pre_existing(tmp_path, RUN, [T]) == {}


def test_unsafe_run_key_refused(tmp_path):
    with pytest.raises(rl.LedgerRefused):
        ruled_pre_existing(tmp_path, "../x", [T])


@pytest.mark.parametrize("note", [
    f"{T} is NOT pre-existing",
    f"{T} is not a pre-existing failure",
    f"{T} is never pre_existing",
    f"{T} isn't pre-existing",
    f"{T} is non-pre-existing",
])
def test_negated_note_does_not_rule(tmp_path, note):
    _notes(tmp_path, RUN, [note])
    assert ruled_pre_existing(tmp_path, RUN, [T]) == {}

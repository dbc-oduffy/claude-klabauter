"""Pins the shared pre-halt credit predicate and its JS twin."""
from __future__ import annotations

import json
import shutil
import subprocess

from coordinator_core.win_portability import no_console_creationflags

import pytest

from coordinator_core.ops.dispatch_emit import pre_halt_credit as phc


def _resume(notes, blocking=("R1",), remaining=None):
    r = {"revise_notes": list(notes), "blocking": list(blocking)}
    if remaining is not None:
        r["remaining"] = remaining
    return r


def _verdict(met_by, status="met", rid="R1"):
    return {"items": [{"id": rid, "status": status, "met_by": met_by}]}


def test_df5e1f_shape():
    r = _resume(["Re-rule R1 as met by commit 2fd9d96c64 (15 parity tests pass)"])
    assert phc.credited_rows(r, _verdict(["2fd9d96c64"])) == {"R1"}


def test_5f687f_shape():
    r = _resume(["R1 met by d46d2a1b88, 18/18 tests"])
    assert phc.credited_rows(r, _verdict(["d46d2a1b88"])) == {"R1"}


def test_unnamed_sha_not_credited():
    r = _resume(["Re-rule R1 as met by commit 2fd9d96c64"])
    assert phc.credited_rows(r, _verdict(["abcdef1234"])) == frozenset()


def test_no_revise_note_no_credit():
    r = _resume([], remaining={"R1": {"evidence": "commit 2fd9d96c64"}})
    assert phc.credited_rows(r, _verdict(["2fd9d96c64"])) == frozenset()


def test_status_not_met_is_not_credited():
    r = _resume(["met by 2fd9d96c64"])
    assert phc.credited_rows(r, _verdict(["2fd9d96c64"], status="not_met")) == frozenset()


def test_7f92dc_shape_post_commit_row_met_by_halt_sha_is_credited():
    r = _resume(["R1 re-ruled: met by f3d01e5f1d (halt commit)"])
    assert phc.credited_rows(r, _verdict(["f3d01e5f1d"], status="post_commit")) == {"R1"}


def test_post_commit_row_with_unnamed_sha_is_not_credited():
    r = _resume(["R1 re-ruled: met by f3d01e5f1d"])
    assert phc.credited_rows(r, _verdict(["abcdef1234"], status="post_commit")) == frozenset()


def test_row_outside_blocking():
    r = _resume(["met by 2fd9d96c64"], blocking=["R2"])
    assert phc.credited_rows(r, _verdict(["2fd9d96c64"])) == frozenset()


def test_digit_only_and_letter_only_are_not_shas():
    assert phc.sha_tokens("1234567890 abcdefabcd 2fd9d96c64 2FD9D96C64") == ["2fd9d96c64"]
    assert not phc.note_rerules(["12345678", "deadbeef"])
    assert phc.note_rerules(["see 2fd9d96c64"])
    r = _resume(["met by 12345678"])
    assert phc.credited_rows(r, _verdict(["12345678"])) == frozenset()


def test_prefix_match_both_directions():
    long_note = _resume(["met by 2fd9d96c64e1a0b"])
    assert phc.credited_rows(long_note, _verdict("2fd9d96c64")) == {"R1"}
    short_note = _resume(["met by 2fd9d96c64"])
    assert phc.credited_rows(short_note, _verdict("2fd9d96c64e1a0b")) == {"R1"}


def test_row_remaining_evidence_is_known():
    r = _resume(["re-ruled"], remaining={"R1": {"evidence": "landed in 2fd9d96c64", "remaining": []}})
    assert phc.credited_rows(r, _verdict(["2fd9d96c64"])) == {"R1"}


@pytest.mark.parametrize(
    "resume,verdict",
    [
        (None, None), ("x", {}), ({}, {}), ({"revise_notes": "n", "blocking": ["R1"]}, {"items": []}),
        ({"revise_notes": ["2fd9d96c64"], "blocking": "R1"}, {"items": [{}]}),
        ({"revise_notes": ["2fd9d96c64"], "blocking": ["R1"]}, {"items": "x"}),
        ({"revise_notes": ["2fd9d96c64"], "blocking": ["R1"]}, {"items": [None, 3, {"id": "R1", "status": "met"}]}),
        ({"revise_notes": [object()], "blocking": [["R1"]], "remaining": 5}, {"items": [{"id": ["R1"], "status": "met"}]}),
    ],
)
def test_malformed_never_raises(resume, verdict):
    assert phc.credited_rows(resume, verdict) == frozenset()


@pytest.mark.spawns_process
@pytest.mark.cadence
@pytest.mark.skipif(shutil.which("node") is None, reason="node unavailable")
@pytest.mark.parametrize(
    "met_by,known",
    [
        (["2fd9d96c64"], "note 2fd9d96c64"),
        ("2fd9d96c64e1a0b", "2fd9d96c64"),
        ("2fd9d96c64", "2fd9d96c64e1a0b"),
        (["abcdef1234"], "2fd9d96c64"),
        (["12345678"], "12345678"),
        (None, "2fd9d96c64"),
    ],
)
def test_js_twin_agrees_with_python(met_by, known):
    src = f"console.log(JSON.stringify(({phc.CREDIT_MATCH_JS})({json.dumps(met_by)}, {json.dumps(known)})))"
    out = subprocess.run(["node", "-"], input=src, capture_output=True, text=True, check=True,
                         **no_console_creationflags()).stdout.strip()
    r = _resume([known])
    expected = bool(phc.credited_rows(r, _verdict(met_by)))
    assert json.loads(out) is expected


def test_with_notes_supplies_missing_revise_notes():
    r = {"blocking": ["R1"]}
    out = phc.with_notes(r, ["N1 re-rule R1 met by 739d3b77"])
    assert out["revise_notes"] == ["N1 re-rule R1 met by 739d3b77"]
    assert phc.credited_rows(out, _verdict(["739d3b7775"])) == {"R1"}
    assert "revise_notes" not in r


def test_with_notes_dedupes_and_keeps_order():
    r = _resume(["a", "b"])
    assert phc.with_notes(r, ["b", "c", "c"])["revise_notes"] == ["a", "b", "c"]


def test_with_notes_skips_non_strings():
    assert phc.with_notes({"blocking": []}, [1, None, "x", {"k": 1}])["revise_notes"] == ["x"]


def test_with_notes_unchanged_for_non_dict_or_empty():
    assert phc.with_notes(None, ["x"]) is None
    lst = ["y"]
    assert phc.with_notes(lst, ["x"]) is lst
    r = _resume(["a"])
    assert phc.with_notes(r, []) is r


def test_with_notes_does_not_mutate_input():
    r = _resume(["a"])
    out = phc.with_notes(r, ["b"])
    assert r["revise_notes"] == ["a"] and out is not r and out["revise_notes"] == ["a", "b"]

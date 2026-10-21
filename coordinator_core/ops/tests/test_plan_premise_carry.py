"""plan.premise_scope / plan.premise_record: zero fresh checks on an unchanged plan, set-based
line diff, flag and stale-key re-checks, refusal of a dropped flag, the skipped-citation basis cap,
row keying, CRLF equality, and zero spawns under the process-time bar.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from coordinator_core.benchmarks.spawn_counter import _count_spawns_attributed
from coordinator_core.ops import plan_premise_carry as op

REL = "docs/plans/2026-10-21-fixture.md"
V = "brief-v1"
LINES = [f"filler line {i}" for i in range(1, 17)]
LINES[4] = "Edit `alpha/one.py` here."
LINES[8] = "Read `beta/two.py` there."
LINES[13] = "See `gamma/three.py` too."


def _plan(root: Path, lines=None, eol: str = "\n") -> Path:
    path = root / REL
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes((eol.join(lines or LINES) + eol).encode("utf-8"))
    (root / ".git").mkdir(exist_ok=True)
    return root / ".git"


def _row(verdict: str, citation: str, line: int | None = None) -> dict:
    row = {"verdict": verdict, "citation": citation}
    if line is not None:
        row["line"] = line
    return row


def _rows() -> list:
    return [
        _row("RESOLVES", "alpha/one.py", 5),
        _row("UNRESOLVED", "beta/two.py", 9),
        _row("RESOLVES", "gamma/three.py", 14),
    ]


def _scope(common: Path, version: str = V) -> dict:
    return op._scope_handler({"plan": REL, "brief_version": version}, common)


def _record(common: Path, rows: list, **extra) -> dict:
    return op._record_handler({"plan": REL, "brief_version": V, "rows": rows, **extra}, common)


def test_unchanged_plan_refire_owes_zero_fresh_checks(tmp_path):
    common = _plan(tmp_path)
    rows = [_row("RESOLVES", "alpha/one.py", 5), _row("UNCHECKABLE", "gamma/three.py", 14)]
    _record(tmp_path / ".git", rows)
    out = _scope(common)
    assert out["first_pass"] is False
    assert out["changed_lines"] == []
    assert out["recheck"] == []
    assert out["carried"] == rows


def test_first_pass_when_no_record(tmp_path):
    out = _scope(_plan(tmp_path))
    assert (out["first_pass"], out["changed_lines"], out["recheck"], out["carried"]) == (True, [], [], [])


def test_edited_line_rechecks_only_its_citations_plus_prior_flags(tmp_path):
    common = _plan(tmp_path)
    _record(common, _rows())
    edited = list(LINES)
    edited[4] = "Edit `alpha/one.py` here, changed."
    edited.insert(4, "a brand new inserted line")
    _plan(tmp_path, edited)
    out = _scope(common)
    assert out["first_pass"] is False
    assert out["changed_lines"] == [
        {"line": 5, "text": "a brand new inserted line"},
        {"line": 6, "text": "Edit `alpha/one.py` here, changed."},
    ]
    assert sorted(r["citation"] for r in out["recheck"]) == ["alpha/one.py", "beta/two.py"]
    assert [r["citation"] for r in out["carried"]] == ["gamma/three.py"]


def test_declared_flag_stays_in_recheck_even_when_its_line_is_unchanged(tmp_path):
    common = _plan(tmp_path)
    _record(common, _rows())
    out = _scope(common)
    assert out["changed_lines"] == []
    assert [r["citation"] for r in out["recheck"]] == ["beta/two.py"]
    assert [r["citation"] for r in out["carried"]] == ["alpha/one.py", "gamma/three.py"]


def test_brief_version_mismatch_is_a_first_pass(tmp_path):
    common = _plan(tmp_path)
    _record(common, _rows())
    out = _scope(common, "brief-v2")
    assert (out["first_pass"], out["recheck"], out["carried"]) == (True, [], [])


def test_foreign_schema_and_different_plan_read_as_no_record(tmp_path):
    common = _plan(tmp_path)
    reply = _record(common, _rows())
    carry_file = tmp_path / reply["path"]
    saved = json.loads(carry_file.read_text(encoding="utf-8"))
    for patch in ({"schema": "premise-carry/2"}, {"plan": "docs/plans/other.md"}):
        carry_file.write_text(json.dumps({**saved, **patch}), encoding="utf-8")
        assert _scope(common)["first_pass"] is True
    carry_file.write_text("{not json", encoding="utf-8")
    assert _scope(common)["first_pass"] is True


def test_record_refuses_a_dropped_flag_and_writes_nothing(tmp_path):
    common = _plan(tmp_path)
    reply = _record(common, _rows())
    carry_file = tmp_path / reply["path"]
    before = carry_file.read_bytes()
    with pytest.raises(ValueError, match="beta/two.py"):
        _record(common, [_row("RESOLVES", "alpha/one.py", 5)])
    assert carry_file.read_bytes() == before
    again = _record(common, [], retired=["beta/two.py"])
    assert [r["citation"] for r in again["rows"]] == ["alpha/one.py", "gamma/three.py"]
    rejudged = _record(common, [_row("RESOLVES", "beta/two.py", 9)])
    assert rejudged["fresh"] == 1
    assert rejudged["carried"] == 2
    assert [r["verdict"] for r in rejudged["rows"]][-1] == "RESOLVES"


def test_record_merges_carried_then_fresh_in_order(tmp_path):
    common = _plan(tmp_path)
    _record(common, [_row("RESOLVES", "alpha/one.py", 5), _row("RESOLVES", "gamma/three.py", 14)])
    out = _record(common, [_row("RESOLVES", "gamma/three.py", 14)])
    assert [r["citation"] for r in out["rows"]] == ["alpha/one.py", "gamma/three.py"]
    assert (out["carried"], out["fresh"]) == (1, 1)


def test_citations_skipped_leaves_lines_past_the_largest_returned_line_changed(tmp_path):
    common = _plan(tmp_path)
    _record(common, [_row("RESOLVES", "alpha/one.py", 5)], citations_skipped=3)
    out = _scope(common)
    assert [c["line"] for c in out["changed_lines"]] == list(range(6, 17))


def test_row_without_line_keys_by_citation_and_unmatched_row_is_rechecked(tmp_path):
    common = _plan(tmp_path)
    reply = _record(
        common, [_row("RESOLVES", "alpha/one.py"), _row("RESOLVES", "nowhere/at-all.py")]
    )
    stored = json.loads((tmp_path / reply["path"]).read_text(encoding="utf-8"))["rows"]
    assert stored[0]["line_hash"] is not None
    assert stored[1]["line_hash"] is None
    out = _scope(common)
    assert [r["citation"] for r in out["recheck"]] == ["nowhere/at-all.py"]
    assert [r["citation"] for r in out["carried"]] == ["alpha/one.py"]


def test_crlf_and_lf_copies_of_one_plan_have_no_changed_lines(tmp_path):
    common = _plan(tmp_path)
    _record(common, _rows())
    _plan(tmp_path, eol="\r\n")
    assert _scope(common)["changed_lines"] == []


def test_unknown_param_and_bad_input_are_refused(tmp_path):
    common = _plan(tmp_path)
    with pytest.raises(ValueError, match="unknown param"):
        op._scope_handler({"plan": REL, "brief_version": V, "force": True}, common)
    with pytest.raises(ValueError, match="brief_version"):
        op._scope_handler({"plan": REL}, common)
    with pytest.raises(ValueError, match="verdict"):
        _record(common, [{"verdict": "MAYBE", "citation": "x"}])


@pytest.mark.parametrize("handler", ["_scope_handler", "_record_handler"])
def test_handlers_spawn_nothing_under_the_bar(tmp_path, monkeypatch, handler):
    common = _plan(tmp_path)
    params = {"plan": REL, "brief_version": V}
    if handler == "_record_handler":
        params["rows"] = _rows()
    with _count_spawns_attributed(monkeypatch) as spawns:
        start = time.process_time()
        getattr(op, handler)(params, common)
        elapsed_ms = (time.process_time() - start) * 1000
    assert spawns == [], [s.argv for s in spawns]
    assert elapsed_ms < 500, elapsed_ms

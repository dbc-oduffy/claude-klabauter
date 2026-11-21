"""`bug_blitz.rulings`: the `rulings` arg shape and the `pm_ruling` write onto matching open rows."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from coordinator_core.bug_blitz import rulings as mod
from coordinator_core.bug_blitz.rulings import Ruling, apply_rulings, open_row_paths, parse_rulings
from coordinator_core.ops.dispatch_emit import minted_records
from coordinator_core.ops.dispatch_emit.requirements_ledger import VERBATIM_BYTE_CAP

QUEUE = "state/bug-backlog"


def _row(root: Path, stem: str, title: str, *, status: str = "open", extra: str = "") -> str:
    d = root / QUEUE
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{stem}.yaml").write_text(
        f"created: 2026-11-21\ntitle: {json.dumps(title)}\nbody: b\nstatus: {status}\nseverity: P1\ntags:\n  - tf\n{extra}",
        encoding="utf-8",
    )
    return f"{QUEUE}/{stem}.yaml"


def _field(root: Path, rel: str, key: str = "pm_ruling"):
    return yaml.safe_load((root / rel).read_text(encoding="utf-8")).get(key)


@pytest.fixture(autouse=True)
def _no_declare(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("coordinator_core.bug_blitz.mint.declare_write", lambda rel: None)


def test_parse_string_and_list_and_context_alias() -> None:
    assert parse_rulings("measure it") == ((Ruling(None, "measure it"),), None)
    got, bad = parse_rulings([{"match": "a.py", "ruling": "r1"}, {"match": "b", "context": "r2"}])
    assert bad is None
    assert got == (Ruling("a.py", "r1"), Ruling("b", "r2"))
    assert parse_rulings(None) == ((), None)


@pytest.mark.parametrize(
    "raw",
    [
        5,
        {"match": "a", "ruling": "r"},
        [1],
        [{"match": "", "ruling": "r"}],
        [{"match": "a"}],
        [{"match": "a", "ruling": " "}],
        [{"match": "a", "ruling": "r", "context": "c"}],
        [{"match": "a", "ruling": "r", "extra": 1}],
        "   ",
        "x" * (VERBATIM_BYTE_CAP + 1),
    ],
)
def test_parse_refuses_a_malformed_shape(raw) -> None:
    got, bad = parse_rulings(raw)
    assert bad and got == ()


def test_parse_admits_a_ruling_at_the_cap() -> None:
    assert parse_rulings("x" * VERBATIM_BYTE_CAP)[1] is None


def test_matching_rows_get_the_ruling_and_others_none_and_edits_are_recorded(tmp_path: Path) -> None:
    a = _row(tmp_path, "TF-a", "TF: pkg/test_a.py::test_one")
    b = _row(tmp_path, "TF-b", "TF: pkg/test_b.py::test_two")
    run_dir = tmp_path / "run"
    applied, unmatched = apply_rulings(
        tmp_path, "R1", [a, b], (Ruling("test_a.py", "measure, do not bump"), Ruling("nowhere", "x")), run_dir
    )
    assert applied == ["TF-a"] and unmatched == ["nowhere"]
    assert _field(tmp_path, a) == "measure, do not bump"
    assert _field(tmp_path, b) is None
    assert _field(tmp_path, a, "tags") == ["tf"]
    assert minted_records.read(run_dir) == [{"path": a, "created": False}]


def test_match_by_row_id_and_several_matches_join_in_arg_order(tmp_path: Path) -> None:
    a = _row(tmp_path, "TF-a", "TF: one")
    apply_rulings(tmp_path, "R1", [a], (Ruling("TF-a", "first"), Ruling("one", "second"), Ruling("TF", "first")), tmp_path / "r")
    assert _field(tmp_path, a) == "first\n\nsecond"


def test_string_ruling_applies_to_every_row_and_reports_no_unmatched(tmp_path: Path) -> None:
    rows = [_row(tmp_path, "TF-a", "A"), _row(tmp_path, "TF-b", "B")]
    applied, unmatched = apply_rulings(tmp_path, "R1", rows, (Ruling(None, "all"),), tmp_path / "r")
    assert applied == ["TF-a", "TF-b"] and unmatched == []
    assert [_field(tmp_path, r) for r in rows] == ["all", "all"]


def test_an_unchanged_value_writes_nothing_and_records_nothing(tmp_path: Path) -> None:
    a = _row(tmp_path, "TF-a", "A")
    run_dir = tmp_path / "r"
    apply_rulings(tmp_path, "R1", [a], (Ruling(None, "same"),), run_dir)
    before = (tmp_path / a).read_bytes()
    run_dir2 = tmp_path / "r2"
    applied, _ = apply_rulings(tmp_path, "R2", [a], (Ruling(None, "same"),), run_dir2)
    assert applied == ["TF-a"]
    assert (tmp_path / a).read_bytes() == before
    assert minted_records.read(run_dir2) == []


def test_an_existing_block_scalar_ruling_is_kept_and_extended(tmp_path: Path) -> None:
    a = _row(tmp_path, "TF-a", "A", extra="pm_ruling: |-\n  keep this\n  and this\nnotes: after\n")
    apply_rulings(tmp_path, "R1", [a], (Ruling(None, "new"),), tmp_path / "r")
    assert _field(tmp_path, a) == "keep this\nand this\n\nnew"
    assert _field(tmp_path, a, "notes") == "after"


def test_a_closed_or_missing_row_is_skipped(tmp_path: Path) -> None:
    done = _row(tmp_path, "TF-done", "D", status="closed")
    applied, unmatched = apply_rulings(tmp_path, "R1", [done, f"{QUEUE}/TF-ghost.yaml"], (Ruling(None, "x"),), tmp_path / "r")
    assert applied == [] and unmatched == ["(every row)"]
    assert _field(tmp_path, done) is None


def test_special_characters_round_trip(tmp_path: Path) -> None:
    a = _row(tmp_path, "TF-a", "A")
    text = 'quote " colon: # hash\nline two é'
    apply_rulings(tmp_path, "R1", [a], (Ruling(None, text),), tmp_path / "r")
    assert _field(tmp_path, a) == text


def test_crlf_row_keeps_its_line_endings(tmp_path: Path) -> None:
    a = _row(tmp_path, "TF-a", "A")
    p = tmp_path / a
    p.write_bytes(p.read_bytes().replace(b"\n", b"\r\n"))
    apply_rulings(tmp_path, "R1", [a], (Ruling(None, "r"),), tmp_path / "run")
    raw = p.read_bytes()
    assert raw.count(b"\n") == raw.count(b"\r\n")
    assert _field(tmp_path, a) == "r"


def test_open_row_paths_lists_only_rows_a_match_could_name(tmp_path: Path) -> None:
    hit_stem = _row(tmp_path, "needle-row", "other")
    hit_title = _row(tmp_path, "r2", "has the needle inside")
    _row(tmp_path, "r3", "unrelated")
    (tmp_path / QUEUE / "note.md").write_text("needle", encoding="utf-8")
    assert open_row_paths(tmp_path, (Ruling("needle", "x"),)) == [hit_stem, hit_title]
    assert open_row_paths(tmp_path, (Ruling(None, "x"),)) == []


def test_pm_ruling_edit_that_would_not_round_trip_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    a = _row(tmp_path, "TF-a", "A")
    monkeypatch.setattr(mod, "_with_pm_ruling", lambda text, value: text)
    with pytest.raises(ValueError, match="clean YAML"):
        apply_rulings(tmp_path, "R1", [a], (Ruling(None, "x"),), tmp_path / "r")

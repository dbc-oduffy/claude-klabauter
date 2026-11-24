"""A review wave's own applied fix never trips verdict-pin-stale: files the wave's applied ledger rows
rewrote after the pin are the reviewed product, in every ledger shape (several files, several rows on
one file, chained rows, ``./`` or absolute ``file`` forms); a foreign rewrite still refuses."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit.terminal_commit_review import (
    review_owed_refusal,
    verdict_pin_mismatches,
)
from coordinator_core.session.touch_record import compute_content_hash

_SIDECAR = "reviewer-sidecar.md"
_FILES = ["pkg/a.py", "pkg/b.py", "pkg/c.py", "pkg/d.py"]


def _write(root: Path, rel: str, text: str) -> None:
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8", newline="\n")


def _sidecar(root: Path, pins: dict, rows: list, name: str = _SIDECAR) -> None:
    lines = "".join(f"  {p}: '{compute_content_hash(root / p)}'\n" for p in pins)
    ledger = "## Findings Ledger\n\n```json\n" + json.dumps(rows) + "\n```\n"
    (root / name).write_text(
        f"---\nverdict: PASS\njudged_sha256:\n{lines}---\n{ledger}", encoding="utf-8", newline="\n"
    )


def _row(i: int, file: str, before: str, after: str) -> dict:
    return {"id": f"f{i}", "file": file, "before": before, "after": after, "status": "applied", "verified": True}


def _review(*names: str) -> dict:
    return {"wave_sidecar_paths": list(names or [_SIDECAR])}


def _judged(root: Path) -> None:
    for i, rel in enumerate(_FILES):
        _write(root, rel, f"v{i} = 0\n")


def test_one_wave_fix_over_several_files_lands(tmp_path: Path) -> None:
    _judged(tmp_path)
    rows = [_row(i, rel, f"v{i} = 0", f"v{i} = 1") for i, rel in enumerate(_FILES)]
    _sidecar(tmp_path, _FILES, rows)
    for i, rel in enumerate(_FILES):
        _write(tmp_path, rel, f"v{i} = 1\n")

    assert verdict_pin_mismatches(tmp_path, _review()) == []
    assert review_owed_refusal(tmp_path, _review()) is None


def test_two_rows_on_one_file_both_land(tmp_path: Path) -> None:
    _judged(tmp_path)
    _write(tmp_path, "pkg/a.py", "x = 0\ny = 0\n")
    _sidecar(tmp_path, ["pkg/a.py"], [_row(0, "pkg/a.py", "x = 0", "x = 1"), _row(1, "pkg/a.py", "y = 0", "y = 1")])
    _write(tmp_path, "pkg/a.py", "x = 1\ny = 1\n")

    assert verdict_pin_mismatches(tmp_path, _review()) == []


def test_chained_rows_on_one_file_land_when_the_later_row_rewrote_the_earlier_after(tmp_path: Path) -> None:
    _judged(tmp_path)
    rows = [_row(0, "pkg/a.py", "v0 = 0", "v0 = 1"), _row(1, "pkg/a.py", "v0 = 1", "v0 = 2")]
    _sidecar(tmp_path, ["pkg/a.py"], rows)
    _write(tmp_path, "pkg/a.py", "v0 = 2\n")

    assert verdict_pin_mismatches(tmp_path, _review()) == []
    assert review_owed_refusal(tmp_path, _review()) is None


def test_a_chain_whose_last_row_is_absent_still_refuses(tmp_path: Path) -> None:
    _judged(tmp_path)
    rows = [_row(0, "pkg/a.py", "v0 = 0", "v0 = 1"), _row(1, "pkg/a.py", "v0 = 1", "v0 = 2")]
    _sidecar(tmp_path, ["pkg/a.py"], rows)
    _write(tmp_path, "pkg/a.py", "v0 = 9\n")

    assert verdict_pin_mismatches(tmp_path, _review()) == ["pkg/a.py"]


@pytest.mark.parametrize("form", ["./pkg/a.py", "pkg\\a.py", "ABS"])
def test_a_ledger_file_in_any_path_form_matches_its_pin_key(tmp_path: Path, form: str) -> None:
    _judged(tmp_path)
    named = str(tmp_path / "pkg" / "a.py") if form == "ABS" else form
    _sidecar(tmp_path, ["pkg/a.py"], [_row(0, named, "v0 = 0", "v0 = 1")])
    _write(tmp_path, "pkg/a.py", "v0 = 1\n")

    assert verdict_pin_mismatches(tmp_path, _review()) == []


def test_the_fix_lands_when_a_second_wave_sidecar_pins_the_pre_fix_bytes(tmp_path: Path) -> None:
    _judged(tmp_path)
    _sidecar(tmp_path, ["pkg/a.py"], [_row(0, "pkg/a.py", "v0 = 0", "v0 = 1")], "first.md")
    _sidecar(tmp_path, _FILES, [], "second.md")
    for i, rel in enumerate(_FILES[:1]):
        _write(tmp_path, rel, f"v{i} = 1\n")

    assert verdict_pin_mismatches(tmp_path, _review("first.md", "second.md")) == []


def test_a_foreign_rewrite_after_the_pin_still_refuses_even_beside_a_landed_fix(tmp_path: Path) -> None:
    _judged(tmp_path)
    rows = [_row(i, rel, f"v{i} = 0", f"v{i} = 1") for i, rel in enumerate(_FILES)]
    _sidecar(tmp_path, _FILES, rows)
    for i, rel in enumerate(_FILES):
        _write(tmp_path, rel, f"v{i} = 1\n")
    _write(tmp_path, "pkg/c.py", "v2 = 'foreign'\n")

    assert verdict_pin_mismatches(tmp_path, _review()) == ["pkg/c.py"]
    reason = review_owed_refusal(tmp_path, _review())
    assert reason is not None and "pkg/c.py" in reason and "pkg/a.py" not in reason


def test_a_rewrite_with_no_applied_fix_present_refuses(tmp_path: Path) -> None:
    _judged(tmp_path)
    _sidecar(tmp_path, _FILES, [])
    _write(tmp_path, "pkg/b.py", "v1 = 5\n")

    assert verdict_pin_mismatches(tmp_path, _review()) == ["pkg/b.py"]

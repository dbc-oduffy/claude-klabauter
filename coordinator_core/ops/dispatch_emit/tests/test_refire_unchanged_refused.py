"""An unchanged criterion_not_met refire is refused naming the unchanged brief; any change is not."""

from __future__ import annotations

import json
from pathlib import Path

from coordinator_core.ops.dispatch_emit import halted_run, refire_unchanged

PRIOR = "ask-20261001T000000-aaaaaa"
NEW = "ask-20261002T000000-bbbbbb"
BRIEF = "Execute C1 for run {run}\nwrites: a.py\n"


def _prior(tmp_path: Path, halted: str = "criterion_not_met", rows=(("C1", ["a.py"]),)) -> Path:
    run_dir = tmp_path / "state" / "warp" / PRIOR
    run_dir.mkdir(parents=True)
    manifest_rows = []
    for row_id, writes in rows:
        rel = f"scratch/warp/{PRIOR}/briefs/{row_id}.md"
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(BRIEF.format(run=PRIOR), encoding="utf-8")
        manifest_rows.append({"id": row_id, "brief_path": rel, "writes": writes})
    (run_dir / "manifest.json").write_text(json.dumps({"rows": manifest_rows}), encoding="utf-8")
    (run_dir / "halted.json").write_text(json.dumps({"status": halted_run.STATUS_HALTED, "halted": halted}), encoding="utf-8")
    return run_dir


def _check(tmp_path, run_dir, new_rows):
    return refire_unchanged.check(tmp_path, run_dir, NEW, new_rows)


def test_equal_rows_are_refused_naming_the_brief(tmp_path):
    run_dir = _prior(tmp_path)
    reply = _check(tmp_path, run_dir, {"C1": (["a.py"], BRIEF.format(run=NEW))})
    assert reply is not None
    assert "briefs/C1.md" in reply and PRIOR in reply and "criterion_not_met" in reply


def test_writes_order_and_backslashes_do_not_count_as_change(tmp_path):
    run_dir = _prior(tmp_path, rows=(("C1", ["a/b.py", "c.py"]),))
    brief = BRIEF.format(run=NEW)
    assert _check(tmp_path, run_dir, {"C1": (["c.py", "a\\b.py"], brief)}) is not None


def test_changed_writes_is_not_refused(tmp_path):
    run_dir = _prior(tmp_path)
    assert _check(tmp_path, run_dir, {"C1": (["a.py", "b.py"], BRIEF.format(run=NEW))}) is None


def test_changed_brief_is_not_refused(tmp_path):
    run_dir = _prior(tmp_path)
    assert _check(tmp_path, run_dir, {"C1": (["a.py"], BRIEF.format(run=NEW) + "## Revise notes\n")}) is None


def test_added_row_is_not_refused(tmp_path):
    run_dir = _prior(tmp_path)
    brief = BRIEF.format(run=NEW)
    assert _check(tmp_path, run_dir, {"C1": (["a.py"], brief), "W1": (["z.py"], brief)}) is None


def test_other_halt_reason_is_not_refused(tmp_path):
    run_dir = _prior(tmp_path, halted="plan_gate")
    assert _check(tmp_path, run_dir, {"C1": (["a.py"], BRIEF.format(run=NEW))}) is None


def test_all_kept_refire_is_not_refused(tmp_path):
    run_dir = _prior(tmp_path)
    assert _check(tmp_path, run_dir, {}) is None


def test_missing_manifest_or_marker_is_not_refused(tmp_path):
    run_dir = _prior(tmp_path)
    new_rows = {"C1": (["a.py"], BRIEF.format(run=NEW))}
    (run_dir / "manifest.json").unlink()
    assert refire_unchanged.fingerprint(run_dir) == {}
    assert _check(tmp_path, run_dir, new_rows) is None
    (run_dir / "halted.json").unlink()
    assert _check(tmp_path, run_dir, new_rows) is None


def test_fingerprint_row_normalises_only_the_run_id():
    a = refire_unchanged.fingerprint_row(["x.py"], f"run {PRIOR}", PRIOR)
    assert a == refire_unchanged.fingerprint_row(["x.py"], f"run {NEW}", NEW)
    assert a != refire_unchanged.fingerprint_row(["x.py"], f"run {NEW} more", NEW)

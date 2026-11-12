"""``_head_baseline`` falls back to a node-level HEAD measure when the whole-file HEAD pass is unusable,
and ``_write_dual`` records live-only reds as foreign ``undeclared-dirt``. Stub runner and exports."""
from __future__ import annotations

import json
from pathlib import Path

from coordinator_core.ops.dispatch_emit import landing_targeted_tests, retest_targeted
from coordinator_core.ops.dispatch_emit.tests.test_retest_isolated_baseline import _setup

RATCHET = "test_a.py::t0"


def test_ran_short_whole_file_falls_back_to_node_measure_and_records_foreign(tmp_path, monkeypatch):
    _, record, _ = _setup(
        tmp_path, monkeypatch, export_red=[RATCHET], head_red=[RATCHET], head_ran_short=1,
    )
    baseline = record["baseline"]
    assert "baseline_skipped" not in record
    assert baseline["method"] == "head-node-measure"
    assert baseline["caused"] == [] and baseline["foreign"] == [RATCHET] and baseline["evidence"] == [RATCHET]
    assert baseline["skipped_whole_file"].startswith("HEAD run did not run what it collected")
    assert "head_red" in baseline and "owed_cleared" in baseline
    assert landing_targeted_tests.failed_targeted_tests(record) == ([], "pre-existing-only")


def test_node_measure_keeps_a_run_caused_red_caused(tmp_path, monkeypatch):
    _, record, _ = _setup(
        tmp_path, monkeypatch, export_red=[RATCHET], head_red=[], head_ran_short=1,
    )
    baseline = record["baseline"]
    assert baseline["method"] == "head-node-measure"
    assert baseline["caused"] == [RATCHET] and baseline["foreign"] == []
    assert landing_targeted_tests.failed_targeted_tests(record)[1] == "failed"


def test_node_measure_unmeasurable_keeps_baseline_skipped(tmp_path, monkeypatch):
    _, record, _ = _setup(tmp_path, monkeypatch, export_red=[RATCHET], head_red=[RATCHET], head_collect_error=True)
    assert "baseline" not in record
    assert "test_a.py" in record["baseline_skipped"]["reason"]
    assert landing_targeted_tests.failed_targeted_tests(record)[1] == "failed"


def _dual(tmp_path: Path, *, export_summary: bool, baseline, live_red: list[str]) -> dict:
    root = tmp_path / "repo"
    root.mkdir()
    export_root = tmp_path / "export"
    export_root.mkdir()
    export_log, live_log, path = tmp_path / "e.log", tmp_path / "l.log", tmp_path / "rec" / "v.json"
    export_log.write_text(
        "FAILED test_a.py::t0 - boom\n" + ("1 passed, 1 failed in 0.1s\n" if export_summary else ""), encoding="utf-8",
    )
    live_log.write_text("".join(f"FAILED {n} - boom\n" for n in live_red), encoding="utf-8")
    path.parent.mkdir()
    meta = {"root": str(export_root), "write_set": []}
    retest_targeted._write_dual(
        root, path, live_log, export_log, "run-x", ["test_a.py"], [], "", meta, lambda r: [], lambda r: "h" * 40,
        ("fail", None, [1], True), ("fail", None, [1], True), None, baseline,
    )
    return json.loads(path.read_text(encoding="utf-8"))


def _base():
    return {"baseline": {"method": "isolated-export", "caused": [], "foreign": ["test_a.py::t0"],
                         "evidence": ["test_a.py::t0"]}}


def test_live_only_red_is_foreign_undeclared_dirt(tmp_path):
    record = _dual(tmp_path, export_summary=True, baseline=_base(), live_red=["test_a.py::t0", "test_b.py::dirt"])
    assert record["baseline"]["foreign"] == ["test_a.py::t0", "test_b.py::dirt"]
    assert record["baseline"]["foreign_reason"] == {"test_b.py::dirt": "undeclared-dirt"}
    assert record["baseline"]["caused"] == []


def test_baseline_skipped_gains_no_baseline_from_dirt(tmp_path):
    skipped = {"baseline_skipped": {"reason": "x", "files": {}}}
    record = _dual(tmp_path, export_summary=True, baseline=skipped, live_red=["test_b.py::dirt"])
    assert "baseline" not in record and "baseline_skipped" in record


def test_export_pass_without_a_summary_records_no_dirt(tmp_path):
    record = _dual(tmp_path, export_summary=False, baseline=_base(), live_red=["test_b.py::dirt"])
    assert record["baseline"]["foreign"] == ["test_a.py::t0"]
    assert "foreign_reason" not in record["baseline"]

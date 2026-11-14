"""Retest child: a red always-run guard gets per-touched-file tree and HEAD hit counts, with no new pytest session."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from coordinator_core.ops.dispatch_emit import landing_export as le
from coordinator_core.ops.dispatch_emit import retest_targeted as rt
from coordinator_core.ops.dispatch_emit.always_run_guards import GUARD_HITS_KEY

GUARD_FILE = "tests/test_guard.py"
GUARD = f"{GUARD_FILE}::test_no_literal"
TOUCHED = "src/a.py"
RED_LINE = f"FAILED {GUARD} - AssertionError\n"


def _repo(tmp_path: Path) -> Path:
    (tmp_path / "coordinator.local.md").write_text(
        f'---\nalways_run_guards: ["{GUARD}"]\n---\n', encoding="utf-8")
    (tmp_path / GUARD_FILE).parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / GUARD_FILE).write_text("", encoding="utf-8")
    return tmp_path


def _hits_lines(n: int) -> str:
    return "".join(f"{TOUCHED}:{i}: literal\n" for i in range(1, n + 1))


def test_red_on_both_records_tree_and_head_counts(tmp_path):
    root = _repo(tmp_path)
    export_log, head_log = tmp_path / "r.export.log", tmp_path / "r.head.log"
    export_log.write_text(_hits_lines(3) + RED_LINE, encoding="utf-8")
    head_log.write_text(_hits_lines(1) + RED_LINE, encoding="utf-8")

    got = rt._guard_hits(root, tmp_path / "export", export_log, head_log, {"baseline": {"method": "export"}}, [TOUCHED])

    assert got == {GUARD_HITS_KEY: {GUARD: {"tree": {TOUCHED: 3}, "head": {TOUCHED: 1}}}}


def test_head_node_measure_reads_the_node_log(tmp_path):
    root = _repo(tmp_path)
    export_log, head_log = tmp_path / "r.export.log", tmp_path / "r.head.log"
    export_log.write_text(_hits_lines(2) + RED_LINE, encoding="utf-8")
    head_log.write_text(_hits_lines(9), encoding="utf-8")
    head_log.with_suffix(".node.log").write_text(_hits_lines(2) + RED_LINE, encoding="utf-8")

    got = rt._guard_hits(root, tmp_path / "export", export_log, head_log, {"baseline": {"method": "head-node-measure"}}, [TOUCHED])

    assert got[GUARD_HITS_KEY][GUARD]["head"] == {TOUCHED: 2}


def test_red_on_tree_only_records_zero_head_hits(tmp_path):
    root = _repo(tmp_path)
    export_log, head_log = tmp_path / "r.export.log", tmp_path / "r.head.log"
    export_log.write_text(_hits_lines(2) + RED_LINE, encoding="utf-8")
    head_log.write_text("1 passed in 0.1s\n", encoding="utf-8")

    got = rt._guard_hits(root, tmp_path / "export", export_log, head_log, {"baseline": {}}, [TOUCHED])

    assert got[GUARD_HITS_KEY][GUARD] == {"tree": {TOUCHED: 2}, "head": {TOUCHED: 0}}


def test_green_guard_records_no_key(tmp_path):
    root = _repo(tmp_path)
    export_log, head_log = tmp_path / "r.export.log", tmp_path / "r.head.log"
    export_log.write_text("FAILED tests/test_other.py::test_x - boom\n", encoding="utf-8")
    head_log.write_text("", encoding="utf-8")

    assert rt._guard_hits(root, tmp_path / "export", export_log, head_log, {"baseline": {}}, [TOUCHED]) == {}


def test_skipped_baseline_or_absent_head_log_records_no_entry(tmp_path):
    root = _repo(tmp_path)
    export_log, head_log = tmp_path / "r.export.log", tmp_path / "r.head.log"
    export_log.write_text(_hits_lines(2) + RED_LINE, encoding="utf-8")
    head_log.write_text(_hits_lines(2), encoding="utf-8")

    skipped = {"baseline_skipped": {"reason": "x", "files": {}}}
    assert rt._guard_hits(root, tmp_path / "export", export_log, head_log, skipped, [TOUCHED]) == {}
    head_log.unlink()
    assert rt._guard_hits(root, tmp_path / "export", export_log, head_log, {"baseline": {}}, [TOUCHED]) == {}


def test_isolated_body_writes_the_key_into_the_record_and_runs_no_extra_session(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    record = tmp_path / "scratch" / "r.json"
    passes = []

    def run_pass(run, pass_root, tests, log, deadline):
        passes.append(log.name)
        log.write_text(_hits_lines(3) + RED_LINE if log.name.endswith(".export.log") else "", encoding="utf-8")
        return ("fail", "", [1], True) if log.name.endswith(".export.log") else ("pass", "", [0], True)

    def head_baseline(root_, run, tests, export_root, export_log, head_log, *a, **kw):
        head_log.write_text(_hits_lines(1) + RED_LINE, encoding="utf-8")
        return {"baseline": {"method": "export", "caused": [], "foreign": [GUARD], "evidence": [GUARD]}}

    captured = {}
    monkeypatch.setattr(le, "build_head_export", lambda *a, **k: SimpleNamespace(head_sha="h" * 40))
    monkeypatch.setattr(rt, "_landing_paths", lambda *a: ([TOUCHED], "t"))
    monkeypatch.setattr(rt, "_overlay_sha", lambda *a: None)
    monkeypatch.setattr(rt, "_run_pass", run_pass)
    monkeypatch.setattr(rt, "_head_baseline", head_baseline)
    monkeypatch.setattr(rt.landing_retest, "run_base_for", lambda *a: None)
    def write_dual(
        root_, path, log_path, export_log, run_id, tests, dropped, drop_note, meta, dirty, head_sha,
        export_pass, live_pass, fail, baseline=None,
    ):
        captured["baseline"] = baseline
        return 1

    monkeypatch.setattr(rt, "_write_dual", write_dual)

    rt._isolated_body(root, record, tmp_path / "live.log", "r1", {}, [GUARD_FILE], [], "", None, None, None)

    assert captured["baseline"][GUARD_HITS_KEY][GUARD] == {"tree": {TOUCHED: 3}, "head": {TOUCHED: 1}}
    assert passes == ["r.export.log", "live.log"]

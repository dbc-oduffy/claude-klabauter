"""A failing retest verdict names its node ids; with none parsable it is typed infra, never a test failure."""
from __future__ import annotations

import json
from pathlib import Path

from coordinator_core.ops.dispatch_emit import landing_retest, retest_targeted


def test_captured_log_error_line_is_not_a_node_id(tmp_path: Path) -> None:
    log = tmp_path / "x.log"
    log.write_text(
        "ERROR example-retrieval-repo.embed:embed.py:10038 tc-30: embed failed {'a'::1}\n"
        "FAILED tests/test_a.py::TestX::test_y - assert 0\n",
        encoding="utf-8",
    )
    assert retest_targeted._failed_node_ids(log, tmp_path) == ["tests/test_a.py::TestX::test_y"]


def test_verbose_progress_lines_name_the_id_when_the_summary_is_off(tmp_path: Path) -> None:
    log = tmp_path / "x.log"
    log.write_text("tests/test_a.py::test_y FAILED                 [ 50%]\n", encoding="utf-8")
    assert retest_targeted._failed_node_ids(log, tmp_path) == ["tests/test_a.py::test_y"]


def _dual(tmp_path: Path, monkeypatch, log_text: str) -> dict:
    monkeypatch.setattr(landing_retest, "landing_fingerprint", lambda *_a, **_k: "fp")
    export_log = tmp_path / "r.export.log"
    export_log.write_text(log_text, encoding="utf-8")
    live_log = tmp_path / "r.log"
    live_log.write_text("", encoding="utf-8")
    meta = {"root": str(tmp_path), "write_set": []}
    out = tmp_path / "r.json"
    retest_targeted._write_dual(
        tmp_path, out, live_log, export_log, "run", ["tests/test_a.py"], [], "", meta, [], lambda _r: "head",
        ("fail", "", [1], True), ("pass", "", [0], True), lambda *a, **k: 2,
    )
    return json.loads(out.read_text(encoding="utf-8"))


def test_fail_with_no_parsable_node_id_is_typed_infra_error(tmp_path: Path, monkeypatch) -> None:
    record = _dual(tmp_path, monkeypatch, "1 failed in 0.1s\n")
    assert record["status"] == "error" and record["export"]["status"] == "error"
    assert record[retest_targeted.landing_targeted_tests.ZERO_RUN_CAUSE_KEY] == retest_targeted.NODELESS_FAIL_CAUSE
    assert "names no failing node id" in record["reason"]


def test_fail_with_a_node_id_stays_a_fail_naming_it(tmp_path: Path, monkeypatch) -> None:
    record = _dual(tmp_path, monkeypatch, "FAILED tests/test_a.py::test_y - assert 0\n")
    assert record["status"] == "fail"
    assert record[landing_retest.FAILED_TESTS_PARAM] == ["tests/test_a.py::test_y"]

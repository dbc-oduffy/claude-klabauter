"""tests-reverify record writer and latest_tests_record reader."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import verdict_supersession as vs

PLAN = "pln-x-123456"
EPOCH = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _write(root: Path, name: str, **fm: str) -> None:
    d = root / vs.VERDICT_DIR / "2026-10"
    d.mkdir(parents=True, exist_ok=True)
    lines = ["---", *[f"{k}: {v}" for k, v in fm.items()], "---", ""]
    (d / name).write_text("\n".join(lines), encoding="utf-8", newline="\n")


def test_writer_round_trip(tmp_path):
    baseline = {"method": "export", "pre_existing": ["a"], "caused": [], "unverified": []}
    rel = vs.record_tests_supersession(
        repo_root=tmp_path,
        plan_id=PLAN,
        tests={"status": "fail", "run": 3, "failed": 1, "sidecar": "s.md",
               "baseline_method": "export", "baseline": baseline},
        session_id="sess",
    )
    assert rel.startswith("state/delivery-verdicts/") and ".tests." in rel and "\\" not in rel
    assert "\r" not in (tmp_path / rel).read_bytes().decode()
    got = vs.latest_tests_record(tmp_path, PLAN, EPOCH)
    assert got["record_path"] == rel
    assert got["kind"] == vs.TESTS_RECORD_KIND
    assert got["session_id"] == "sess"
    assert got["tests"] == {"status": "fail", "run": 3, "failed": 1, "sidecar": "s.md",
                            "baseline_method": "export", "baseline": baseline}


def test_writer_refuses_missing_plan_or_status(tmp_path):
    with pytest.raises(ValueError):
        vs.record_tests_supersession(repo_root=tmp_path, plan_id="", tests={"status": "pass"})
    with pytest.raises(ValueError):
        vs.record_tests_supersession(repo_root=tmp_path, plan_id=PLAN, tests={"run": 1})
    assert not (tmp_path / "state").exists()


def test_reader_refuses_empty_plan_id(tmp_path):
    with pytest.raises(ValueError):
        vs.latest_tests_record(tmp_path, "", EPOCH)


def test_no_store_returns_none(tmp_path):
    assert vs.latest_tests_record(tmp_path, PLAN, EPOCH) is None


def test_newest_wins_by_recorded_at(tmp_path):
    _write(tmp_path, "z.md", kind=vs.TESTS_RECORD_KIND, plan_id=PLAN,
           recorded_at="'2026-10-15T10:00:00Z'", tests="{status: pass}")
    _write(tmp_path, "a.md", kind=vs.TESTS_RECORD_KIND, plan_id=PLAN,
           recorded_at="'2026-10-15T11:00:00Z'", tests="{status: fail}")
    got = vs.latest_tests_record(tmp_path, PLAN, EPOCH)
    assert got["tests"]["status"] == "fail"
    assert got["record_path"].endswith("a.md")


def test_floor_is_strictly_after(tmp_path):
    _write(tmp_path, "a.md", kind=vs.TESTS_RECORD_KIND, plan_id=PLAN,
           recorded_at="'2026-10-15T10:00:00Z'", tests="{status: pass}")
    at = datetime(2026, 10, 15, 10, 0, tzinfo=timezone.utc)
    assert vs.latest_tests_record(tmp_path, PLAN, at) is None
    assert vs.latest_tests_record(tmp_path, PLAN, datetime(2026, 10, 15, 9, 59)) is not None


def test_other_plan_id_ignored(tmp_path):
    _write(tmp_path, "a.md", kind=vs.TESTS_RECORD_KIND, plan_id="pln-other",
           recorded_at="'2026-10-15T10:00:00Z'", tests="{status: pass}")
    assert vs.latest_tests_record(tmp_path, PLAN, EPOCH) is None


def test_delivery_verdict_record_tests_count(tmp_path):
    _write(tmp_path, "d.md", kind=vs.RECORD_KIND, plan_id=PLAN,
           recorded_at="'2026-10-15T10:00:00Z'", tests="{status: pass, run: 5, failed: 0}")
    got = vs.latest_tests_record(tmp_path, PLAN, EPOCH)
    assert got["tests"]["run"] == 5


def test_criterion_rejudge_record_ignored(tmp_path):
    _write(tmp_path, "r.md", kind=vs.REJUDGE_KIND, plan_id=PLAN,
           recorded_at="'2026-10-15T10:00:00Z'", criterion="{status: met}")
    assert vs.latest_tests_record(tmp_path, PLAN, EPOCH) is None
    _write(tmp_path, "r2.md", kind=vs.REJUDGE_KIND, plan_id=PLAN,
           recorded_at="'2026-10-15T10:00:00Z'", tests="{status: pass}")
    assert vs.latest_tests_record(tmp_path, PLAN, EPOCH) is None

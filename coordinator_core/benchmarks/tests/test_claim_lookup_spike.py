"""Structure pins for the claim-lookup spike: candidate parity with claim_index, write containment, verdict line.

No time figure is asserted; the live verdict is S2's run.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from coordinator_core.benchmarks import claim_lookup_spike as spike
from coordinator_core.session import claim_index, touch_record

SID_A = "aaaaaaaa-1111-4222-8333-444444444444"
SID_B = "bbbbbbbb-1111-4222-8333-444444444444"
SID_C = "cccccccc-1111-4222-8333-444444444444"


def _event(sessions: Path, sid: str, verb: str, path: str, kind=touch_record.KIND_WRITE, ts=1000.0):
    touch_record.append_event(
        touch_record.sink_path(sessions / sid),
        session_id=sid, agent_id=None, verb=verb, path=path, timestamp=ts, name=None, kind=kind,
    )


@pytest.fixture
def sessions(tmp_path):
    base = tmp_path / "sessions"
    _event(base, SID_A, "T", "src/one.py")
    _event(base, SID_A, "T", "src/two.py", kind=touch_record.KIND_READ)
    _event(base, SID_B, "T", "src/one.py")
    _event(base, SID_B, "T", "src/gone.py")
    _event(base, SID_B, "R", "src/gone.py", ts=1001.0)
    _event(base, SID_C, "T", "src/other.py")
    return base


PATHS = ["src/one.py", "src/two.py"]


def _today(base):
    return spike.lookup_shape(claim_index.lookup(PATHS, sessions_dir=str(base)))


def _candidates(base, work: Path):
    spike.p_backfill(str(base), work / "P")
    journal = work / "J" / "journal.json"
    spike.j_build(str(base), journal)
    return journal


def test_today_split_matches_claim_index(sessions):
    shape, parts = spike.today_split(str(sessions), PATHS)
    assert shape == _today(sessions)
    assert {"enumerate", "stats", "fold", "aggregate", "total"} <= set(parts)
    assert shape["claimants"]["src/one.py"] == [SID_A, SID_B]


def test_candidates_agree_with_claim_index(sessions, tmp_path):
    work = tmp_path / "work"
    journal = _candidates(sessions, work)
    expected = _today(sessions)
    assert spike.p_lookup(work / "P", PATHS) == expected
    assert spike.j_lookup(str(sessions), journal, PATHS)[0] == expected


def test_candidates_agree_after_append_and_release(sessions, tmp_path):
    work = tmp_path / "work"
    journal = _candidates(sessions, work)
    sink_c = str(touch_record.sink_path(sessions / SID_C))
    _event(sessions, SID_C, "T", "src/two.py", ts=2000.0)
    spike.p_append(work / "P", "src/two.py", "T", SID_C, None, sink_c, 2000.0, touch_record.KIND_WRITE)
    _event(sessions, SID_B, "R", "src/one.py", ts=2001.0)
    sink_b = str(touch_record.sink_path(sessions / SID_B))
    spike.p_append(work / "P", "src/one.py", "R", SID_B, None, sink_b, 2001.0, None)
    expected = _today(sessions)
    assert expected["claimants"] == {"src/one.py": [SID_A], "src/two.py": [SID_A, SID_C]}
    assert spike.p_lookup(work / "P", PATHS) == expected
    shape, refolded = spike.j_lookup(str(sessions), journal, PATHS)
    assert shape == expected
    assert refolded == 2


def test_p_refuses_a_stream_positive_the_sink_no_longer_holds(sessions, tmp_path):
    work = tmp_path / "work"
    _candidates(sessions, work)
    _event(sessions, SID_A, "R", "src/one.py", ts=3000.0)
    assert spike.p_lookup(work / "P", PATHS) == _today(sessions)
    assert SID_A not in spike.p_lookup(work / "P", PATHS)["claimants"]["src/one.py"]


def test_j_warm_hit_refolds_nothing_and_forced_miss_refolds_k(sessions, tmp_path):
    journal = _candidates(sessions, tmp_path / "work")
    assert spike.j_lookup(str(sessions), journal, PATHS)[1] == 0
    assert spike.j_lookup(str(sessions), journal, PATHS, force_stale=1)[1] == 1
    assert spike.j_lookup(str(sessions), journal, PATHS, force_stale=10)[1] == 3


def test_candidates_write_only_under_work_dir_and_leave_sessions_untouched(sessions, tmp_path):
    def snapshot(root: Path):
        return {
            str(p.relative_to(root)): (p.stat().st_size, p.stat().st_mtime_ns)
            for p in root.rglob("*") if p.is_file()
        }

    before_sessions = snapshot(sessions)
    outside = {p for p in tmp_path.iterdir() if p.name not in ("sessions",)}
    work = tmp_path / "work"
    journal = _candidates(sessions, work)
    spike.p_lookup(work / "P", PATHS)
    spike.j_lookup(str(sessions), journal, PATHS, force_stale=10)
    spike.today_split(str(sessions), PATHS)
    assert snapshot(sessions) == before_sessions
    assert {p for p in tmp_path.iterdir() if p.name not in ("sessions",)} - outside == {work}


def test_blocking_holders_excludes_self_and_read_holds():
    shape = {"claimants": {"a": ["me", "w", "r"], "b": ["w"]}, "held_by": {}}
    kinds = {("a", "r"): touch_record.KIND_READ, ("a", "w"): touch_record.KIND_WRITE}
    assert spike.blocking_holders(shape, kinds, "me") == [("a", "w"), ("b", "w")]


def test_spike_line_from_injected_figures():
    clears = [10.0, 12.0, 20.0]
    assert spike.spike_line({"P": clears, "J": [30.0, 31.0]}) == "SPIKE: AT-BAR P"
    assert spike.spike_line({"P": [14.0, 15.0], "J": [8.0, 9.0]}) == "SPIKE: AT-BAR J"
    assert spike.spike_line({"P": clears, "J": [24.0, 25.0, 26.0]}) == "SPIKE: AT-BAR P"
    assert spike.spike_line({"P": [5.0, 5.0, 5.0, 111.0], "J": [30.0, 31.0]}) == "SPIKE: OVER-BAR -- P 5.0ms"
    assert spike.spike_line({"P": [26.0, 27.0], "J": [40.0]}) == "SPIKE: OVER-BAR -- P 26.5ms"


def test_main_refuses_a_work_dir_outside_scratch(tmp_path):
    with pytest.raises(SystemExit):
        spike.main(["--work-dir", "docs/x", "--repo-root", str(tmp_path)])
    with pytest.raises(SystemExit):
        spike.main(["--work-dir", str(tmp_path / "scratch"), "--repo-root", str(tmp_path)])


def test_main_prints_one_verdict_line_and_writes_only_scratch(tmp_path, capsys):
    root = tmp_path / "repo"
    sessions = root / ".git" / "coordinator-sessions"
    for rel in PATHS:
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text("x\n", encoding="utf-8")
    _event(sessions, SID_A, "T", "src/one.py")
    _event(sessions, SID_B, "T", "src/two.py")
    before = {p for p in root.rglob("*") if p.is_file() and "scratch" not in p.parts}
    stamp = {p: p.stat().st_mtime_ns for p in before}
    code = spike.main([
        "--samples", "3", "--work-dir", "scratch/w", "--repo-root", str(root),
        "--paths", *PATHS, "--skip-gates",
    ])
    out = capsys.readouterr().out.splitlines()
    verdicts = [line for line in out if line.startswith("SPIKE: ")]
    assert len(verdicts) == 1 and out[-1] == verdicts[0]
    assert code == (0 if "AT-BAR" in verdicts[0] else 1)
    assert "parity vs today_split: P ok  J ok" in out
    after = {p for p in root.rglob("*") if p.is_file() and "scratch" not in p.parts}
    assert after == before and {p: p.stat().st_mtime_ns for p in after} == stamp
    assert (root / "scratch" / "w" / "J" / "journal.json").is_file()
    assert os.path.isdir(root / "scratch" / "w" / "P")

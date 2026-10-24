"""Paired verdict reader: pairing rules, filters and the three verdict lines, from synthetic ledgers."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from coordinator_core.benchmarks import commit_v2_paired_verdict as pv

OP = "ceremony.commit_v2"


def _repo(tmp_path: Path) -> Path:
    (tmp_path / ".git" / "coordinator-sessions" / "logs").mkdir(parents=True)
    return tmp_path


def _write(repo: Path, rows: list[dict], gen: str = "op-latency.jsonl") -> None:
    p = repo / ".git" / "coordinator-sessions" / "logs" / gen
    p.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


def _pair(cid, t=1000.0, ms=200.0, spawns=1, paths=2, phases=None):
    phases = phases if phases is not None else {"stage": 20.0, "land": 100.0}
    return [
        {"kind": "phase_time", "op": OP, "corr_id": cid, "t_start": t, "paths": paths,
         "phases_ms": phases, "total_ms": sum(phases.values()), "spawns": spawns},
        {"kind": "process_time", "op": OP, "corr_id": cid, "t_start": t,
         "measurement_scope": "per_op_handler", "process_ms": ms, "spawns": spawns},
    ]


def _many(n, **kw):
    rows = []
    for i in range(n):
        rows += _pair(f"c{i}", t=1000.0 + i, **kw)
    return rows


def test_null_and_mismatched_corr_id_not_paired(tmp_path):
    repo = _repo(tmp_path)
    rows = _pair("a")
    rows[0]["corr_id"] = None
    rows[1]["corr_id"] = None
    rows += [dict(_pair("b")[0]), dict(_pair("zzz")[1])]
    _write(repo, rows)
    assert pv.paired_rows(repo, since_epoch=None) == []


def test_pairs_by_corr_id_not_t_start(tmp_path):
    repo = _repo(tmp_path)
    p1, h1 = _pair("a", t=1000.0, ms=111.0)
    p2, h2 = _pair("b", t=1000.0, ms=222.0)
    _write(repo, [p1, p2, h2, h1])
    got = {r["corr_id"]: r["process_ms"] for r in pv.paired_rows(repo, since_epoch=None)}
    assert got == {"a": 111.0, "b": 222.0}


def test_other_scope_not_paired(tmp_path):
    repo = _repo(tmp_path)
    rows = _pair("a")
    rows[1]["measurement_scope"] = "process_wide"
    _write(repo, rows)
    assert pv.paired_rows(repo, since_epoch=None) == []


def test_since_and_paths_max_filter(tmp_path):
    repo = _repo(tmp_path)
    _write(repo, _pair("old", t=100.0) + _pair("new", t=2000.0) + _pair("big", t=2001.0, paths=9))
    assert [r["corr_id"] for r in pv.paired_rows(repo, since_epoch=None)] == ["old", "new"]
    assert [r["corr_id"] for r in pv.paired_rows(repo, since_epoch=1000.0)] == ["new"]
    assert [r["corr_id"] for r in pv.paired_rows(repo, since_epoch=1000.0, paths_max=9)] == ["new", "big"]


def test_missing_generation_does_not_raise(tmp_path):
    repo = _repo(tmp_path)
    assert pv.paired_rows(repo, since_epoch=None) == []
    _write(repo, _pair("a"))
    _write(repo, _pair("b"), gen="op-latency.1.jsonl")
    assert len(pv.paired_rows(repo, since_epoch=None)) == 2


def test_pass_reduced_and_flat():
    reduced = pv.verdict(_rows(5, ms=300.0, spawns=0))
    assert reduced == "VERDICT: PASS -- median 300.0ms max 300.0ms over 5 paired rows; spawns: reduced"
    assert pv.verdict(_rows(5, ms=300.0, spawns=1)).endswith("spawns: flat")


def test_breach_when_max_over_bar_names_largest_median_phase():
    rows = _rows(5, ms=300.0, spawns=1, phases={"stage": 20.0, "land": 100.0})
    rows[0]["process_ms"] = 900.0
    assert pv.verdict(rows) == "VERDICT: BREACH -- land 100.0ms"


def test_breach_when_median_over_bar():
    assert pv.verdict(_rows(5, ms=600.0, spawns=1)).startswith("VERDICT: BREACH -- land")


def test_breach_when_spawns_over_baseline_or_absent():
    assert pv.verdict(_rows(5, ms=300.0, spawns=3)) == "VERDICT: BREACH -- spawns median 3.0 over baseline 1.0"
    assert pv.verdict(_rows(5, ms=300.0, spawns=None)) == "VERDICT: BREACH -- spawns absent"


def test_unknown_paths_row_is_excluded_from_the_reference_set(tmp_path):
    repo = _repo(tmp_path)
    rows = _pair("a")
    rows[0]["paths"] = None
    _write(repo, rows)
    assert pv.paired_rows(repo, since_epoch=None) == []


def test_insufficient_below_min_rows():
    assert pv.verdict(_rows(4, ms=300.0, spawns=1)) == "VERDICT: INSUFFICIENT -- 4 paired rows"


def test_main_exit_codes_and_single_final_line(tmp_path, capsys):
    repo = _repo(tmp_path)
    _write(repo, _many(5, ms=300.0, spawns=1, phases={"land": 100.0, "tiny": 1.0}) + _pair("orphan")[:1])
    assert pv.main(["--repo-root", str(repo)]) == 0
    out = capsys.readouterr().out.strip().splitlines()
    assert out[0] == "unpaired: 1"
    assert out[-1].startswith("VERDICT: PASS")
    assert sum(1 for line in out if line.startswith("VERDICT")) == 1
    assert "land=100.0" in "\n".join(out) and "tiny" not in "\n".join(out)

    _write(repo, _many(5, ms=700.0, spawns=1))
    assert pv.main(["--repo-root", str(repo)]) == 1
    _write(repo, _many(2, ms=100.0, spawns=1))
    assert pv.main(["--repo-root", str(repo)]) == 2


@pytest.mark.parametrize("text,expected", [("1500.5", 1500.5), ("1970-01-01T00:10:00Z", 600.0), ("1970-01-01T00:10:00", 600.0)])
def test_since_parse(text, expected):
    assert pv._parse_since(text) == expected


def _rows(n, ms, spawns, phases=None):
    phases = phases if phases is not None else {"stage": 20.0, "land": 100.0}
    return [{"corr_id": f"c{i}", "t_start": float(i), "paths": 2, "process_ms": ms,
             "spawns": spawns, "total_ms": sum(phases.values()), "phases_ms": dict(phases)}
            for i in range(n)]

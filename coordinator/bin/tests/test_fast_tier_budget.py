from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "fast-tier-budget.py"
_spec = importlib.util.spec_from_file_location("fast_tier_budget", _SCRIPT)
budget = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(budget)

JUNIT = """<?xml version="1.0"?>
<testsuites><testsuite name="pytest" tests="4" time="{wall}">
<testcase classname="pkg.tests.test_a" name="t1" time="10.5"/>
<testcase classname="pkg.tests.test_a.TestK" name="t2" time="4.5"/>
<testcase classname="pkg.tests.test_b" name="t1" time="2.0"/>
<testcase classname="pkg.tests.test_c" name="t1" time="0.5"/>
</testsuite></testsuites>
"""


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    (tmp_path / "pyproject.toml").write_text('[tool.pytest.ini_options]\ntestpaths = ["pkg"]\n', encoding="utf-8")
    tests = tmp_path / "pkg" / "tests"
    tests.mkdir(parents=True)
    (tests / "test_a.py").write_text("import pytest\n\n@pytest.mark.complete\ndef test_t1():\n    pass\n", encoding="utf-8")
    (tests / "test_b.py").write_text("import pytest\npytestmark = [pytest.mark.complete]\n", encoding="utf-8")
    (tests / "test_c.py").write_text(
        'S = "pytest.mark.complete"\n\ndef test_t1():\n    assert S\n', encoding="utf-8"
    )
    (tests / "test_d.py").write_text("import pytest\n\n@pytest.mark.cadence\ndef test_t():\n    pass\n", encoding="utf-8")
    return tmp_path


def _junit(tmp_path: Path, wall: float = 1800) -> Path:
    p = tmp_path / "run.xml"
    p.write_text(JUNIT.format(wall=wall), encoding="utf-8")
    return p


def test_scan_finds_decorator_and_pytestmark_not_strings_or_other_markers(repo):
    assert budget.scan_complete_files(repo) == ["pkg/tests/test_a.py", "pkg/tests/test_b.py"]


def test_parse_junit_groups_by_file_sorted_desc(repo, tmp_path):
    wall, total, per_file = budget.parse_junit(_junit(tmp_path), repo)
    assert wall == 1800 and total == 17.5
    assert per_file == [
        {"file": "pkg/tests/test_a.py", "seconds": 15.0, "tests": 2},
        {"file": "pkg/tests/test_b.py", "seconds": 2.0, "tests": 1},
        {"file": "pkg/tests/test_c.py", "seconds": 0.5, "tests": 1},
    ]


def test_projection_uses_recorded_cap_over_floor_cap(repo, tmp_path):
    box = {"physical_cores": 24, "ram_gb": 96.0, "cap": 12}
    ledger = budget.build_ledger(_junit(tmp_path, 1800), repo, box, None, "abc", "t")
    assert ledger["projected_floor_wall_s"] == 1800 * 12 / budget.FLOOR_CAP == 3600
    assert ledger["budget_s"] == 900 and ledger["base_sha"] == "abc"
    assert [m["id"] for m in ledger["moves"]] == ["pkg/tests/test_a.py", "pkg/tests/test_b.py"]
    assert all(m["new_marker"] == "complete" for m in ledger["moves"])


def test_rerecord_carries_move_seconds_from_prior_per_file(repo, tmp_path):
    box = {"physical_cores": 12, "ram_gb": 24.0, "cap": 6}
    base = budget.build_ledger(_junit(tmp_path), repo, box, None, None, "t")
    # after the move the fast run no longer contains the moved files
    after = tmp_path / "after.xml"
    after.write_text('<testsuites><testsuite time="100"><testcase classname="pkg.tests.test_c" name="t" time="1"/></testsuite></testsuites>')
    ledger = budget.build_ledger(after, repo, box, base, None, "t2")
    assert {m["id"]: m["seconds"] for m in ledger["moves"]} == {"pkg/tests/test_a.py": 15.0, "pkg/tests/test_b.py": 2.0}
    assert ledger["projected_floor_wall_s"] == 100


def test_cli_record_then_rank(repo, tmp_path, capsys):
    run = _junit(tmp_path)
    assert budget.main(["record", str(run), "--cap", "6", "--base-sha", "deadbeef", "--repo", str(repo)]) == 0
    ledger = json.loads((repo / budget.LEDGER_REL).read_text())
    assert ledger["projected_floor_wall_s"] == 1800 and ledger["base_sha"] == "deadbeef"
    assert ledger["box"]["cap"] == 6
    capsys.readouterr()
    assert budget.main(["rank", "--top", "2", "--repo", str(repo)]) == 0
    out = capsys.readouterr().out
    assert "pkg/tests/test_a.py" in out and "saving needed 1080s" in out


def _big_ledger(n: int = 100, sec: float = 60.0) -> dict:
    per_file = [{"file": f"p/test_{i}.py", "seconds": sec, "tests": 1} for i in range(n)]
    return {
        "budget_s": 900,
        "box": {"cap": 6},
        "measured_wall_s": 1500.0,
        "sum_test_s": sec * n,
        "projected_floor_wall_s": 1500.0,
        "per_file": per_file,
    }


def test_rank_default_prints_past_forty_files_until_gap_reached():
    # gap 780s at 10s saving per file -> 78 rows
    out = budget.rank(_big_ledger()).splitlines()
    rows = out[1:]
    assert len(rows) == 78 > 40
    assert "reached" in rows[-1]
    assert float(rows[-1].split("cum_saving")[1].split("s")[0]) >= 780
    assert "optimistic" in out[0]


def test_rank_top_is_an_explicit_cap_and_marks_not_reached():
    out = budget.rank(_big_ledger(), top=5).splitlines()
    assert len(out) == 1 + 5 + 1
    assert "not reached" in out[-1]


def test_rank_with_gap_already_closed_prints_header_only():
    ledger = _big_ledger()
    ledger["projected_floor_wall_s"] = 700.0
    assert len(budget.rank(ledger).splitlines()) == 1


def test_rank_without_ledger_reports_error(repo, capsys):
    assert budget.main(["rank", "--repo", str(repo)]) == 2
    assert "no ledger" in capsys.readouterr().err


def test_check_ledger_flags_budget_and_missing_move():
    ledger = {"budget_s": 900, "projected_floor_wall_s": 901, "moves": []}
    problems = budget.check_ledger(ledger, ["x/test_y.py"])
    assert any("900s" in p for p in problems) and any("x/test_y.py" in p for p in problems)


PROFILE = {"wall_s": 1800.0, "session_setup_s": 100.0, "worker_tail_s": 20.0, "collection_s": 90.0, "tail_idle_s": 300.0}


def test_split_projection_holds_constant_overhead_fixed(repo, tmp_path):
    box = {"physical_cores": 24, "ram_gb": 96.0, "cap": 12}
    ledger = budget.build_ledger(_junit(tmp_path, 1800), repo, box, None, "a", "t", PROFILE)
    assert ledger["profile"] == {"base": PROFILE}
    assert ledger["split"]["constant_overhead_s"] == 120.0
    assert ledger["split"]["projected_floor_wall_s"] == (1800 - 120) * 12 / budget.FLOOR_CAP + 120 == 3480
    assert ledger["projected_floor_wall_s"] == 3600


def test_no_profile_means_no_split_and_check_uses_plain_projection(repo, tmp_path):
    box = {"physical_cores": 12, "ram_gb": 24.0, "cap": 6}
    ledger = budget.build_ledger(_junit(tmp_path, 800), repo, box, None, "a", "t")
    assert "split" not in ledger and "profile" not in ledger
    assert budget.check_ledger(ledger, ["pkg/tests/test_a.py", "pkg/tests/test_b.py"]) == []


def test_check_ledger_switches_to_split_only_when_profile_present():
    ledger = {"budget_s": 900, "projected_floor_wall_s": 1000.0, "moves": []}
    assert budget.check_ledger(ledger, [])
    ledger["split"] = {"constant_overhead_s": 100.0, "projected_floor_wall_s": 880.0}
    assert budget.check_ledger(ledger, []) == []
    ledger["split"]["projected_floor_wall_s"] = 901.0
    assert budget.check_ledger(ledger, [])


def test_cli_record_profile_phases_base_then_after(repo, tmp_path):
    prof = tmp_path / "prof.json"
    prof.write_text(json.dumps(PROFILE))
    args = ["record", str(_junit(tmp_path)), "--cap", "6", "--repo", str(repo), "--profile", str(prof)]
    assert budget.main(args) == 0
    after = dict(PROFILE, session_setup_s=10.0)
    prof.write_text(json.dumps(after))
    assert budget.main(args) == 0
    ledger = json.loads((repo / budget.LEDGER_REL).read_text())
    assert ledger["profile"] == {"base": PROFILE, "after": after}
    assert ledger["split"]["constant_overhead_s"] == 30.0
    assert budget.main(args + ["--profile-phase", "base"]) == 0
    ledger = json.loads((repo / budget.LEDGER_REL).read_text())
    assert ledger["profile"]["base"] == after and ledger["split"]["constant_overhead_s"] == 30.0


def test_reproject_recomputes_split_when_profile_present():
    ledger = {
        "box": {"cap": 6},
        "measured_wall_s": 1000.0,
        "sum_test_s": 4000.0,
        "projected_floor_wall_s": 1000.0,
        "budget_s": 900,
        "per_file": [{"file": "a/test_x.py", "seconds": 600.0, "tests": 3}],
        "moves": [],
        "profile": {"base": PROFILE},
        "split": {"constant_overhead_s": 120.0, "projected_floor_wall_s": 1000.0},
    }
    out = budget.reproject(ledger, ["a/test_x.py"])
    assert out["split"]["projected_floor_wall_s"] == 900.0


def test_reproject_drops_moved_seconds_and_lists_moves():
    ledger = {
        "box": {"cap": 6},
        "measured_wall_s": 1000.0,
        "sum_test_s": 4000.0,
        "projected_floor_wall_s": 1000.0,
        "budget_s": 900,
        "per_file": [
            {"file": "a/test_x.py", "seconds": 600.0, "tests": 3},
            {"file": "b/test_y.py", "seconds": 60.0, "tests": 1},
        ],
        "moves": [],
    }
    out = budget.reproject(ledger, ["a/test_x.py"])
    assert out["moves"] == [{"id": "a/test_x.py", "seconds": 600.0, "new_marker": "complete"}]
    assert [p["file"] for p in out["per_file"]] == ["b/test_y.py"]
    assert out["measured_wall_s"] == 900.0 and out["sum_test_s"] == 3400.0
    assert out["projected_floor_wall_s"] == 900.0
    assert budget.check_ledger(out, ["a/test_x.py"]) == []
    assert budget.check_ledger(ledger, ["a/test_x.py"]) != []

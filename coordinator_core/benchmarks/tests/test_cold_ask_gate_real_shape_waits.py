"""The real-shape cold ask_gate benchmark names the dominant wait and refuses an unresolvable settings home."""

from __future__ import annotations

import pytest

from coordinator_core.benchmarks import cold_ask_gate_real_shape_waits as bench


def test_dominant_wait_is_the_largest_and_ties_keep_list_order():
    waits = dict.fromkeys(bench.WAITS, 1.0)
    waits["child_idle_wait_ms"] = 9.0
    assert bench.dominant_wait(waits) == "child_idle_wait_ms"
    assert bench.dominant_wait(dict.fromkeys(bench.WAITS, 3.0)) == bench.WAITS[0]


def test_attempts_are_read_from_a_hung_error_and_from_a_completed_envelope():
    rec = [{"phase": "backstop", "wall_ms": 30000.0}, {"phase": "await_ceiling", "wall_ms": 120000.0}]
    hung = {"error": {"data": {"diagnosis": {"attempts": rec}}}}
    done = {"result": {}, "data": {"diagnosis": {"attempts": rec[:1]}}}
    assert bench.attempts_wall_ms(hung) == (150000.0, rec)
    assert bench.attempts_wall_ms(done) == (30000.0, rec[:1])
    assert bench.attempts_wall_ms({"result": {}}) == (0.0, [])


def test_an_unresolvable_settings_home_is_a_typed_refusal_naming_the_key(tmp_path, monkeypatch):
    monkeypatch.setenv(bench.SETTINGS_HOME_KEY, str(tmp_path / "absent"))
    with pytest.raises(bench.SettingsHomeKeyMissing, match=bench.SETTINGS_HOME_KEY):
        bench.resolve_settings_home()


def test_a_tree_without_git_is_refused(tmp_path):
    with pytest.raises(bench.RepoRootMissing):
        bench.resolve_repo_root(tmp_path)


def _report(child_process_ms: float) -> dict:
    return {"waits": {"child_process_ms": child_process_ms}}


def test_the_bar_is_the_brightline_and_the_child_at_or_over_it_is_over():
    assert bench.BAR_MS == 500.0
    assert bench.over_bar(_report(bench.BAR_MS)) is True
    assert bench.over_bar(_report(bench.BAR_MS - 0.1)) is False


@pytest.mark.parametrize("over,expected_rc", [(600.0, 1), (100.0, 0)])
def test_enforce_bar_exits_one_over_the_bar_and_zero_under_it(tmp_path, monkeypatch, capsys, over, expected_rc):
    import coordinator_core.benchmarks as benchmarks

    (tmp_path / ".git").mkdir()
    monkeypatch.setattr(benchmarks, "declare_benchmark_origin", lambda: None)
    monkeypatch.setattr(bench, "measure", lambda repo, sizing_path, run_id, profile: _report(over))
    monkeypatch.setattr(bench.sys, "argv", ["bench", "--repo", str(tmp_path), "--enforce-bar"])
    assert bench.main() == expected_rc
    monkeypatch.setattr(bench.sys, "argv", ["bench", "--repo", str(tmp_path)])
    assert bench.main() == 0
    capsys.readouterr()


def test_main_refuses_with_the_typed_error_when_the_settings_home_does_not_resolve(tmp_path, monkeypatch, capsys):
    import coordinator_core.benchmarks as benchmarks

    (tmp_path / ".git").mkdir()
    monkeypatch.setattr(benchmarks, "declare_benchmark_origin", lambda: None)
    monkeypatch.setenv(bench.SETTINGS_HOME_KEY, str(tmp_path / "absent"))
    monkeypatch.setattr(bench.sys, "argv", ["bench", "--repo", str(tmp_path), "--profile", "--enforce-bar"])
    assert bench.main() == 2
    assert bench.SETTINGS_HOME_KEY in capsys.readouterr().out


@pytest.mark.parametrize("carveout", [False, True])
def test_the_profile_launch_is_valid_python_naming_the_cold_module_and_the_dump_env(carveout):
    src = bench.profile_launch(carveout)
    compile(src, "<profile-launch>", "exec")
    assert bench.COLD_MODULE in src and bench._PROFILE_OUT_ENV in src
    assert ("allow_unstamped_dispatch" in src) is carveout


def test_top_cumulative_ranks_a_real_cprofile_dump_by_either_column(tmp_path):
    import cProfile
    import time

    def inner():
        sum(range(200_000))

    def outer():
        inner()
        time.sleep(0.01)

    prof = cProfile.Profile()
    prof.runcall(outer)
    out = tmp_path / "p.prof"
    prof.dump_stats(str(out))
    cum = bench.top_cumulative(out, n=3)
    assert len(cum) == 3
    assert [r["cumtime_ms"] for r in cum] == sorted((r["cumtime_ms"] for r in cum), reverse=True)
    assert any("outer" in r["func"] for r in cum)
    selfs = bench.top_cumulative(out, n=3, by="self")
    assert [r["tottime_ms"] for r in selfs] == sorted((r["tottime_ms"] for r in selfs), reverse=True)
    assert set(cum[0]) == {"func", "calls", "tottime_ms", "cumtime_ms"}


def test_a_throwaway_run_creates_a_unique_run_bound_sizing_and_removes_every_trace_of_it(tmp_path):
    (tmp_path / "state" / "sizings").mkdir(parents=True)
    live = tmp_path / "state" / "sizings" / "live.yaml"
    live.write_text("name: live\n", encoding="utf-8")
    with bench.throwaway_run(tmp_path, plan_rows=5) as (sizing_rel, run_id):
        sizing = tmp_path / sizing_rel
        assert sizing.is_file() and sizing_rel != "state/sizings/live.yaml"
        assert bench.THROWAWAY_STEM in sizing_rel
        assert (tmp_path / "state" / "warp" / run_id / "run.json").is_file()
        sizing.with_suffix(".yaml.lock").write_text("", encoding="utf-8")
        plan = (tmp_path / "docs" / "plans" / f"{sizing.stem}.md").read_text(encoding="utf-8")
        assert plan.count("disposition: open") == 5
        assert f"docs/plans/{sizing.stem}.md" in sizing.read_text(encoding="utf-8")
    assert not (tmp_path / "docs" / "plans" / f"{sizing.stem}.md").exists()
    assert not (tmp_path / "state" / "warp" / run_id).exists()
    assert [p.name for p in (tmp_path / "state" / "sizings").iterdir()] == ["live.yaml"]


def test_a_throwaway_run_is_removed_when_the_body_raises(tmp_path):
    with pytest.raises(RuntimeError):
        with bench.throwaway_run(tmp_path) as (sizing_rel, run_id):
            raise RuntimeError("boom")
    assert list((tmp_path / "state" / "sizings").iterdir()) == []
    assert list((tmp_path / "state" / "warp").iterdir()) == []


@pytest.mark.cadence
def test_measure_bills_every_wait_and_names_one_as_dominant(tmp_path, monkeypatch):
    home = tmp_path / "settings-home"
    home.mkdir()
    monkeypatch.setenv(bench.SETTINGS_HOME_KEY, str(home))
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    report = bench.measure(bench.resolve_repo_root(repo))
    assert set(report["waits"]) == set(bench.WAITS)
    assert all(v >= 0.0 for v in report["waits"].values())
    assert report["dominant_wait"] in bench.WAITS
    assert report["waits"][report["dominant_wait"]] == max(report["waits"].values())
    assert report["child_procs"] >= 1 and report["child_rc"] == 0, report
    assert report["settings_home"] in (str(home.resolve()), str(home))
    assert isinstance(report["engine_stamped"], bool)


REAL_SHAPE_PLAN_ROWS = 3000


def real_shape_verdict(report: dict) -> tuple[bool, str]:
    """`(passes, why)`: a backstop error or a missing `child_process_ms` is a failure, never a skip."""
    err = report.get("error_code")
    child = report.get("waits", {}).get("child_process_ms")
    if err is not None:
        return False, f"child errored {err}: {report.get('error_message')}"
    if not isinstance(child, (int, float)) or child <= 0:
        return False, f"no child_process_ms: {report}"
    if bench.over_bar(report):
        return False, f"child_process_ms {child} >= bar {bench.BAR_MS}"
    return True, f"child_process_ms {child}"


@pytest.mark.parametrize(
    "report,passes",
    [
        ({"error_code": -32004, "error_message": "cold-run dispatch hung", "waits": {"child_process_ms": 30000.0}}, False),
        ({"error_code": None, "waits": {}}, False),
        ({"error_code": None, "waits": {"child_process_ms": 17171.9}}, False),
        ({"error_code": None, "waits": {"child_process_ms": 499.9}}, True),
    ],
)
def test_the_real_shape_verdict_fails_a_backstop_a_missing_figure_and_an_over_bar_child(report, passes):
    assert real_shape_verdict(report)[0] is passes


@pytest.mark.cadence
def test_measure_with_profile_reports_a_top_twenty_by_cumulative_time(tmp_path, monkeypatch):
    home = tmp_path / "settings-home"
    home.mkdir()
    monkeypatch.setenv(bench.SETTINGS_HOME_KEY, str(home))
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    report = bench.measure(bench.resolve_repo_root(repo), profile=True)
    prof = report["profile"]
    assert prof["rc"] == 0 and len(prof["top"]) == bench.PROFILE_TOP, prof
    assert [r["cumtime_ms"] for r in prof["top"]] == sorted((r["cumtime_ms"] for r in prof["top"]), reverse=True)
    assert prof["top_self"]
    assert report["bar_ms"] == bench.BAR_MS

"""The real-shape cold ask_gate benchmark names the dominant wait and refuses an unresolvable settings home."""

from __future__ import annotations

import pytest

from coordinator_core.benchmarks import cold_ask_gate_real_shape_waits as bench

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


REAL_SHAPE_PLAN_ROWS = 3000


def _tmp_git_repo(tmp_path):
    """A git repo under `tmp_path` with a `coordinator.local.md`, the settings home pinned beside it."""
    import subprocess

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "README.md").write_text("seed\n", encoding="utf-8", newline="\n")
    (repo / "coordinator.local.md").write_text("---\nproject: fixture\n---\n\nnotes\n" * 20, encoding="utf-8", newline="\n")
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    for args in (("init", "-q", "-b", "main"), ("add", "-A"), ("commit", "-q", "-m", "seed")):
        subprocess.run(
            ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t.example", *args],
            check=True, capture_output=True, creationflags=flags,
        )
    return repo


def real_shape_report(tmp_path, plan_rows: int = REAL_SHAPE_PLAN_ROWS) -> dict:
    """The run-bound cold `dispatch.ask_gate` of a repo whose sizing names a plan with `plan_rows` open rows
    (the shape that scaled the cold child to 27s), measured through the production `measure`."""
    from coordinator_core.benchmarks import fixture_settings_home

    repo = _tmp_git_repo(tmp_path)
    (tmp_path / "settings-home").mkdir()
    with fixture_settings_home(tmp_path):
        with bench.throwaway_run(repo, plan_rows=plan_rows) as (sizing_rel, run_id):
            return bench.measure(repo, sizing_rel, run_id)


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


@pytest.mark.cadence
@pytest.mark.designed_red
def test_the_cold_child_answers_under_the_bar_on_a_plan_with_thousands_of_open_rows(tmp_path):
    report = real_shape_report(tmp_path)
    ok, why = real_shape_verdict(report)
    assert ok, why
    assert report["child_rc"] == 0, report

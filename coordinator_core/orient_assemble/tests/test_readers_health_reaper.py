"""
Tests for coordinator_core.orient_assemble.readers_health_reaper's
`_read_reaper_dry_run` — the in-process replacement for the deleted
`_REAP_SUBPROCESS_EXCEPTION` subprocess call.

Spec backlink: docs/plans/2026-08-26-two-callers-want-two-numbers-not-a-1301-line-cli.md
chunk C2.

Negative-spec:
    - Does NOT spawn a subprocess anywhere in this module — `_read_reaper_dry_run`
      calls `reap_in_flight_claims.survey()` directly, in-process. Every test here
      patches `_reap_survey` rather than touching the filesystem corpus `survey()`
      itself reads.
    - Does NOT re-implement the deleted `_REAP_WOULD_RELEASE_RE` /
      `_REAP_WOULD_RECLAIM_RE` prose-parsing contract — `survey()` returns integers
      directly, so there is no stdout to regex-match.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

from unittest import mock

from pathlib import Path

from coordinator_core.orient_assemble import readers_health_reaper as rhr
from coordinator_core.ops.reap_in_flight_claims import SurveyResult

_REPO_ROOT = Path(__file__).resolve().parents[3]


def test_two_integer_contract_produces_expected_directive():
    fake_result = SurveyResult(would_release=2, would_reclaim=3, dispositions=[])
    with mock.patch.object(rhr, "_reap_survey", return_value=fake_result) as survey_mock:
        result = rhr._read_reaper_dry_run()

    survey_mock.assert_called_once_with(rhr._CLAUDE_KLABAUTER_ROOT)
    assert len(result.directives) == 1
    directive = result.directives[0]
    assert directive["id"] == "d-reaper-orphaned-handoffs"
    assert directive["cli"] == "reap-orphaned-in-flight-handoffs"
    assert directive["args"] == []
    assert "2 orphaned in_flight handoff(s) would be released" in directive["detail"]
    assert "3 orphaned in_flight handoff(s) would be reclaimed as shipped" in directive["detail"]
    assert not result.judgment_points


def test_zero_directive_case_returns_empty_reader_result():
    fake_result = SurveyResult(would_release=0, would_reclaim=0, dispositions=[])
    with mock.patch.object(rhr, "_reap_survey", return_value=fake_result):
        result = rhr._read_reaper_dry_run()

    assert result.directives == []
    assert result.judgment_points == []


def test_no_subprocess_created_on_this_path():
    fake_result = SurveyResult(would_release=1, would_reclaim=0, dispositions=[])
    with mock.patch.object(rhr, "_reap_survey", return_value=fake_result):
        with mock.patch("subprocess.run") as subprocess_run_mock:
            rhr._read_reaper_dry_run()

    subprocess_run_mock.assert_not_called()


def test_reader_goes_quiet_rather_than_killing_orientation(monkeypatch):
    def _boom(_repo_root):
        raise OSError("handoff vanished mid-scan")

    monkeypatch.setattr(rhr, "_reap_survey", _boom)
    result = rhr._read_reaper_dry_run()
    assert result.directives == []
    assert result.judgment_points == []


def test_an_unexpected_raise_is_not_swallowed_by_the_reader(monkeypatch):
    def _boom(_repo_root):
        raise RuntimeError("survey blew up")

    monkeypatch.setattr(rhr, "_reap_survey", _boom)
    with pytest.raises(RuntimeError, match="survey blew up"):
        rhr.collect("day")


class _FakeGoalCoverageScanModule:

    def __init__(self, *, active_goals=None, raise_on_fetch=False, coverage=None):
        self._active_goals = active_goals or []
        self._raise_on_fetch = raise_on_fetch
        self._coverage = coverage if coverage is not None else []
        self.bootstrap_calls = 0
        self.compute_coverage_calls = []

    def _bootstrap_query_records(self):
        self.bootstrap_calls += 1

    def _fetch_active_goals(self):
        if self._raise_on_fetch:
            raise RuntimeError("records query failed")
        return self._active_goals

    def _fetch_coverage_for_goal(self, goal_id):  # pragma: no cover - stubbed lookup
        raise AssertionError("real lookup must not run when compute_coverage is patched")

    def compute_coverage(self, goals, lookup_coverage):
        self.compute_coverage_calls.append((goals, lookup_coverage))
        return self._coverage


def test_read_goal_coverage_emits_nothing_when_records_query_raises(monkeypatch):
    fake = _FakeGoalCoverageScanModule(raise_on_fetch=True)
    monkeypatch.setattr(rhr, "_goal_coverage_scan", fake)

    result = rhr._read_goal_coverage()

    assert result.directives == []
    assert result.judgment_points == []
    assert fake.bootstrap_calls == 1


def test_read_goal_coverage_emits_directive_for_zero_coverage_goals(monkeypatch):
    fake = _FakeGoalCoverageScanModule(
        active_goals=[{"id": "g-1"}, {"id": "g-2"}],
        coverage=[
            {"goalId": "g-1", "zeroCoverage": True},
            {"goalId": "g-2", "zeroCoverage": False},
        ],
    )
    monkeypatch.setattr(rhr, "_goal_coverage_scan", fake)

    result = rhr._read_goal_coverage()

    assert len(result.directives) == 1
    directive = result.directives[0]
    assert directive["id"] == "d-goal-coverage"
    assert directive["cli"] == "goal-coverage-scan"
    assert directive["args"] == ["--format", "text"]
    assert "g-1" in directive["detail"]
    assert "g-2" not in directive["detail"]
    assert result.judgment_points == []


def test_read_goal_coverage_emits_nothing_when_no_goal_is_zero_coverage(monkeypatch):
    fake = _FakeGoalCoverageScanModule(
        active_goals=[{"id": "g-1"}],
        coverage=[{"goalId": "g-1", "zeroCoverage": False}],
    )
    monkeypatch.setattr(rhr, "_goal_coverage_scan", fake)

    result = rhr._read_goal_coverage()

    assert result.directives == []
    assert result.judgment_points == []


def test_read_trail_scope_emits_nothing_when_no_session_id(monkeypatch):
    monkeypatch.setattr(rhr._workweek_trail_scope, "_resolve_session_id", lambda: "")

    def _boom(*_args, **_kwargs):
        raise AssertionError("main() must never be called from a reader")

    monkeypatch.setattr(rhr._workweek_trail_scope, "main", _boom)

    result = rhr._read_trail_scope()

    assert result.directives == []
    assert result.judgment_points == []


def test_read_trail_scope_emits_directive_when_header_file_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(rhr._workweek_trail_scope, "_resolve_session_id", lambda: "sid-123")
    missing_header = tmp_path / "does-not-exist" / "HEADER.md"
    monkeypatch.setenv("HEADER_FILE", str(missing_header))

    def _boom(*_args, **_kwargs):
        raise AssertionError("main() must never be called from a reader")

    monkeypatch.setattr(rhr._workweek_trail_scope, "main", _boom)

    result = rhr._read_trail_scope()

    assert [d["id"] for d in result.directives] == ["d-workweek-trail-scope"]
    assert result.directives[0]["cli"] == "workweek-trail-scope"
    assert "not found" in result.directives[0]["detail"]


def test_read_trail_scope_emits_directive_when_week_start_unparseable(monkeypatch, tmp_path):
    header_file = tmp_path / "HEADER.md"
    header_file.write_text("no week-starting line here", encoding="utf-8")
    monkeypatch.setenv("HEADER_FILE", str(header_file))
    monkeypatch.setattr(rhr._workweek_trail_scope, "_resolve_session_id", lambda: "sid-123")
    monkeypatch.setattr(rhr._workweek_trail_scope, "_parse_week_start", lambda _header: None)

    def _boom(*_args, **_kwargs):
        raise AssertionError("main() must never be called from a reader")

    monkeypatch.setattr(rhr._workweek_trail_scope, "main", _boom)

    result = rhr._read_trail_scope()

    assert [d["id"] for d in result.directives] == ["d-workweek-trail-scope"]
    assert "cannot parse" in result.directives[0]["detail"]


def test_read_trail_scope_emits_nothing_when_resolution_succeeds(monkeypatch, tmp_path):
    header_file = tmp_path / "HEADER.md"
    header_file.write_text("Week starting: 2026-09-14", encoding="utf-8")
    monkeypatch.setenv("HEADER_FILE", str(header_file))
    monkeypatch.setattr(rhr._workweek_trail_scope, "_resolve_session_id", lambda: "sid-123")
    monkeypatch.setattr(rhr._workweek_trail_scope, "_parse_week_start", lambda _header: "2026-09-14")

    trail_files_calls = {"count": 0}
    monkeypatch.setattr(
        rhr._workweek_trail_scope,
        "_trail_files",
        lambda: (trail_files_calls.__setitem__("count", trail_files_calls["count"] + 1), [])[1],
    )

    def _boom(*_args, **_kwargs):
        raise AssertionError("main() must never be called from a reader")

    monkeypatch.setattr(rhr._workweek_trail_scope, "main", _boom)

    result = rhr._read_trail_scope()

    assert result.directives == []
    assert result.judgment_points == []
    assert trail_files_calls["count"] == 1


def test_read_git_maintenance_due_day_cadence_stale(monkeypatch):
    from coordinator_core.ops.ceremony import housekeeping_liveness as hl

    monkeypatch.setattr(
        rhr, "_liveness_status",
        lambda repo_root, classes: {rhr._GIT_MAINTENANCE: hl.STATUS_STALE},
    )

    result = rhr._read_git_maintenance_due("/some/repo", "day")

    assert [d["id"] for d in result.directives] == ["d-git-maintenance-daily"]
    directive = result.directives[0]
    assert directive["cli"] == "coordinator-git-maintenance"
    assert directive["args"] == ["daily"]
    assert "stale" in directive["detail"]


def test_read_git_maintenance_due_week_cadence_never_stamped(monkeypatch):
    from coordinator_core.ops.ceremony import housekeeping_liveness as hl

    monkeypatch.setattr(
        rhr, "_liveness_status",
        lambda repo_root, classes: {rhr._GIT_MAINTENANCE: hl.STATUS_NEVER_STAMPED},
    )

    result = rhr._read_git_maintenance_due("/some/repo", "week")

    assert [d["id"] for d in result.directives] == ["d-git-maintenance-weekly"]
    directive = result.directives[0]
    assert directive["cli"] == "coordinator-git-maintenance"
    assert directive["args"] == ["weekly"]
    assert "never stamped" in directive["detail"]


def test_read_git_maintenance_due_emits_nothing_when_fresh(monkeypatch):
    from coordinator_core.ops.ceremony import housekeeping_liveness as hl

    monkeypatch.setattr(
        rhr, "_liveness_status",
        lambda repo_root, classes: {rhr._GIT_MAINTENANCE: hl.STATUS_FRESH},
    )

    result = rhr._read_git_maintenance_due("/some/repo", "day")

    assert result.directives == []
    assert result.judgment_points == []


def _write_plugin_drift_fixture(tmp_path, *, sentinel_sha="a" * 40, drifted=False):
    registry_dir = tmp_path / "registry"
    registry_dir.mkdir()
    (registry_dir / "registry.toml").write_text("", encoding="utf-8")

    live_path = tmp_path / "live" / "some-plugin"
    live_path.mkdir(parents=True)
    (live_path / "version.txt").write_text(sentinel_sha, encoding="utf-8")

    source_path = tmp_path / "source" / "some-plugin"
    source_path.mkdir(parents=True)
    (source_path / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")

    return registry_dir, live_path, source_path


def test_read_plugin_drift_emits_nothing_when_registry_absent(monkeypatch, tmp_path):
    empty_dir = tmp_path / "no-registry"
    empty_dir.mkdir()
    monkeypatch.setattr(rhr._drift, "_resolve_registry_dir", lambda: empty_dir)

    result = rhr._read_plugin_drift()

    assert result.directives == []
    assert result.judgment_points == []


def test_read_plugin_drift_clean_mirror_emits_nothing(monkeypatch, tmp_path):
    registry_dir, live_path, source_path = _write_plugin_drift_fixture(tmp_path)
    monkeypatch.setattr(rhr._drift, "_resolve_registry_dir", lambda: registry_dir)
    monkeypatch.setattr(
        rhr._drift, "read_merged_mirrors",
        lambda files: {
            "some-plugin": {
                "propagation_mode": "copy_install",
                "live_path": str(live_path),
                "source_path": str(source_path),
            }
        },
    )
    monkeypatch.setattr(rhr._drift, "_resolve_claude_home", lambda: tmp_path / "claude-home")
    monkeypatch.setattr(rhr._drift, "_refresh_log_baseline_hash", lambda log, name: "same-hash")
    monkeypatch.setattr(rhr._drift, "_pyproject_hash", lambda path: "same-hash")

    def _boom(*_args, **_kwargs):
        raise AssertionError("no git-touching leg may run from this reader")

    monkeypatch.setattr(rhr._drift, "_run_git", _boom)

    result = rhr._read_plugin_drift()

    assert result.directives == []
    assert result.judgment_points == []


def test_read_plugin_drift_malformed_sentinel_emits_directive(monkeypatch, tmp_path):
    registry_dir, live_path, source_path = _write_plugin_drift_fixture(
        tmp_path, sentinel_sha="not-a-valid-sha"
    )
    monkeypatch.setattr(rhr._drift, "_resolve_registry_dir", lambda: registry_dir)
    monkeypatch.setattr(
        rhr._drift, "read_merged_mirrors",
        lambda files: {
            "some-plugin": {
                "propagation_mode": "copy_install",
                "live_path": str(live_path),
                "source_path": str(source_path),
            }
        },
    )
    monkeypatch.setattr(rhr._drift, "_resolve_claude_home", lambda: tmp_path / "claude-home")

    def _boom(*_args, **_kwargs):
        raise AssertionError("no git-touching leg may run from this reader")

    monkeypatch.setattr(rhr._drift, "_run_git", _boom)

    result = rhr._read_plugin_drift()

    assert [d["id"] for d in result.directives] == ["d-plugin-drift"]
    directive = result.directives[0]
    assert directive["cli"] == "check-plugin-drift"
    assert "some-plugin" in directive["detail"]


def test_read_plugin_drift_absent_sentinel_emits_nothing(monkeypatch, tmp_path):
    registry_dir, live_path, source_path = _write_plugin_drift_fixture(tmp_path)
    (live_path / "version.txt").unlink()
    monkeypatch.setattr(rhr._drift, "_resolve_registry_dir", lambda: registry_dir)
    monkeypatch.setattr(
        rhr._drift, "read_merged_mirrors",
        lambda files: {
            "some-plugin": {
                "propagation_mode": "copy_install",
                "live_path": str(live_path),
                "source_path": str(source_path),
            }
        },
    )
    monkeypatch.setattr(rhr._drift, "_resolve_claude_home", lambda: tmp_path / "claude-home")

    def _boom(*_args, **_kwargs):
        raise AssertionError("no git-touching leg may run from this reader")

    monkeypatch.setattr(rhr._drift, "_run_git", _boom)

    result = rhr._read_plugin_drift()

    assert result.directives == []
    assert result.judgment_points == []


def test_read_plugin_drift_changed_pyproject_hash_emits_directive(monkeypatch, tmp_path):
    registry_dir, live_path, source_path = _write_plugin_drift_fixture(tmp_path)
    monkeypatch.setattr(rhr._drift, "_resolve_registry_dir", lambda: registry_dir)
    monkeypatch.setattr(
        rhr._drift, "read_merged_mirrors",
        lambda files: {
            "some-plugin": {
                "propagation_mode": "copy_install",
                "live_path": str(live_path),
                "source_path": str(source_path),
            }
        },
    )
    monkeypatch.setattr(rhr._drift, "_resolve_claude_home", lambda: tmp_path / "claude-home")
    monkeypatch.setattr(rhr._drift, "_refresh_log_baseline_hash", lambda log, name: "old-hash")
    monkeypatch.setattr(rhr._drift, "_pyproject_hash", lambda path: "new-hash")

    def _boom(*_args, **_kwargs):
        raise AssertionError("no git-touching leg may run from this reader")

    monkeypatch.setattr(rhr._drift, "_run_git", _boom)

    result = rhr._read_plugin_drift()

    assert [d["id"] for d in result.directives] == ["d-plugin-drift"]
    assert "some-plugin" in result.directives[0]["detail"]


def test_probe_subcommands_are_callable_in_a_clean_interpreter():
    """The loaded probes CLI reaches `coordinator/bin/lib` on its own.

    Its subcommands `import lib`, which resolves only when `coordinator/bin`
    is on `sys.path`. Both sanctioned entry paths arrange that for
    themselves -- a directly-run script gets its own dir as `sys.path[0]`,
    and the warm door calls `bin_lib_binding.ensure_bin_lib_bound()` -- but this reader
    loads the CLI by file location, which is neither. The reader must
    therefore bootstrap it, and this runs in a SUBPROCESS because inside a
    pytest session some unrelated import has usually put the directory on
    `sys.path` already, which is exactly the ambient luck that hid the
    original defect.
    """
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            "import coordinator_core.orient_assemble.readers_health_reaper as r;"
            "print(r._health_probes.cmd_working_repo_registration([]))",
        ],
        capture_output=True,
        text=True,
        cwd=str(_REPO_ROOT),
    )
    assert "ModuleNotFoundError" not in proc.stderr, proc.stderr
    assert proc.returncode == 0, proc.stderr

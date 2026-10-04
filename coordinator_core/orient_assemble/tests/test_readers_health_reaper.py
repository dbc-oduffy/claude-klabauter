"""
Tests for coordinator_core.orient_assemble.readers_health_reaper's readers:
goal coverage, trail scope, git maintenance, plugin drift, hook currency, and
the session-cadence workday judgment point.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

from pathlib import Path

from coordinator_core.orient_assemble import readers_health_reaper as rhr
from coordinator_core.session import record_homes
from coordinator_core.win_portability import no_console_creationflags

_STUB_A = Path(record_homes.record_path("", "handoffs", "stub-a.md")).as_posix()

_REPO_ROOT = Path(__file__).resolve().parents[3]


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
        cwd=str(_REPO_ROOT), **no_console_creationflags(),
    )
    assert "ModuleNotFoundError" not in proc.stderr, proc.stderr
    assert proc.returncode == 0, proc.stderr


# --- d-hook-currency: a stale hook disposition surfaces as the directive ---

_RETIRED_AUTO_PUSH_MARKER = "# retired-auto-push-body-fixture\n"


def _hook_currency_fleet(monkeypatch, tmp_path, *, stale):
    """One registered clone; its post-commit hook carries the retired body when
    `stale`. The disposition table is a synthetic entry so the pin does not
    depend on the live entry module."""
    from coordinator_core import machine_resolver
    from coordinator_core.git import hook_dispositions as hd

    repo = tmp_path / "fleet-clone"
    hooks = repo / ".git" / "hooks"
    hooks.mkdir(parents=True)
    body = _RETIRED_AUTO_PUSH_MARKER if stale else "#!/bin/sh\nexit 0\n"
    (hooks / "post-commit").write_text(body, encoding="utf-8", newline="\n")

    def _identify(text):
        return hd.Match(0, len(text)) if text == _RETIRED_AUTO_PUSH_MARKER else None

    entry = hd.HookDisposition(
        id="retired-auto-push-post-commit",
        hook_name="post-commit",
        action="remove",
        identify=_identify,
    )
    monkeypatch.setattr(hd, "DISPOSITIONS", (entry,), raising=False)
    monkeypatch.setattr(
        machine_resolver,
        "merged_flat_registry",
        lambda *a, **k: {"repos.fixture_clone": str(repo)},
    )
    return hooks / "post-commit"


def test_read_hook_currency_surfaces_stale_disposition_as_directive(monkeypatch, tmp_path):
    hook = _hook_currency_fleet(monkeypatch, tmp_path, stale=True)
    result = rhr._read_hook_currency()
    assert [d["id"] for d in result.directives] == ["d-hook-currency"]
    assert "retired-auto-push-post-commit" in result.directives[0]["detail"]
    # The orient reader only checks; it never repairs the clone.
    assert hook.read_text(encoding="utf-8") == _RETIRED_AUTO_PUSH_MARKER


def test_read_hook_currency_clean_fleet_yields_no_directive(monkeypatch, tmp_path):
    _hook_currency_fleet(monkeypatch, tmp_path, stale=False)
    result = rhr._read_hook_currency()
    assert result.directives == []


def test_read_hook_currency_consumer_gets_a_judgment_point_not_a_directive(monkeypatch, tmp_path):
    from coordinator_core import machine_profile as mp

    _hook_currency_fleet(monkeypatch, tmp_path, stale=True)
    monkeypatch.setattr(mp, "machine_profile", lambda: "consumer")
    result = rhr._read_hook_currency()
    assert result.directives == []
    assert [j["id"] for j in result.judgment_points] == ["j-hook-currency-repair"]
    assert "workday-start-health-probes hook-currency" in result.judgment_points[0]["question"]


def test_read_hook_currency_author_keeps_the_directive(monkeypatch, tmp_path):
    from coordinator_core import machine_profile as mp

    _hook_currency_fleet(monkeypatch, tmp_path, stale=True)
    monkeypatch.setattr(mp, "machine_profile", lambda: "author")
    result = rhr._read_hook_currency()
    assert [d["id"] for d in result.directives] == ["d-hook-currency"]
    assert result.judgment_points == []


@pytest.mark.parametrize(
    "registered_key,onboarded,expect_jp",
    [(None, True, False), ("repos.x", False, False), ("repos.x", True, True)],
)
def test_session_workday_jp_needs_registered_and_onboarded_cwd(
    monkeypatch, tmp_path, registered_key, onboarded, expect_jp
):
    from coordinator_core import repo_standing as rs
    from coordinator_core.ops import check_weekly_staleness as cws

    (tmp_path / ".workday-start-marker").write_text("1999-01-01", encoding="utf-8")
    monkeypatch.setattr(cws, "_resolve_state_root", lambda: str(tmp_path))
    monkeypatch.setattr(
        rs,
        "repo_standing",
        lambda p: rs.RepoStanding(str(p), registered_key, onboarded, False),
    )
    ids = [j["id"] for j in rhr._read_marker_freshness("session").judgment_points]
    assert (ids == ["j-session-day-review-due"]) is expect_jp
    assert (not ids) is (not expect_jp)
def _stub_survey(*stale):
    from coordinator_core.ops.origin_stub_staleness import OriginStubSurvey

    return OriginStubSurvey(stale=tuple(stale), live_with_pair=len(stale), unreadable=())


def _one_stale():
    from coordinator_core.ops.origin_stub_staleness import StaleOriginStub

    return StaleOriginStub(
        path=_STUB_A,
        pair=("r", "s"),
        deployment_state="ready_to_fire",
        evidence_path="docs/plans/shipped-plan.md",
        evidence_kind="plan",
    )


def _isolate_collect(monkeypatch):
    from coordinator_core.orient_assemble.reader_result import ReaderResult

    for name in (
        "_read_claude_klabauter_bin_sentinel",
        "_read_working_repo_registration",
        "_read_hook_currency",
        "_read_git_perf_currency",
        "_read_ceremony_hook",
        "_read_marker_freshness",
        "_read_plugin_drift",
        "_read_git_maintenance_due",
        "_read_goal_coverage",
        "_read_trail_scope",
    ):
        monkeypatch.setattr(rhr, name, lambda *a, **k: ReaderResult())


def test_collect_day_surfaces_stale_origin_stub_judgment_point(monkeypatch, tmp_path):
    _isolate_collect(monkeypatch)
    monkeypatch.setattr(rhr, "survey", lambda root: _stub_survey(_one_stale()))
    result = rhr.collect("day", repo_root=str(tmp_path))
    assert result.directives == []
    assert [jp["id"] for jp in result.judgment_points] == ["j-stale-origin-stubs"]
    evidence = result.judgment_points[0]["evidence"]
    assert _STUB_A in evidence
    assert "docs/plans/shipped-plan.md" in evidence


def test_collect_session_has_no_stale_origin_stub_point(monkeypatch, tmp_path):
    _isolate_collect(monkeypatch)

    def _boom(root):
        raise AssertionError("survey must not run at session cadence")

    monkeypatch.setattr(rhr, "survey", _boom)
    result = rhr.collect("session", repo_root=str(tmp_path))
    assert result.judgment_points == []


def test_stale_origin_stubs_empty_survey_is_quiet(monkeypatch, tmp_path):
    monkeypatch.setattr(rhr, "survey", lambda root: _stub_survey())
    result = rhr._read_stale_origin_stubs(str(tmp_path))
    assert result.directives == [] and result.judgment_points == []


def test_stale_origin_stubs_oserror_yields_empty_result(monkeypatch, tmp_path):
    def _raise(root):
        raise OSError("vanished")

    monkeypatch.setattr(rhr, "survey", _raise)
    result = rhr._read_stale_origin_stubs(str(tmp_path))
    assert result.directives == [] and result.judgment_points == []


def test_stale_origin_stubs_non_oserror_propagates(monkeypatch, tmp_path):
    def _raise(root):
        raise TypeError("survey defect")

    monkeypatch.setattr(rhr, "survey", _raise)
    with pytest.raises(TypeError):
        rhr._read_stale_origin_stubs(str(tmp_path))

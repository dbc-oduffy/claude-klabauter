"""Health family of the rebuilt orient brief: one test per R0 requirement id (REQ-H1..H11),
cadence gating, the zero-spawn contract, and the check-only registry seam of
`coordinator/bin/lib/git_hook_install.py` that REQ-H3 reads the fleet through.

Fixtures are `tmp_path` repos plus a `MACHINE_LOCAL_REGISTRY_DIR` registry, so nothing
here reads this box's registry, hooks, or markers.
"""
from __future__ import annotations

import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

from coordinator_core.orient_brief import _health


class _Spawned(AssertionError):
    pass


def _forbid_spawns(monkeypatch) -> None:
    def _no(*a, **k):
        raise _Spawned(f"subprocess spawned: {a[:1]}")

    for name in ("run", "Popen", "check_output", "check_call", "call"):
        monkeypatch.setattr(subprocess, name, _no)


def _toml_value(v: str) -> str:
    return '"' + v.replace("\\", "/") + '"'


@pytest.fixture
def box(tmp_path, monkeypatch):
    """An isolated machine: registry dir, settings home, HOME, state root, and a repo."""
    reg = tmp_path / "registry"
    reg.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / "state").mkdir()
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(reg))
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "settings"))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("CWS_TEST_STATE_ROOT", str(repo / "state"))
    monkeypatch.delenv("HEADER_FILE", raising=False)
    for var in ("COORDINATOR_SESSION_ID", "CLAUDE_SESSION_ID", "CLAUDE_CODE_SESSION_ID"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.chdir(repo)

    class Box:
        registry_dir = reg
        root = repo
        tmp = tmp_path

        @staticmethod
        def write_registry(entries: dict[str, str]) -> None:
            lines = [f"{_toml_value(k)} = {_toml_value(v)}" for k, v in entries.items()]
            (reg / "registry.toml").write_text("\n".join(lines) + "\n", encoding="utf-8")

    from coordinator_core import machine_profile

    machine_profile.reset_cache()
    yield Box
    machine_profile.reset_cache()


def _ids(result) -> list[str]:
    return [d["id"] for d in result.directives] + [p["id"] for p in result.judgment_points]


def _run(cadence: str, root: Path):
    return _health.collect(cadence, repo_root=root)


def _only(probe, cadence: str, root: Path, flat=None):
    ctx = _health._Ctx(cadence=cadence, root=root, flat=flat or {})
    return probe(ctx)


# ---------------------------------------------------------------------------
# REQ-H1 .. H2: the bin and registration probes
# ---------------------------------------------------------------------------

def test_req_h1_sentinel_present_is_silent(box):
    assert _only(_health._claude_klabauter_bin_sentinel, "day", box.root).directives == []


def test_req_h1_empty_bin_dir_emits_directive_with_probe_message(box, monkeypatch):
    probes = _health._load_bin("workday-start-health-probes.py", "workday_start_health_probes")
    empty = box.tmp / "emptybin"
    empty.mkdir()
    monkeypatch.setattr(probes, "_SCRIPT_DIR", str(empty))
    (d,) = _only(_health._claude_klabauter_bin_sentinel, "day", box.root).directives
    assert (d["id"], d["cli"], d["args"]) == (
        "d-claude-klabauter-bin-sentinel", "workday-start-health-probes", ["claude-klabauter-bin-sentinel"]
    )
    assert "CLAUDE-KLABAUTER-BIN PROBE" in d["detail"]


@pytest.mark.parametrize(
    "identity, registered, expect",
    [
        ("engine-authoring", "", True),
        ("engine-authoring", str(_health._ENGINE_ROOT), False),
        ("engine-authoring", str(_health._BIN), True),
        ("engine-mirror", "", False),
    ],
)
def test_req_h2_working_repo_registration(box, monkeypatch, identity, registered, expect):
    probes = _health._load_bin("workday-start-health-probes.py", "workday_start_health_probes")
    monkeypatch.setattr(probes, "_resolve_repo_identity", lambda: identity)
    monkeypatch.setattr("coordinator_core.machine_resolver.registry_get", lambda key: registered)
    result = _only(_health._working_repo_registration, "day", box.root)
    if not expect:
        assert result.directives == []
        return
    (d,) = result.directives
    assert d["id"] == "d-working-repo-registration"
    assert d["args"] == ["working-repo-registration", "--fix"]


# ---------------------------------------------------------------------------
# REQ-H3: hook currency, check only
# ---------------------------------------------------------------------------

def _worktree_repo(box, name: str) -> Path:
    repo = box.tmp / name
    (repo / ".git" / "hooks").mkdir(parents=True)
    (repo / "CLAUDE.md").write_text("x", encoding="utf-8")
    return repo


def _fleet_registry(box, *repos: Path) -> dict[str, str]:
    entries = {"repos.claude_klabauter": str(_health._ENGINE_ROOT)}
    for repo in repos:
        entries[f"repos.{repo.name}"] = str(repo)
    box.write_registry(entries)
    return entries


def test_req_h3_stale_hook_on_author_box_emits_the_repairing_directive(box, monkeypatch):
    repo = _worktree_repo(box, "stale")
    flat = _fleet_registry(box, repo)
    monkeypatch.setattr("coordinator_core.machine_profile.machine_profile", lambda: "author")
    result = _only(_health._hook_currency, "day", box.root, flat)
    (d,) = result.directives
    assert (d["id"], d["cli"], d["args"]) == (
        "d-hook-currency", "workday-start-health-probes", ["hook-currency"]
    )
    assert "stale" in d["detail"] and "prepare-commit-msg" in d["detail"]
    assert result.judgment_points == []
    assert not (repo / ".git" / "hooks" / "prepare-commit-msg").exists(), "check-only wrote a hook"


def test_req_h3_stale_hook_on_consumer_box_asks_instead_of_repairing(box, monkeypatch):
    repo = _worktree_repo(box, "stale")
    flat = _fleet_registry(box, repo)
    monkeypatch.setattr("coordinator_core.machine_profile.machine_profile", lambda: "consumer")
    result = _only(_health._hook_currency, "day", box.root, flat)
    assert result.directives == []
    (p,) = result.judgment_points
    assert p["id"] == "j-hook-currency-repair"
    assert [d["value"] for d in p["dispositions"]] == ["repair_hooks_now", "defer"]
    assert p["reason"] == "recommendation-forbidden" and p["recommendation"] is None


def test_req_h3_walk_that_cannot_run_reads_as_stale_never_clean(box, monkeypatch):
    monkeypatch.setattr("coordinator_core.machine_profile.machine_profile", lambda: "author")

    def _boom(*a, **k):
        raise ImportError("no loader")

    monkeypatch.setattr(_health, "_load_bin", _boom)
    (d,) = _only(_health._hook_currency, "day", box.root, {}).directives
    assert "COULD NOT RUN" in d["detail"]


def test_req_h3_current_fleet_is_silent(box, monkeypatch):
    repo = _worktree_repo(box, "fresh")
    flat = _fleet_registry(box, repo)
    ghi = _health._load_bin("lib/git_hook_install.py", "git_hook_install")
    # Heal the fixture repo once through the unchanged heal path, then check it.
    monkeypatch.setattr(ghi, "_ml_get", lambda ml, key: flat.get(key))
    with _health._bin_on_path():
        ghi.ensure_hooks_fleet(str(_health._BIN), check_only=False)
    assert (repo / ".git" / "hooks" / "prepare-commit-msg").is_file()
    monkeypatch.setattr("coordinator_core.machine_profile.machine_profile", lambda: "author")
    assert _only(_health._hook_currency, "day", box.root, flat).directives == []


def test_hook_check_only_registry_seam_spawns_nothing_and_never_reaches_ml_get(box, monkeypatch):
    repo = _worktree_repo(box, "seamed")
    flat = _fleet_registry(box, repo)
    ghi = _health._load_bin("lib/git_hook_install.py", "git_hook_install")

    def _ml_trap(*a, **k):
        raise AssertionError("check-only registry path reached _ml_get")

    monkeypatch.setattr(ghi, "_ml_get", _ml_trap)
    monkeypatch.setattr(ghi, "_resolve_machine_local_bin", _ml_trap)
    _forbid_spawns(monkeypatch)
    with _health._bin_on_path():
        assert ghi.ensure_hooks_fleet(str(_health._BIN), check_only=True, registry=flat) == 0


def test_hook_heal_path_still_resolves_through_ml_get_without_a_registry(box, monkeypatch):
    repo = _worktree_repo(box, "healed")
    flat = _fleet_registry(box, repo)
    ghi = _health._load_bin("lib/git_hook_install.py", "git_hook_install")
    asked: list[str] = []

    def _ml(ml_bin, key):
        asked.append(key)
        return flat.get(key)

    monkeypatch.setattr(ghi, "_ml_get", _ml)
    with _health._bin_on_path():
        ghi.ensure_hooks_fleet(str(_health._BIN), check_only=False)
    assert "repos.claude_klabauter" in asked


def test_hook_registry_seam_is_refused_on_the_heal_path(box):
    ghi = _health._load_bin("lib/git_hook_install.py", "git_hook_install")
    with pytest.raises(ValueError, match="check_only"):
        ghi.ensure_hooks_fleet(str(_health._BIN), check_only=False, registry={})


# ---------------------------------------------------------------------------
# REQ-H4: git-perf currency
# ---------------------------------------------------------------------------

def _git_config(repo: Path, body: str) -> None:
    (repo / ".git" / "config").write_text(body, encoding="utf-8")


def test_req_h4_missing_untracked_cache_emits_fix_directive_without_spawning(box, monkeypatch):
    repo = _worktree_repo(box, "drifted")
    _git_config(repo, "[core]\n\tbare = false\n")
    _fleet_registry(box, repo)
    _forbid_spawns(monkeypatch)
    (d,) = _only(_health._git_perf_currency, "day", box.root).directives
    assert (d["id"], d["cli"], d["args"]) == (
        "d-git-perf-currency", "workday-start-health-probes", ["git-perf-currency", "--fix"]
    )
    assert "drifted" in d["detail"]


def test_req_h4_current_fleet_is_silent_and_value_not_substring(box, monkeypatch):
    repo = _worktree_repo(box, "current")
    _fleet_registry(box, repo)
    _git_config(repo, "[core]\n\tuntrackedcache = true\n")
    _forbid_spawns(monkeypatch)
    assert _only(_health._git_perf_currency, "day", box.root).directives == []
    _git_config(repo, "[core]\n\tuntrackedcache = false\n")
    assert _only(_health._git_perf_currency, "day", box.root).directives, "= false is not current"


@pytest.mark.parametrize("cadence", ["session", "day", "week"])
def test_req_h4_fires_at_every_cadence(box, cadence):
    repo = _worktree_repo(box, "drifted")
    _fleet_registry(box, repo)
    assert "d-git-perf-currency" in _ids(_run(cadence, box.root))


# ---------------------------------------------------------------------------
# REQ-H5: ceremony hook output
# ---------------------------------------------------------------------------

def test_req_h5_no_registered_command_is_silent_and_spawns_nothing(box, monkeypatch):
    _forbid_spawns(monkeypatch)
    assert _only(_health._ceremony_hook, "day", box.root).directives == []


@pytest.mark.parametrize(
    "cadence, ceremony", [("day", "workday-start"), ("week", "workweek-start")]
)
def test_req_h5_registered_command_output_is_surfaced_verbatim(box, cadence, ceremony):
    key = ceremony.replace("-", "_") + "_post_command"
    (box.root / "coordinator.local.md").write_text(
        f"---\n{key}: \"{Path(sys.executable).as_posix()} -c pass\"\n---\n", encoding="utf-8"
    )
    (d,) = _only(_health._ceremony_hook, cadence, box.root).directives
    assert (d["id"], d["args"]) == ("d-ceremony-hook-output", ["ceremony-hook", ceremony])
    assert d["detail"].startswith(f"Post-{ceremony} hook: ran ")


def test_req_h5_session_ceremony_is_not_a_hook_ceremony_so_nothing_spawns(box, monkeypatch):
    (box.root / "coordinator.local.md").write_text(
        '---\nworkstream_start_post_command: "echo hi"\n---\n', encoding="utf-8"
    )
    _forbid_spawns(monkeypatch)
    assert _only(_health._ceremony_hook, "session", box.root).directives == []


# ---------------------------------------------------------------------------
# REQ-H6: freshness markers
# ---------------------------------------------------------------------------

def _marker(box, text: str) -> None:
    (box.root / "state" / ".workday-start-marker").write_text(text, encoding="utf-8")


def _today() -> str:
    from coordinator_core.daily_day import local_day

    return local_day(None)


def test_req_h6_day_stale_marker_names_the_writer(box):
    _marker(box, "2000-01-01")
    (d,) = _only(_health._marker_freshness, "day", box.root, {}).directives
    assert (d["id"], d["cli"], d["args"]) == ("d-workday-marker-write", "write-workday-start-marker", [])
    assert "2000-01-01" in d["detail"]


def test_req_h6_day_absent_marker_fires_and_current_marker_is_silent(box):
    assert _only(_health._marker_freshness, "day", box.root, {}).directives
    _marker(box, _today() + "\n")
    assert _only(_health._marker_freshness, "day", box.root, {}).directives == []


def test_req_h6_session_asks_only_for_a_registered_onboarded_repo(box):
    (box.root / "archive").mkdir()
    _marker(box, "2000-01-01")
    # Unregistered: silent.
    assert _only(_health._marker_freshness, "session", box.root, {}).judgment_points == []
    box.write_registry({"repos.thing": str(box.root)})
    (p,) = _only(_health._marker_freshness, "session", box.root, {}).judgment_points
    assert p["id"] == "j-session-day-review-due"
    assert [d["value"] for d in p["dispositions"]] == ["run_workday_start_now", "defer"]
    assert p["reason"] == "recommendation-forbidden"


def test_req_h6_session_not_onboarded_is_silent(box):
    box.write_registry({"repos.thing": str(box.root)})
    _marker(box, "2000-01-01")
    assert _only(_health._marker_freshness, "session", box.root, {}).judgment_points == []


@pytest.mark.parametrize("verdict, fires", [("STALE", True), ("MILD", True), ("FRESH", False), ("UNKNOWN", False)])
def test_req_h6_week_asks_on_stale_or_mild_header(box, monkeypatch, verdict, fires):
    header = box.root / "state" / "week-changelog" / "HEADER.md"
    header.parent.mkdir()
    header.write_text("x", encoding="utf-8")
    monkeypatch.setattr(_health, "_week_staleness", lambda state_root: verdict)
    points = _only(_health._marker_freshness, "week", box.root, {}).judgment_points
    assert bool(points) is fires
    if fires:
        (p,) = points
        assert p["id"] == "j-week-marker-freshness"
        assert verdict in p["question"]
        assert [d["value"] for d in p["dispositions"]] == ["reset_week", "update_in_place"]


def test_req_h6_week_without_a_header_is_silent(box):
    assert _only(_health._marker_freshness, "week", box.root, {}).judgment_points == []


def test_req_h6_week_staleness_unknown_without_a_reset_sha_spawns_nothing(box, monkeypatch):
    header = box.root / "state" / "week-changelog"
    header.mkdir()
    (header / "HEADER.md").write_text(
        "**Week starting:** not yet set\n**Prior week released:** nothing\n", encoding="utf-8"
    )
    _forbid_spawns(monkeypatch)
    assert _health._week_staleness(str(box.root / "state")) == "UNKNOWN"


def test_req_h6_week_staleness_thresholds_use_the_commit_distance(box, monkeypatch):
    header = box.root / "state" / "week-changelog"
    header.mkdir()
    old = (date.today() - timedelta(days=9)).isoformat()
    (header / "HEADER.md").write_text(
        f"**Prior week released:** commit {'a' * 40}\n**Week starting:** {old}\n", encoding="utf-8"
    )
    calls: list[list[str]] = []

    def _fake(argv, **kw):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="30\n", stderr="")

    monkeypatch.setattr(subprocess, "run", _fake)
    assert _health._week_staleness(str(box.root / "state")) == "STALE"
    assert len(calls) == 1 and calls[0][:3] == ["git", "rev-list", "--count"]
    monkeypatch.setattr(
        subprocess, "run", lambda argv, **kw: subprocess.CompletedProcess(argv, 128, stdout="", stderr="bad")
    )
    assert _health._week_staleness(str(box.root / "state")) == "UNKNOWN"


# ---------------------------------------------------------------------------
# REQ-H7: stale origin stubs
# ---------------------------------------------------------------------------

def _stub(box, name: str, *, state="ready_to_fire", pair=("rm-1", "st-1"), created="2026-07-01") -> None:
    d = box.root / "state" / "handoffs"
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text(
        f"---\ntitle: stub\ncreated: {created}\nstatus: open\nkind: roadmap-baton\n"
        f"roadmap_id: \"{pair[0]}\"\nstub_id: \"{pair[1]}\"\ndeployment_state: {state}\n---\nbody\n",
        encoding="utf-8",
    )


def test_req_h7_stub_with_a_shipped_carrier_is_one_reportable_point(box):
    _stub(box, "2026-07-01_stub.md")
    arch = box.root / "archive" / "handoffs" / "2026-08"
    arch.mkdir(parents=True)
    (arch / "2026-08-01_done.md").write_text(
        "---\ntitle: done\ncreated: 2026-08-01\ndeployment_state: shipped\n"
        "closes_stubs:\n  - roadmap_id: \"rm-1\"\n    stub_id: \"st-1\"\n---\n",
        encoding="utf-8",
    )
    (p,) = _only(_health._stale_origin_stubs, "day", box.root).judgment_points
    assert p["id"] == "j-stale-origin-stubs" and p["reportable"] is True
    assert [d["value"] for d in p["dispositions"]] == ["close_stubs", "leave_as_is"]
    assert "2026-07-01_stub.md" in p["evidence"] and "2026-08-01_done.md" in p["evidence"]


def test_req_h7_plan_in_a_ripe_status_is_evidence(box):
    _stub(box, "2026-07-01_stub.md", state="awaiting_gate")
    plans = box.root / "docs" / "plans"
    plans.mkdir(parents=True)
    (plans / "2026-08-02-plan.md").write_text(
        "---\ntitle: p\nstatus: implemented\nroadmap_id: \"rm-1\"\nstub_id: \"st-1\"\n---\n",
        encoding="utf-8",
    )
    assert _only(_health._stale_origin_stubs, "day", box.root).judgment_points


def test_req_h7_no_carrier_or_no_live_stub_is_silent(box):
    _stub(box, "2026-07-01_stub.md")
    assert _only(_health._stale_origin_stubs, "day", box.root).judgment_points == []
    _stub(box, "2026-07-01_stub.md", state="shipped")
    assert _only(_health._stale_origin_stubs, "day", box.root).judgment_points == []


def test_req_h7_only_runs_at_day(box):
    _stub(box, "2026-07-01_stub.md")
    assert "j-stale-origin-stubs" not in _ids(_run("session", box.root))
    assert "j-stale-origin-stubs" not in _ids(_run("week", box.root))


# ---------------------------------------------------------------------------
# REQ-H8: plugin drift
# ---------------------------------------------------------------------------

def _mirror(box, live: Path, source: Path, name="addon") -> dict[str, str]:
    flat = {
        f"plugin.mirrors.{name}.propagation_mode": "copy_install",
        f"plugin.mirrors.{name}.live_path": str(live),
        f"plugin.mirrors.{name}.source_path": str(source),
    }
    box.write_registry(flat)
    return flat


def test_req_h8_missing_and_malformed_sentinels_are_drift(box, monkeypatch):
    live, src = box.tmp / "live", box.tmp / "src"
    live.mkdir()
    src.mkdir()
    flat = _mirror(box, live, src)
    _forbid_spawns(monkeypatch)
    (d,) = _only(_health._plugin_drift, "day", box.root, flat).directives
    assert (d["id"], d["cli"], d["args"]) == ("d-plugin-drift", "check-plugin-drift", [])
    assert "addon" in d["detail"] and "sentinel missing" in d["detail"]
    (live / "version.txt").write_text("not-a-sha", encoding="utf-8")
    (d,) = _only(_health._plugin_drift, "day", box.root, flat).directives
    assert "sentinel malformed" in d["detail"]


def test_req_h8_good_sentinel_and_matching_pyproject_is_silent(box):
    live, src = box.tmp / "live", box.tmp / "src"
    live.mkdir()
    src.mkdir()
    (live / "version.txt").write_text("a" * 40 + "\n", encoding="utf-8")
    flat = _mirror(box, live, src)
    assert _only(_health._plugin_drift, "day", box.root, flat).directives == []


def test_req_h8_pyproject_changed_since_refresh_is_drift(box):
    from coordinator_core.plugin_health import refresh_log as drift

    live, src = box.tmp / "live", box.tmp / "src"
    live.mkdir()
    src.mkdir()
    (live / "version.txt").write_text("a" * 40, encoding="utf-8")
    (src / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    flat = _mirror(box, live, src)
    log = drift.resolve_refresh_log()
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("2026-01-01 addon refreshed pyproject_hash=" + "0" * 64 + "\n", encoding="utf-8")
    (d,) = _only(_health._plugin_drift, "day", box.root, flat).directives
    assert "pyproject changed since last refresh" in d["detail"]


def test_req_h8_no_mirrors_is_silent_and_week_session_skip_it(box):
    assert _only(_health._plugin_drift, "day", box.root, {}).directives == []
    live, src = box.tmp / "live", box.tmp / "src"
    live.mkdir()
    src.mkdir()
    box.write_registry({})
    flat = _mirror(box, live, src)
    assert "d-plugin-drift" not in _ids(_run("session", box.root))
    assert "d-plugin-drift" not in _ids(_run("week", box.root))
    assert flat


# ---------------------------------------------------------------------------
# REQ-H9: git-maintenance liveness
# ---------------------------------------------------------------------------

def test_req_h9_never_stamped_and_stale_are_distinct_and_fresh_is_silent(box, monkeypatch):
    _forbid_spawns(monkeypatch)
    (d,) = _only(_health._git_maintenance, "day", box.root).directives
    assert (d["id"], d["cli"], d["args"]) == (
        "d-git-maintenance-daily", "coordinator-git-maintenance", ["daily"]
    )
    assert "never been stamped" in d["detail"]
    live = box.root / "state" / "housekeeping-liveness.json"
    live.write_text('{"git_maintenance": "2000-01-01T00:00:00Z"}', encoding="utf-8")
    (d,) = _only(_health._git_maintenance, "day", box.root).directives
    assert "stale" in d["detail"]
    from datetime import datetime, timezone

    live.write_text(
        '{"git_maintenance": "%s"}' % datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        encoding="utf-8",
    )
    assert _only(_health._git_maintenance, "day", box.root).directives == []


def test_req_h9_week_names_the_weekly_tier_and_session_skips(box):
    (d,) = _only(_health._git_maintenance, "week", box.root).directives
    assert (d["id"], d["args"]) == ("d-git-maintenance-weekly", ["weekly"])
    assert "d-git-maintenance-daily" not in _ids(_run("session", box.root))
    assert not [i for i in _ids(_run("session", box.root)) if i.startswith("d-git-maintenance")]


# ---------------------------------------------------------------------------
# REQ-H10: goal coverage
# ---------------------------------------------------------------------------

def _goal(box, gid: str) -> None:
    d = box.root / "state" / "goals"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{gid}.yaml").write_text(
        f'schema: goal\nid: "{gid}"\ntitle: "{gid}"\nstatus: active\n', encoding="utf-8"
    )


def test_req_h10_zero_coverage_goals_are_listed_sorted_and_covered_ones_are_not(box, monkeypatch):
    _goal(box, "goal-b")
    _goal(box, "goal-a")
    _goal(box, "goal-covered")
    handoffs = box.root / "state" / "handoffs"
    handoffs.mkdir(parents=True)
    (handoffs / "2026-07-01_h.md").write_text(
        "---\ntitle: h\norigin_goal_id:\n  - goal-covered\n---\n", encoding="utf-8"
    )
    _forbid_spawns(monkeypatch)
    (d,) = _only(_health._goal_coverage, "week", box.root).directives
    assert (d["id"], d["cli"], d["args"]) == ("d-goal-coverage", "goal-coverage-scan", ["--format", "text"])
    assert d["detail"] == "zero-coverage active goal(s): goal-a, goal-b"


def test_req_h10_no_active_goals_and_all_covered_are_silent(box):
    assert _only(_health._goal_coverage, "week", box.root).directives == []
    _goal(box, "goal-a")
    handoffs = box.root / "state" / "handoffs"
    handoffs.mkdir(parents=True)
    (handoffs / "2026-07-01_h.md").write_text(
        "---\ntitle: h\norigin_goal_id: [goal-a]\n---\n", encoding="utf-8"
    )
    assert _only(_health._goal_coverage, "week", box.root).directives == []


def test_req_h10_only_runs_at_week(box):
    _goal(box, "goal-a")
    assert "d-goal-coverage" not in _ids(_run("day", box.root))
    assert "d-goal-coverage" not in _ids(_run("session", box.root))
    assert "d-goal-coverage" in _ids(_run("week", box.root))


# ---------------------------------------------------------------------------
# REQ-H11: workweek trail scope
# ---------------------------------------------------------------------------

def test_req_h11_no_session_id_is_silent(box):
    assert _only(_health._trail_scope, "week", box.root).directives == []


def test_req_h11_missing_header_and_unparseable_header(box, monkeypatch):
    monkeypatch.setenv("COORDINATOR_SESSION_ID", "11111111-2222-3333-4444-555555555555")
    (d,) = _only(_health._trail_scope, "week", box.root).directives
    assert (d["id"], d["cli"], d["args"]) == ("d-workweek-trail-scope", "workweek-trail-scope", [])
    assert "not found -- run /workweek-start to initialise" in d["detail"]
    header = box.root / "state" / "week-changelog"
    header.mkdir()
    (header / "HEADER.md").write_text("no date here\n", encoding="utf-8")
    (d,) = _only(_health._trail_scope, "week", box.root).directives
    assert "cannot parse 'Week starting:' YYYY-MM-DD" in d["detail"]
    (header / "HEADER.md").write_text("**Week starting:** 2026-10-01\n", encoding="utf-8")
    assert _only(_health._trail_scope, "week", box.root).directives == []


def test_req_h11_header_file_env_overrides_and_writes_no_shard(box, monkeypatch):
    monkeypatch.setenv("COORDINATOR_SESSION_ID", "11111111-2222-3333-4444-555555555555")
    custom = box.tmp / "elsewhere.md"
    custom.write_text("**Week starting:** 2026-10-01\n", encoding="utf-8")
    monkeypatch.setenv("HEADER_FILE", str(custom))
    assert _only(_health._trail_scope, "week", box.root).directives == []
    assert not (box.root / "state" / "review-trail").exists()


# ---------------------------------------------------------------------------
# Family contract
# ---------------------------------------------------------------------------

def test_every_probe_row_cites_a_distinct_requirement_and_a_known_cadence():
    reqs = [r for r, _p, _c in _health._PROBES]
    assert reqs == [f"REQ-H{i}" for i in range(1, 12)]
    assert all(set(c) <= {"session", "day", "week"} for _r, _p, c in _health._PROBES)


def test_a_raising_probe_does_not_blind_the_rest(box, monkeypatch, capsys):
    def _boom(ctx):
        raise RuntimeError("probe broke")

    survivor = lambda ctx: _health.ReaderResult(directives=[_health._directive("d-x", "cli", [], "ok")])
    monkeypatch.setattr(
        _health, "_PROBES",
        (("REQ-H1", _boom, ("day",)), ("REQ-H2", survivor, ("day",))),
    )
    result = _run("day", box.root)
    assert _ids(result) == ["d-x"]
    assert "REQ-H1" in capsys.readouterr().err


def test_directives_carry_the_envelope_shape(box):
    repo = _worktree_repo(box, "drifted")
    _fleet_registry(box, repo)
    for d in _run("day", box.root).directives:
        assert set(d) == {"id", "cli", "args", "depends_on", "already_satisfied", "detail"}
        assert d["depends_on"] is None and d["already_satisfied"] is False


def test_clean_session_collect_spawns_nothing(box, monkeypatch):
    repo = _worktree_repo(box, "healthy")
    _git_config(repo, "[core]\n\tuntrackedcache = true\n")
    _fleet_registry(box, repo)
    _forbid_spawns(monkeypatch)
    _run("session", box.root)


def test_day_collect_spawns_nothing_without_a_registered_ceremony_command(box, monkeypatch):
    repo = _worktree_repo(box, "healthy")
    _git_config(repo, "[core]\n\tuntrackedcache = true\n")
    _fleet_registry(box, repo)
    _marker(box, _today())
    _forbid_spawns(monkeypatch)
    _run("day", box.root)


def test_week_collect_spawns_nothing_when_the_header_needs_no_git(box, monkeypatch):
    repo = _worktree_repo(box, "healthy")
    _git_config(repo, "[core]\n\tuntrackedcache = true\n")
    _fleet_registry(box, repo)
    _forbid_spawns(monkeypatch)
    _run("week", box.root)

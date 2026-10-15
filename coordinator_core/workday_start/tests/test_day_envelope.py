"""Reader branches of the day envelope, and its output driven through the driver's parse and decide-resolve."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from coordinator_core.ceremony_common.run import decide, envelope as envelope_mod
from coordinator_core.ops import agent_worktree_sweep
from coordinator_core.workday_start import day_envelope as mod
from coordinator_core.workday_start import rules, run_spec

ENVELOPE_CAP = 30000


def _ids(result):
    directives, points = result
    return [d["id"] for d in directives], [p["id"] for p in points]


@pytest.fixture
def author(monkeypatch):
    monkeypatch.setattr(mod, "_author_profile", lambda: True)


@pytest.fixture
def consumer(monkeypatch):
    monkeypatch.setattr(mod, "_author_profile", lambda: False)


def _fm(path: Path, **fields: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "".join(f"{k}: {v}\n" for k, v in fields.items())
    path.write_text(f"---\n{body}---\n\nbody\n", encoding="utf-8")
    return path


def test_addon_health_one_directive_per_line_capped(monkeypatch):
    from coordinator_core.plugin_health import scan

    monkeypatch.setattr(scan, "_run", lambda mode: ([f"[health] addon {i} red" for i in range(30)], 0))
    directives, points = mod._addon_health(Path("."))
    assert len(directives) == mod.MAX_ADDON_LINES and points == []
    assert directives[0]["id"] == "d-addon-health-1" and directives[0]["cli"] == "scan-addon-health"
    assert directives[0]["args"] == ["--red-and-stale"]


def test_addon_health_silent_when_clean(monkeypatch):
    from coordinator_core.plugin_health import scan

    monkeypatch.setattr(scan, "_run", lambda mode: ([], 0))
    assert mod._addon_health(Path(".")) == ([], [])


def test_rag_staleness_missing_stale_fresh_and_consumer(tmp_path, author, monkeypatch):
    assert _ids(mod._rag_staleness(tmp_path)) == (["d-rag-staleness-regen"], [])
    repomap = tmp_path / mod.REPOMAP_RELPATH
    repomap.parent.mkdir(parents=True)
    repomap.write_text("x", encoding="utf-8")
    assert mod._rag_staleness(tmp_path) == ([], [])
    old = time.time() - mod.REPOMAP_MAX_AGE_S - 3600
    os.utime(repomap, (old, old))
    directives, _ = mod._rag_staleness(tmp_path)
    assert directives[0]["cli"] == "generate-repomap" and "25h old" in directives[0]["detail"]
    monkeypatch.setattr(mod, "_author_profile", lambda: False)
    assert mod._rag_staleness(tmp_path) == ([], [])


def test_bin_sentinel_present_missing_and_unreadable_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(mod, "_BIN_DIR", tmp_path / "bin")
    assert _ids(mod._bin_sentinel(tmp_path)) == (["d-claude-klabauter-bin-sentinel"], [])
    (tmp_path / "bin").mkdir()
    assert _ids(mod._bin_sentinel(tmp_path)) == (["d-claude-klabauter-bin-sentinel"], [])
    (tmp_path / "bin" / "archive-stamp-cli.py").write_text("x", encoding="utf-8")
    assert mod._bin_sentinel(tmp_path) == ([], [])


def _engine_tree(root: Path, repo_id: str = "claude-klabauter") -> None:
    manifest = root / "docs" / "install" / "agent-install-manifest.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(json.dumps({"repo_id": repo_id}), encoding="utf-8")


def test_working_repo_registration_branches(tmp_path, monkeypatch):
    from coordinator_core import machine_resolver

    assert mod._working_repo_registration(tmp_path) == ([], [])
    _engine_tree(tmp_path)
    monkeypatch.setattr(machine_resolver, "registry_get", lambda key: None)
    directives, _ = mod._working_repo_registration(tmp_path)
    assert directives[0]["id"] == "d-working-repo-registration"
    assert directives[0]["args"] == ["working-repo-registration", "--fix"]
    assert "not registered" in directives[0]["detail"]
    monkeypatch.setattr(machine_resolver, "registry_get", lambda key: str(tmp_path / "elsewhere"))
    assert "registered as" in mod._working_repo_registration(tmp_path)[0][0]["detail"]
    monkeypatch.setattr(machine_resolver, "registry_get", lambda key: str(tmp_path))
    assert mod._working_repo_registration(tmp_path) == ([], [])


def test_working_repo_registration_silent_on_a_foreign_tree(tmp_path, monkeypatch):
    from coordinator_core import machine_resolver

    _engine_tree(tmp_path, repo_id="some-other-repo")
    monkeypatch.setattr(machine_resolver, "registry_get", lambda key: None)
    assert mod._working_repo_registration(tmp_path) == ([], [])


def test_working_repo_registration_silent_on_a_klabauter_mirror(tmp_path, monkeypatch):
    from coordinator_core import machine_resolver

    _engine_tree(tmp_path)
    (tmp_path / "AGENTS.md").write_text("# claude-klabauter\n", encoding="utf-8")
    monkeypatch.setattr(machine_resolver, "registry_get", lambda key: None)
    assert mod._working_repo_registration(tmp_path) == ([], [])


def _env_home(tmp_path, monkeypatch, effort=None, model=None, session="sess-1"):
    home = tmp_path / "home" / ".claude"
    home.mkdir(parents=True)
    if effort is not None:
        (home / "settings.json").write_text(json.dumps({"effortLevel": effort}), encoding="utf-8")
    if model is not None:
        projects = home / "projects" / "p"
        projects.mkdir(parents=True)
        (projects / f"{session}.jsonl").write_text('{"x":1}\n{"model": "%s"}\n' % model, encoding="utf-8")
    monkeypatch.setattr(mod, "_claude_home", lambda: home)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", session)
    return tmp_path / "proj"


def test_em_environment_silent_on_medium_and_opus(tmp_path, monkeypatch):
    proj = _env_home(tmp_path, monkeypatch, effort="medium", model="claude-opus-4-8")
    assert mod._em_environment(proj) == ([], [])


def test_em_environment_silent_when_model_unknown(tmp_path, monkeypatch):
    proj = _env_home(tmp_path, monkeypatch, effort="medium")
    assert mod._em_environment(proj) == ([], [])


def test_em_environment_unpinned_and_non_opus(tmp_path, monkeypatch):
    proj = _env_home(tmp_path, monkeypatch, model="claude-sonnet-5")
    _, points = mod._em_environment(proj)
    assert [p["id"] for p in points] == ["j-em-env-effort", "j-em-env-model"]
    assert all(p["reportable"] is True and p["recommendation"] is None for p in points)
    assert [d["value"] for d in points[0]["dispositions"]] == ["pin", "leave"]
    assert [d["value"] for d in points[1]["dispositions"]] == ["switch", "leave"]
    assert "unpinned" in points[0]["evidence"]


def test_em_environment_pinned_non_medium(tmp_path, monkeypatch):
    proj = _env_home(tmp_path, monkeypatch, effort="high", model="claude-opus-4-8")
    assert _ids(mod._em_environment(proj)) == ([], ["j-em-env-effort"])


def _worktree_seams(monkeypatch, roots, states):
    monkeypatch.setattr(mod, "_current_branch", lambda root: "work/x/2026-10-15")
    monkeypatch.setattr(mod, "_agent_worktrees", lambda root: roots)
    monkeypatch.setattr(
        agent_worktree_sweep, "classify_worktree",
        lambda path, ref: agent_worktree_sweep.Classification(states[path], 1, 2),
    )


def test_agent_worktrees_clean_reap_dirty_asks(monkeypatch):
    roots = ["/r/.claude/worktrees/agent-a", "/r/.claude/worktrees/agent-b", "/r/.claude/worktrees/agent-c"]
    _worktree_seams(monkeypatch, roots, dict(zip(roots, ["empty-clean", "dirty", "commits-clean"])))
    directives, points = mod._agent_worktrees_reader(Path("."))
    assert [d["id"] for d in directives] == ["d-worktree-reap-1", "d-worktree-reap-3"]
    assert directives[0]["cli"] == "agent-worktree-sweep" and directives[0]["args"] == ["--reap"]
    assert [p["id"] for p in points] == ["j-worktree-dirty-2"]
    assert [d["value"] for d in points[0]["dispositions"]] == ["pm_reviews_manually", "leave_for_now"]


def test_agent_worktrees_absent_when_no_branch_or_none(monkeypatch):
    monkeypatch.setattr(mod, "_current_branch", lambda root: None)
    assert mod._agent_worktrees_reader(Path(".")) == ([], [])
    monkeypatch.setattr(mod, "_current_branch", lambda root: "main")
    monkeypatch.setattr(mod, "_agent_worktrees", lambda root: [])
    assert mod._agent_worktrees_reader(Path(".")) == ([], [])


def test_agent_worktrees_classification_is_capped(monkeypatch):
    roots = [f"/r/.claude/worktrees/agent-{i}" for i in range(mod.MAX_CLASSIFIED_WORKTREES + 4)]
    _worktree_seams(monkeypatch, roots, {r: "empty-clean" for r in roots})
    assert len(mod._agent_worktrees_reader(Path("."))[0]) == mod.MAX_CLASSIFIED_WORKTREES


def test_agent_worktrees_found_by_gitdir_records(tmp_path):
    git = tmp_path / ".git"
    agent = tmp_path / ".claude" / "worktrees" / "agent-1"
    other = tmp_path / "elsewhere"
    gone = tmp_path / ".claude" / "worktrees" / "agent-gone"
    for name, target in (("a", agent), ("b", other), ("c", gone)):
        (git / "worktrees" / name).mkdir(parents=True)
        (git / "worktrees" / name / "gitdir").write_text(str(target / ".git") + "\n", encoding="utf-8")
    agent.mkdir(parents=True)
    other.mkdir()
    assert mod._agent_worktrees(tmp_path) == [agent.as_posix()]


def test_hook_currency_current_is_silent(monkeypatch, author):
    monkeypatch.setattr(mod, "_hook_walk_report", lambda: "")
    assert mod._hook_currency(Path(".")) == ([], [])


def test_hook_currency_author_gets_a_directive(monkeypatch, author):
    monkeypatch.setattr(mod, "_hook_walk_report", lambda: "stale hook in repos.a\nand b")
    directives, points = mod._hook_currency(Path("."))
    assert points == [] and directives[0]["id"] == "d-hook-currency"
    assert directives[0]["args"] == ["hook-currency"] and directives[0]["depends_on"] is None


def test_hook_currency_consumer_gets_a_point_gating_the_directive(monkeypatch, consumer):
    monkeypatch.setattr(mod, "_hook_walk_report", lambda: "stale")
    directives, points = mod._hook_currency(Path("."))
    point = points[0]
    assert point["id"] == "j-hook-currency-repair" and point["recommendation"] is None
    assert directives[0]["depends_on"] == "j-hook-currency-repair"
    repair = next(d for d in point["dispositions"] if d["value"] == "repair_hooks_now")
    assert repair["resolves"] == ["d-hook-currency"]
    assert "defer" in [d["value"] for d in point["dispositions"]]


def test_hook_walk_that_cannot_run_reads_as_stale(monkeypatch):
    import git_hook_install

    mod._bin_on_path()

    def boom(*a, **k):
        raise RuntimeError("registry unreadable")

    monkeypatch.setattr(git_hook_install, "ensure_hooks_fleet", boom)
    assert "COULD NOT RUN" in mod._hook_walk_report()


def _perf_fleet(monkeypatch, tmp_path, configs):
    from coordinator_core.install import git_perf_config

    items = []
    for key, text in configs.items():
        repo = tmp_path / key
        (repo / ".git").mkdir(parents=True)
        if text is not None:
            (repo / ".git" / "config").write_text(text, encoding="utf-8")
        items.append(("worktree", key, str(repo)))
    monkeypatch.setattr(
        git_perf_config, "iter_fleet_worktrees",
        lambda bin_dir: git_perf_config.FleetWalkResult(ok=True, roots_count=len(items), items=items),
    )


def test_git_perf_currency_current_is_silent(monkeypatch, tmp_path):
    _perf_fleet(monkeypatch, tmp_path, {"a": "[core]\n\tuntrackedCache = true\n"})
    assert mod._git_perf_currency(tmp_path) == ([], [])


def test_git_perf_currency_drift_names_the_repo(monkeypatch, tmp_path):
    _perf_fleet(monkeypatch, tmp_path, {"a": "[core]\n\tuntrackedCache = true\n", "b": "[core]\n", "c": None})
    directives, _ = mod._git_perf_currency(tmp_path)
    assert directives[0]["id"] == "d-git-perf-currency"
    assert directives[0]["args"] == ["git-perf-currency", "--fix"]
    assert "drift b" in directives[0]["detail"] and "drift c" in directives[0]["detail"]
    assert "drift a" not in directives[0]["detail"]


def test_git_perf_currency_unwalkable_fleet_is_not_current(monkeypatch, tmp_path):
    from coordinator_core.install import git_perf_config

    monkeypatch.setattr(
        git_perf_config, "iter_fleet_worktrees",
        lambda bin_dir: git_perf_config.FleetWalkResult(ok=False, reason="no_roots"),
    )
    directives, _ = mod._git_perf_currency(tmp_path)
    assert "no_roots" in directives[0]["detail"]


def _pair(**extra):
    return {"roadmap_id": "rm-1", "stub_id": "stub-1", **extra}


def _stub(tmp_path, name="stub.md", **extra):
    return _fm(tmp_path / "state" / "handoffs" / name, kind="spinoff-roadmap", deployment_state="ready_to_fire", **_pair(**extra))


def test_stale_origin_stubs_plan_evidence(tmp_path):
    _stub(tmp_path)
    _fm(tmp_path / "docs" / "plans" / "p.md", status="implemented", **_pair())
    directives, points = mod._stale_origin_stubs(tmp_path)
    point = points[0]
    assert point["id"] == "j-stale-origin-stubs" and point["reportable"] is True
    assert point["recommendation"]["disposition"] == "close_stubs"
    assert [d["value"] for d in point["dispositions"]] == ["close_stubs", "leave_as_is"]
    assert point["dispositions"][0]["resolves"] == ["d-close-origin-stub-1"]
    assert directives[0]["args"] == ["--plan", "docs/plans/p.md"]
    assert directives[0]["depends_on"] == "j-stale-origin-stubs"


def test_stale_origin_stubs_shipped_handoff_evidence(tmp_path):
    _stub(tmp_path)
    _fm(tmp_path / "state" / "handoffs" / "done.md", kind="roadmap-baton", deployment_state="shipped", **_pair())
    directives, points = mod._stale_origin_stubs(tmp_path)
    assert directives[0]["args"] == ["--handoff", "state/handoffs/done.md"]
    assert len(points) == 1


def test_stale_origin_stubs_no_shipping_record_is_silent(tmp_path):
    _stub(tmp_path)
    _fm(tmp_path / "docs" / "plans" / "p.md", status="executing", **_pair())
    _fm(tmp_path / "state" / "handoffs" / "live.md", kind="roadmap-baton", deployment_state="in_flight", **_pair())
    assert mod._stale_origin_stubs(tmp_path) == ([], [])


def test_stale_origin_stubs_non_stub_and_closed_stub_are_ignored(tmp_path):
    _fm(tmp_path / "state" / "handoffs" / "a.md", kind="session", deployment_state="ready_to_fire", **_pair())
    _fm(tmp_path / "state" / "handoffs" / "b.md", kind="spinoff", deployment_state="shipped", **_pair())
    _fm(tmp_path / "state" / "handoffs" / "c.md", kind="spinoff", deployment_state="ready_to_fire", roadmap_id="x")
    _fm(tmp_path / "docs" / "plans" / "p.md", status="implemented", **_pair())
    assert mod._stale_origin_stubs(tmp_path) == ([], [])


def test_stale_origin_stubs_absent_directories_are_silent(tmp_path):
    assert mod._stale_origin_stubs(tmp_path) == ([], [])


def test_stale_origin_stubs_stub_cap_reports_the_remainder(tmp_path):
    for i in range(mod.MAX_STUB_DIRECTIVES + 3):
        _fm(tmp_path / "state" / "handoffs" / f"s{i:02}.md", kind="spinoff", deployment_state="ready_to_fire", roadmap_id="r", stub_id=f"s{i}")
        _fm(tmp_path / "docs" / "plans" / f"p{i:02}.md", status="implemented", roadmap_id="r", stub_id=f"s{i}")
    directives, points = mod._stale_origin_stubs(tmp_path)
    assert len(directives) == mod.MAX_STUB_DIRECTIVES
    assert points[0]["question"].startswith(f"{mod.MAX_STUB_DIRECTIVES + 3} origin stub(s)")


def test_a_failing_reader_is_named_and_hides_nothing(monkeypatch):
    def boom(root):
        raise OSError("disk gone")

    monkeypatch.setattr(mod, "READERS", (("broken", boom), ("fine", lambda root: ([{"id": "d-x"}], []))))
    envelope = mod.build_envelope(Path("."))
    assert envelope["directives"] == [{"id": "d-x"}]
    assert envelope["reader_errors"] == ["broken: OSError: disk gone"]
    assert "judgment_points" not in envelope


def test_silent_envelope_is_an_empty_object(monkeypatch):
    monkeypatch.setattr(mod, "READERS", (("quiet", lambda root: ([], [])),))
    assert mod.day_envelope(Path(".")) == (0, "{}")


def test_plan_orphan_tiers_is_not_produced():
    assert "d-plan-orphan-tiers" not in json.dumps([name for name, _ in mod.READERS])
    assert not any("orphan" in name for name, _ in mod.READERS)


def test_worst_case_output_stays_under_the_context_flood_cap(monkeypatch):
    from coordinator_core.plugin_health import scan

    monkeypatch.setattr(scan, "_run", lambda mode: (["x" * 5000] * 500, 0))
    monkeypatch.setattr(mod, "READERS", (("addon-health", mod._addon_health),))
    assert len(mod.day_envelope(Path("."))[1].encode("utf-8")) < ENVELOPE_CAP


def _full_envelope(monkeypatch, tmp_path, *, profile_author: bool) -> dict:
    """Every kept reader firing at once, through its real function with seams at the machine edge."""
    from coordinator_core.install import git_perf_config
    from coordinator_core.plugin_health import scan

    monkeypatch.setattr(mod, "_author_profile", lambda: profile_author)
    monkeypatch.setattr(scan, "_run", lambda mode: (["[health] addon red"], 0))
    monkeypatch.setattr(mod, "_BIN_DIR", tmp_path / "nobin")
    monkeypatch.setattr(mod, "_hook_walk_report", lambda: "stale hook")
    monkeypatch.setattr(
        git_perf_config, "iter_fleet_worktrees",
        lambda bin_dir: git_perf_config.FleetWalkResult(ok=False, reason="no_roots"),
    )
    roots = ["/r/.claude/worktrees/agent-a", "/r/.claude/worktrees/agent-b"]
    _worktree_seams(monkeypatch, roots, dict(zip(roots, ["empty-clean", "dirty"])))
    from coordinator_core import machine_resolver

    _engine_tree(tmp_path)
    monkeypatch.setattr(machine_resolver, "registry_get", lambda key: None)
    _stub(tmp_path)
    _fm(tmp_path / "docs" / "plans" / "p.md", status="implemented", **_pair())
    _env_home(tmp_path, monkeypatch, effort="high", model="claude-sonnet-5")
    code, text = mod.day_envelope(tmp_path)
    assert code == 0
    return envelope_mod.parse(text)


EXPECTED_DIRECTIVES = {
    "d-addon-health-1", "d-rag-staleness-regen", "d-worktree-reap-1", "d-claude-klabauter-bin-sentinel",
    "d-working-repo-registration", "d-hook-currency", "d-git-perf-currency", "d-close-origin-stub-1",
}


def test_author_envelope_through_the_driver_parse_and_decide_resolve(monkeypatch, tmp_path):
    env = _full_envelope(monkeypatch, tmp_path, profile_author=True)
    assert {d["id"] for d in env["directives"]} == EXPECTED_DIRECTIVES
    assert {p["id"] for p in env["judgment_points"]} == {
        "j-worktree-dirty-2", "j-em-env-effort", "j-em-env-model", "j-stale-origin-stubs",
    }
    resolution = decide.resolve(
        env["judgment_points"], env["directives"], rules=rules.POINT_RULES, context={"envelope": env}
    )
    decided = {d.point_id: d for d in resolution.decisions}
    assert decided["j-stale-origin-stubs"].decision == "close_stubs"
    assert decided["j-stale-origin-stubs"].rule == rules.RECOMMENDATION
    assert {o["id"] for o in resolution.observations} == {"j-worktree-dirty-2", "j-em-env-effort", "j-em-env-model"}
    assert resolution.judgment_entries == []

    steps = envelope_mod.steps_from_envelope(env, stage_of=run_spec.SPEC.stage_of, costs={})
    assert {s.id for s in steps} == EXPECTED_DIRECTIVES
    by_id = {p["id"]: p for p in env["judgment_points"]}
    chosen = {"j-stale-origin-stubs": {"disposition": "close_stubs"}}
    ready, held, closed = envelope_mod.ready(steps, by_id, chosen, directive_ids={s.id for s in steps})
    assert "d-close-origin-stub-1" in {s.id for s in ready}
    ready, held, closed = envelope_mod.ready(steps, by_id, {}, directive_ids={s.id for s in steps})
    assert "d-close-origin-stub-1" in {s.id for s in held}
    assert "d-hook-currency" in {s.id for s in ready}


def test_consumer_envelope_hook_repair_is_an_agent_entry_that_opens_its_directive(monkeypatch, tmp_path):
    env = _full_envelope(monkeypatch, tmp_path, profile_author=False)
    assert "j-hook-currency-repair" in {p["id"] for p in env["judgment_points"]}
    assert "d-rag-staleness-regen" not in {d["id"] for d in env["directives"]}
    resolution = decide.resolve(
        env["judgment_points"], env["directives"], rules=rules.POINT_RULES, context={"envelope": env}
    )
    entry = next(e for e in resolution.judgment_entries if e["id"] == "j-hook-currency-repair")
    assert [d["value"] for d in entry["dispositions"]] == ["repair_hooks_now", "defer"]
    assert not any(d.point_id == "j-hook-currency-repair" for d in resolution.decisions)

    steps = envelope_mod.steps_from_envelope(env, stage_of=run_spec.SPEC.stage_of, costs={})
    by_id = {p["id"]: p for p in env["judgment_points"]}
    ids = {s.id for s in steps}
    ready, held, _ = envelope_mod.ready(steps, by_id, {}, directive_ids=ids)
    assert "d-hook-currency" in {s.id for s in held}
    chosen = {"j-hook-currency-repair": {"disposition": "repair_hooks_now"}}
    ready, held, _ = envelope_mod.ready(steps, by_id, chosen, directive_ids=ids)
    assert "d-hook-currency" in {s.id for s in ready}
    assert "repair_hooks_now" in rules.ACTING_DISPOSITIONS


def test_every_emitted_id_has_a_documented_shape(monkeypatch, tmp_path):
    env = _full_envelope(monkeypatch, tmp_path, profile_author=True)
    for directive in env["directives"]:
        assert directive["id"].startswith("d-") and (directive.get("cli") or directive.get("op"))
    for point in env["judgment_points"]:
        assert point["id"].startswith("j-") and point["dispositions"]

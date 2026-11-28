"""Contract tests for the example-retrieval-repo usage/probe adapter."""

import json

import pytest

from coordinator_core.ops.dispatch_emit import rag_use
from coordinator_core.ops.dispatch_emit.rag_use import ProbeState, RagUsage


def _runner(payload, seen=None):
    def run(argv):
        if seen is not None:
            seen.append(argv)
        return payload if isinstance(payload, str) or payload is None else json.dumps(payload)

    return run


def test_touches_ue_path_accepts_ue_paths_and_refuses_python_and_docs():
    for ue in (
        "control/plugin/Foo/Bar.txt", "Source/Game/X.cs", "Plugins/P/Source/P/Y.cpp",
        "a/b.h", "Content/Hair.uasset", "control\\plugin\\x", "A/B.CPP",
    ):
        assert rag_use.touches_ue_path([ue]) is True, ue
    for other in (
        "tools/likeness-prototype/likeness/hairstyle_class/labeller.py", "detector_findings.py",
        "docs/batch-contract.md", "schema/batch.json", "resources/Sourcery.py", "tests/test_x.py",
    ):
        assert rag_use.touches_ue_path([other]) is False, other
    assert rag_use.touches_ue_path([]) is False
    assert rag_use.touches_ue_path(["x.py", "y/Source/z"]) is True


def test_usage_parses_and_builds_argv():
    seen = []
    u = rag_use.read_usage(
        "s1", "2026-01-01T00:00:00Z", "2026-01-01T01:00:00Z",
        runner=_runner({"calls": {"semantic": 2, "symbol": 3}, "timeouts": 1, "degraded": True}, seen),
    )
    assert u == RagUsage(calls=5, by_tool={"semantic": 2, "symbol": 3}, timeouts=1, degraded=True)
    assert seen[0] == ["project-rag", "usage", "--session", "s1", "--since",
                       "2026-01-01T00:00:00Z", "--until", "2026-01-01T01:00:00Z", "--json"]


def test_usage_int_calls_and_zero():
    u = rag_use.read_usage("s", "a", "b", runner=_runner({"calls": 0}))
    assert u is not None and u.calls == 0 and u.by_tool == {} and not u.degraded
    u = rag_use.read_usage("s", "a", "b", runner=_runner({"calls": 7, "by_tool": {"x": 7}}))
    assert u.calls == 7 and u.by_tool == {"x": 7}


def test_usage_unreadable_is_none_not_zero():
    assert rag_use.read_usage("s", "a", "b", runner=_runner(None)) is None
    assert rag_use.read_usage("s", "a", "b", runner=_runner("not json")) is None
    assert rag_use.read_usage("s", "a", "b", runner=_runner({"other": 1})) is None


def test_probe_parses_each_state_and_refuses_unknown():
    for s in ("ok", "degraded", "down"):
        p = rag_use.probe(runner=_runner({"state": s, "per_source": {"engine": s}}))
        assert p == ProbeState(state=s, per_source={"engine": s})
    assert rag_use.probe(runner=_runner({"state": "weird"})) is None
    assert rag_use.probe(runner=_runner(None)) is None


def test_no_runner_is_none():
    assert rag_use.probe() is None
    assert rag_use.read_usage("s", "a", "b") is None
    assert rag_use.read_usage("s", "a", "b", runner=None) is None


def test_is_ue_repo_uses_find_uproject(tmp_path):
    from coordinator_core.ops.dispatch_emit import ue_route_source

    ue_route_source.clear_caches()
    assert rag_use.is_ue_repo(tmp_path) is False
    ue_route_source.clear_caches()
    (tmp_path / "Game.uproject").write_text("{}")
    assert rag_use.is_ue_repo(tmp_path) is True


def test_is_ue_repo_true_for_declared_project_type_without_uproject(tmp_path):
    from coordinator_core.ops.dispatch_emit import ue_route_source

    ue_route_source.clear_caches()
    (tmp_path / "coordinator.local.md").write_text(
        "---\nproject_type: unreal-engine\n---\n", encoding="utf-8"
    )
    assert rag_use.is_ue_repo(tmp_path) is True
    ue_route_source.clear_caches()


def _verdict(**kw):
    base = dict(ue=True, rag_installed=True, rag_calls=3, rag_failed=0)
    base.update(kw)
    return rag_use.docs_check_rag_verdict(**base)


def test_verdict_zero_calls_is_rag_not_used_never_down():
    assert _verdict(rag_calls=0) == "rag_not_used"
    assert _verdict(rag_calls=0, engine_queryable=True) == "rag_not_used"
    assert rag_use.RAG_NOT_USED != rag_use.HALT_RAG_CORPUS_DOWN


def test_verdict_down_when_all_refused_else_degraded():
    assert _verdict(rag_calls=4, rag_failed=4, rag_refused=4) == "rag_corpus_down"
    assert _verdict(rag_calls=4, rag_failed=4) == "degraded"


def test_verdict_timeout_only_is_degraded_not_down():
    assert _verdict(rag_calls=4, rag_failed=4, rag_timeouts=4) == "degraded"
    assert _verdict(rag_calls=4, rag_failed=1, rag_timeouts=1) == "degraded"


def test_verdict_failure_while_queryable_is_degraded():
    assert _verdict(rag_calls=2, rag_failed=2, engine_queryable=True) == "degraded"


def test_verdict_definite_down_still_halts():
    assert _verdict(rag_calls=3, rag_failed=3, rag_refused=3) == "rag_corpus_down"
    assert _verdict(rag_calls=3, rag_failed=3, rag_timeouts=2, rag_refused=1) == "rag_corpus_down"
    assert _verdict(rag_calls=3, rag_failed=1, rag_timeouts=1, engine_queryable=False) == "rag_corpus_down"
    assert _verdict(rag_calls=0, engine_queryable=False) == "rag_corpus_down"


def test_verdict_ok_on_any_success():
    assert _verdict() == "ok"
    assert _verdict(rag_calls=4, rag_failed=3) == "ok"


def test_verdict_not_applicable_off_ue_or_without_rag():
    assert _verdict(ue=False, rag_calls=0) == "not_applicable"
    assert _verdict(rag_installed=False, rag_calls=0) == "not_applicable"


def test_rag_installed_delegates_and_never_raises(tmp_path, monkeypatch):
    from coordinator_core.hooks import example_retrieval_repo_detect

    monkeypatch.setattr(example_retrieval_repo_detect, "find_example_retrieval_repo_dir", lambda d: "/x")
    assert rag_use.rag_installed(tmp_path) is True
    monkeypatch.setattr(example_retrieval_repo_detect, "find_example_retrieval_repo_dir", lambda d: None)
    assert rag_use.rag_installed(tmp_path) is False

    def boom(d):
        raise OSError("x")

    monkeypatch.setattr(example_retrieval_repo_detect, "find_example_retrieval_repo_dir", boom)
    assert rag_use.rag_installed(tmp_path) is False


def test_constants_pinned():
    assert rag_use.DANGER_CALLS == 0
    assert rag_use.DEBOUNCE_SECONDS == 180
    assert rag_use.PROBE_INTERVAL_SECONDS == 60
    assert rag_use.HALT_RAG_CORPUS_DOWN == "rag_corpus_down"
    assert "Restore the code index" in rag_use.RESTORE_MESSAGE


def _row(label, calls, failed=0):
    return {"agent_id": f"id-{label}", "label": label, "rag_calls": calls, "rag_failed": failed}


def test_stage_verdict_unserved_is_not_applicable_even_with_zero_calls():
    out = rag_use.stage_rag_verdict(served=False, rows=[_row("a", 0)])
    assert out == {"verdict": "not_applicable", "rag_calls": 0, "per_worker": []}


def test_stage_verdict_served_zero_calls_is_rag_not_used_and_any_call_is_ok():
    zero = rag_use.stage_rag_verdict(served=True, rows=[_row("a", 0), _row("b", 0)])
    assert zero["verdict"] == rag_use.RAG_NOT_USED and zero["rag_calls"] == 0
    used = rag_use.stage_rag_verdict(served=True, rows=[_row("a", 0), _row("b", 1)])
    assert used["verdict"] == "ok" and used["rag_calls"] == 1
    assert [w["rag_calls"] for w in used["per_worker"]] == [0, 1]


def test_stage_verdict_no_rows_is_unreadable_never_zero():
    for rows in (None, []):
        assert rag_use.stage_rag_verdict(served=True, rows=rows)["verdict"] == rag_use.TRANSCRIPT_UNREADABLE


def test_search_degraded_key_and_failed_attempt_never_halts_unused():
    assert rag_use.SEARCH_DEGRADED_KEY == "search_degraded"
    roles = {"gated": ["*scout*"], "exempt": []}
    row = {"agent_id": "a", "agent_type": "coordinator:scout", "label": "s", "rag_calls": 2, "rag_errors": 2}
    assert rag_use.agent_rag_halt([row], tier=rag_use.TIER_RAG, roles=roles) is None
    silent = {**row, "rag_calls": 0, "rag_errors": 0}
    assert rag_use.agent_rag_halt([silent], tier=rag_use.TIER_RAG, roles=roles)["halted"] == \
        rag_use.HALT_EXAMPLE_RETRIEVAL_REPO_UNUSED


def test_stage_verdict_all_calls_failed_is_degraded():
    assert rag_use.stage_rag_verdict(served=True, rows=[_row("a", 2, 2)])["verdict"] == rag_use.DEGRADED


# --- tier, gated roles, per-agent halt ---------------------------------------------------------

def _tier(tmp_path, *, mcp=(), plugins=(), settings_dir=False, root_sub="repo"):
    settings = tmp_path / "settings"
    if settings_dir:
        (settings / "project-rag").mkdir(parents=True)
    root = tmp_path / root_sub
    root.mkdir(exist_ok=True)
    return rag_use.rag_tier(root, settings=settings, plugin_roots=list(plugins), mcp_names=list(mcp))


def test_halt_constants_pinned_and_classed():
    from coordinator_core.ops.dispatch_emit import halt_remedy_class as hrc

    assert (rag_use.HALT_EXAMPLE_RETRIEVAL_REPO_UNUSED, rag_use.HALT_EXAMPLE_RETRIEVAL_REPO_CADENCE,
            rag_use.HALT_EXAMPLE_RETRIEVAL_REPO_NOT_SERVING, rag_use.HALT_UE_CORPUS_GAP) == (
        "example_retrieval_repo_unused", "example_retrieval_repo_cadence", "example_retrieval_repo_not_serving", "ue_corpus_gap")
    for kind in (rag_use.HALT_EXAMPLE_RETRIEVAL_REPO_UNUSED, rag_use.HALT_EXAMPLE_RETRIEVAL_REPO_CADENCE,
                 rag_use.HALT_EXAMPLE_RETRIEVAL_REPO_NOT_SERVING, rag_use.HALT_UE_CORPUS_GAP):
        assert not hrc.is_mechanical(kind) and hrc.is_irreversible(kind)
    assert rag_use.RAG_NOT_USED == "rag_not_used"


def test_tier_none_when_example_retrieval_repo_absent(tmp_path):
    assert _tier(tmp_path) == "none"
    assert _tier(tmp_path, mcp=["context7", "lsp"]) == "none"


def test_tier_rag_when_installed_by_mcp_settings_dir_or_plugin(tmp_path):
    assert _tier(tmp_path, mcp=["project-rag"]) == "rag"
    assert _tier(tmp_path, settings_dir=True) == "rag"
    plug = tmp_path / "example-retrieval-repo-plugin"
    (plug / ".claude-plugin").mkdir(parents=True)
    (plug / ".claude-plugin" / "plugin.json").write_text('{"name": "project-rag"}', encoding="utf-8")
    assert _tier(tmp_path, plugins=[plug]) == "rag"


def test_tier_ue_for_ue_addon_example_game_repo_or_ue_repo(tmp_path):
    assert _tier(tmp_path, mcp=["project-rag", "example-game-repo-control"]) == "ue"
    assert _tier(tmp_path, mcp=["example-retrieval-repo-ue-addon"]) == "ue"
    ue_root = tmp_path / "uerepo"
    ue_root.mkdir()
    (ue_root / "Game.uproject").write_text("{}", encoding="utf-8")
    assert _tier(tmp_path, mcp=["project-rag"], root_sub="uerepo") == "ue"


def test_ue_repo_without_an_install_stays_none(tmp_path):
    ue_root = tmp_path / "uerepo"
    ue_root.mkdir()
    (ue_root / "Game.uproject").write_text("{}", encoding="utf-8")
    assert _tier(tmp_path, root_sub="uerepo") == "none"


def _roles_file(tmp_path, payload):
    d = tmp_path / "plug" / "coordinator" / "contract"
    d.mkdir(parents=True)
    (d / "example-retrieval-repo-gated-roles.json").write_text(
        payload if isinstance(payload, str) else json.dumps(payload), encoding="utf-8")
    return tmp_path / "plug"


def test_load_gated_roles_reads_the_doe_file(tmp_path):
    root = _roles_file(tmp_path, {"gated": ["plan-author", "*reviewer"], "exempt": ["executor"],
                                  "ue_cadence": {"window_tool_calls": 5, "min_rag_calls": 2}})
    roles = rag_use.load_gated_roles(root)
    assert roles["source"] == "file" and roles["gated"] == ["plan-author", "*reviewer"]
    assert roles["ue_cadence"] == {"window_tool_calls": 5, "min_rag_calls": 2}
    assert rag_use.is_gated("coordinator:plan-author", roles)
    assert not rag_use.is_gated("coordinator:executor", roles)


@pytest.mark.parametrize("payload", [None, "{not json", "[]", {"gated": []}])
def test_missing_or_bad_roles_file_falls_back_to_the_r3_list_and_says_so(tmp_path, payload):
    root = tmp_path / "empty" if payload is None else _roles_file(tmp_path, payload)
    roles = rag_use.load_gated_roles(root)
    assert roles["source"] == "fallback" and roles["reason"]
    for t in ("sizing-scout", "plan-author", "enricher", "prior-art-checker", "premise-checker",
              "coordinator:code-reviewer", "coordinator:staff-eng", "repo-specialist"):
        assert rag_use.is_gated(t, roles), t
    for t in ("coordinator:executor", "test-runner", "op-relay", "", None, "something-else"):
        assert not rag_use.is_gated(t, roles), t


def _arow(agent_type, calls, *, errors=0, positions=(), total=None, empty=0, label=None):
    return {"agent_id": f"id-{agent_type}", "agent_type": agent_type, "label": label or agent_type,
            "rag_calls": calls, "rag_errors": errors, "rag_positions": list(positions),
            "tool_calls": total if total is not None else (max(positions) + 1 if positions else 0),
            "rag_empty": empty}


ROLES = rag_use.load_gated_roles("no-such-root")


def test_tier_none_never_halts_even_with_zero_calls():
    assert rag_use.agent_rag_halt([_arow("plan-author", 0)], tier="none", roles=ROLES) is None


def test_tier_rag_halts_only_on_zero_attempts_of_a_gated_agent():
    halt = rag_use.agent_rag_halt([_arow("plan-author", 0), _arow("enricher", 3)], tier="rag", roles=ROLES)
    assert halt == {"halted": "example_retrieval_repo_unused", "agents": [
        {"agent_id": "id-plan-author", "agent_type": "plan-author", "label": "plan-author", "rag_calls": 0}]}
    assert rag_use.agent_rag_halt([_arow("plan-author", 2), _arow("enricher", 1)], tier="rag", roles=ROLES) is None


def test_failed_calls_count_as_attempts():
    row = _arow("plan-author", 2, errors=2, positions=[0, 1])
    assert rag_use.agent_rag_halt([row], tier="rag", roles=ROLES) is None


def test_exempt_role_with_zero_calls_never_halts():
    rows = [_arow("coordinator:executor", 0), _arow("test-runner", 0), _arow("op-relay", 0)]
    assert rag_use.agent_rag_halt(rows, tier="ue", roles=ROLES) is None


def test_unknown_calls_never_halt():
    row = _arow("plan-author", 0)
    row["rag_calls"] = None
    assert rag_use.agent_rag_halt([row], tier="rag", roles=ROLES) is None


def test_single_use_agent_is_nag_only_at_tier_rag():
    row = _arow("plan-author", 1, positions=[0], total=40)
    assert rag_use.agent_rag_halt([row], tier="rag", roles=ROLES) is None


def test_cadence_boundary_at_tier_ue():
    roles = {**ROLES, "ue_cadence": {"window_tool_calls": 5, "min_rag_calls": 1}}
    ok = _arow("plan-author", 3, positions=[0, 5, 9], total=10)
    assert rag_use.agent_rag_halt([ok], tier="ue", roles=roles) is None  # gaps of exactly 5 hold one call per window
    low = _arow("plan-author", 3, positions=[0, 6, 9], total=10)
    halt = rag_use.agent_rag_halt([low], tier="ue", roles=roles)
    assert halt["halted"] == "example_retrieval_repo_cadence" and halt["agents"][0]["rag_calls"] == 3
    # The same sparse agent is not a cadence halt at tier rag.
    assert rag_use.agent_rag_halt([low], tier="rag", roles=roles) is None


def test_cadence_counts_the_silent_tail_and_min_rag_calls():
    roles = {**ROLES, "ue_cadence": {"window_tool_calls": 4, "min_rag_calls": 2}}
    tail = _arow("plan-author", 4, positions=[0, 1, 2, 3], total=12)
    assert rag_use.agent_rag_halt([tail], tier="ue", roles=roles)["halted"] == "example_retrieval_repo_cadence"
    steady = _arow("plan-author", 6, positions=[0, 1, 4, 5, 8, 9], total=10)
    assert rag_use.agent_rag_halt([steady], tier="ue", roles=roles) is None


def test_short_agent_is_one_window():
    roles = {**ROLES, "ue_cadence": {"window_tool_calls": 10, "min_rag_calls": 1}}
    assert rag_use.agent_rag_halt([_arow("plan-author", 1, positions=[2], total=3)], tier="ue", roles=roles) is None


def test_ue_zero_calls_is_unused_not_cadence():
    halt = rag_use.agent_rag_halt([_arow("plan-author", 0, total=8)], tier="ue", roles=ROLES)
    assert halt["halted"] == "example_retrieval_repo_unused"


def test_ue_not_serving_names_the_layer():
    rows = [_arow("plan-author", 2, positions=[0, 1])]
    halt = rag_use.agent_rag_halt(rows, tier="ue", roles=ROLES, serving={"serving": False, "layer": "semantic"})
    assert halt["halted"] == "example_retrieval_repo_not_serving" and halt["layer"] == "semantic"
    assert rag_use.agent_rag_halt(rows, tier="ue", roles=ROLES, serving=False)["halted"] == "example_retrieval_repo_not_serving"
    assert rag_use.agent_rag_halt(rows, tier="rag", roles=ROLES, serving=False) is None
    assert rag_use.agent_rag_halt(rows, tier="ue", roles=ROLES, serving=True) is None


def test_ue_corpus_gap_when_every_successful_call_is_empty():
    gap = _arow("plan-author", 2, positions=[0, 1])
    gap["rag_empty"] = ["UMetaHumanComponent", "FGroomAsset"]
    halt = rag_use.agent_rag_halt([gap], tier="ue", roles=ROLES)
    assert halt["halted"] == "ue_corpus_gap" and halt["queries"] == ["UMetaHumanComponent", "FGroomAsset"]
    mixed = _arow("plan-author", 3, positions=[0, 1, 2], empty=1)
    assert rag_use.agent_rag_halt([mixed], tier="ue", roles=ROLES) is None
    assert rag_use.agent_rag_halt([gap], tier="rag", roles=ROLES) is None


def _urow(label, ue_calls):
    return {**_arow(label, 1), "ue_calls": ue_calls}


def test_ue_verdict_cause_constants_pinned():
    assert (rag_use.HALT_UE_RETRIEVAL_MISSING, rag_use.HALT_UE_RETRIEVAL_CHECK_FAILED,
            rag_use.HALT_EXAMPLE_RETRIEVAL_REPO_OUTAGE_REPORTED) == (
        "ue_retrieval_missing", "ue_retrieval_check_failed", "example_retrieval_repo_outage_reported")


def test_ue_verdict_zero_ue_calls_is_missing_and_names_the_silent_agents():
    v = rag_use.ue_retrieval_verdict(ue=True, rows=[_urow("a", 0), _urow("b", 2)], friction_rows=[], outage_reports=[])
    assert v["halted"] == v["cause"] == "ue_retrieval_missing"
    assert [a["label"] for a in v["agents"]] == ["a"] and v["agents"][0]["ue_calls"] == 0
    assert v["friction_count"] == 0 and v["reports"] == []


def test_ue_verdict_one_call_per_agent_passes_and_non_ue_never_fails():
    assert rag_use.ue_retrieval_verdict(ue=True, rows=[_urow("a", 1), _urow("b", 1)], friction_rows=[], outage_reports=[]) is None
    assert rag_use.ue_retrieval_verdict(ue=False, rows=[_urow("a", 0)], friction_rows=[{"x": 1}], outage_reports=["down"]) is None
    assert rag_use.ue_retrieval_verdict(ue=True, rows=[], friction_rows=[], outage_reports=[]) is None
    assert rag_use.ue_retrieval_verdict(ue=True, rows=[_urow("a", None)], friction_rows=[], outage_reports=[]) is None


def test_ue_verdict_priority_friction_then_outage_then_missing():
    rows = [_urow("a", 0)]
    both = rag_use.ue_retrieval_verdict(ue=True, rows=rows, friction_rows=[{"r": 1}, {"r": 2}], outage_reports=["example-retrieval-repo down"])
    assert both["halted"] == "ue_retrieval_check_failed" and both["friction_count"] == 2
    out = rag_use.ue_retrieval_verdict(ue=True, rows=rows, friction_rows=[], outage_reports=["example-retrieval-repo down"])
    assert out["halted"] == "example_retrieval_repo_outage_reported" and out["reports"] == ["example-retrieval-repo down"]
    passing = [_urow("a", 3)]
    assert rag_use.ue_retrieval_verdict(ue=True, rows=passing, friction_rows=[{"r": 1}], outage_reports=[])["halted"] == \
        "ue_retrieval_check_failed"
    assert rag_use.ue_retrieval_verdict(ue=True, rows=passing, friction_rows=[], outage_reports=["x"])["halted"] == \
        "example_retrieval_repo_outage_reported"


def test_ue_verdict_breaks_out_denies_and_metrics_rows_per_agent():
    rows = [_urow("a", 0), _urow("b", 1)]
    friction = [{"agent_id": "id-a"}, {"agent_id": "id-a"}, {"agent_id": "id-b"}]
    metrics = [{"agent_id": "id-b", "decision": "retrieval"}, {"agent_id": "id-a", "decision": "deny"}]
    v = rag_use.ue_retrieval_verdict(ue=True, rows=rows, friction_rows=friction, outage_reports=[], metrics_rows=metrics)
    by = {a["label"]: a for a in v["agents"]}
    assert (by["a"]["ue_calls"], by["a"]["denies"], by["a"]["metrics_rows"]) == (0, 2, 1)
    assert (by["b"]["ue_calls"], by["b"]["denies"], by["b"]["metrics_rows"]) == (1, 1, 1)
    quiet = rag_use.ue_retrieval_verdict(ue=True, rows=rows, friction_rows=[], outage_reports=["x"])
    assert all(a["denies"] == 0 and a["metrics_rows"] == 0 for a in quiet["agents"])


def test_agent_usage_counts_ue_calls_by_prefix_or_engine_band():
    from coordinator_core.usage import agent_usage

    def line(name, inp):
        return json.dumps({"message": {"content": [{"type": "tool_use", "id": "t", "name": name, "input": inp}]}})

    def flag(name, inp):
        return agent_usage._tool_uses(line(name, inp), 0)["t"][3]

    assert agent_usage._UE_ADDON_PREFIXES[0].startswith("mcp__project-rag__project_engine_")
    assert flag("mcp__project-rag__project_semantic_search", {"band": "engine_source"}) is True
    assert flag("mcp__project-rag__project_semantic_search", {"bands": ["project", "Engine_runtime"]}) is True
    assert flag("mcp__project-rag__project_semantic_search", {"query": "x"}) is False
    assert flag("mcp__project-rag__project_semantic_search", {"band": "project"}) is False
